"""Katam Virtualization control plane: API v1, sign-in (local, AD/LDAP, SAML), UI."""
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import timedelta
from urllib.parse import urlencode

import libvirt
import yaml
from flask import (Flask, Response, g, jsonify, redirect, request, send_from_directory,
                   session)

from . import __version__, finops, reports, runbooks
from .config import load_config
from .db import (DB, audit, bindings, delete, dr_plans, group_members, groups, images, insert,
                 projects, protection_policies, security_groups, select, templates, tokens, update,
                 users)
from .db import runbooks as runbooks_t
from .driver import Cluster, ClusterError
from .iam import IAM, ROLES, AuthError, Forbidden, saml_request_data, saml_settings
from .images import EXPORT_FORMATS, ImageService, staging_path
from .nodeexec import ExecError, NodeExec
from .tasks import Busy, Tasks

log = logging.getLogger("katam")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
POWER_ACTIONS = {"start", "shutdown", "poweroff", "reboot", "pause", "resume"}


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


def _int(body, key, lo, hi, label, default=None):
    v = body.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{label} must be a whole number")
    if not lo <= v <= hi:
        raise BadRequest(f"{label} must be between {lo} and {hi}")
    return v


def create_app(cfg_path=None, start_background=True):
    cfg = load_config(cfg_path or os.environ.get("KATAM_CONFIG", "/etc/katam/config.yaml"))
    for d in ("console_token_dir", "staging_dir", "exports_dir"):
        os.makedirs(cfg["controller"][d], exist_ok=True)

    db = DB(cfg["controller"]["database_url"])
    iam = IAM(db, cfg)
    iam.bootstrap()
    cluster = Cluster(cfg)
    tasks = Tasks(db)
    nexec = NodeExec(cfg)
    imgs = ImageService(cfg, db, cluster, nexec)

    sdn = None
    if cfg["sdn"]["enabled"]:
        from .sdn import SDN
        sdn = SDN(cfg, db, nexec)
        cluster.sdn = sdn
    from .dr import DR
    from .finops import Metering
    from .ha import HAMonitor
    dr = DR(cfg, db, cluster, nexec)
    metering = Metering(cfg, db, cluster)
    ha = HAMonitor(cfg, db, cluster, tasks)
    from .protection import Locked, Protection
    protection = Protection(cfg, db, cluster, nexec, tasks, imgs)
    from .updates import Updates
    updates = Updates(cfg, ha, nexec)

    here = os.path.dirname(__file__)
    app = Flask(__name__, static_folder=os.path.join(here, "static"), static_url_path="/static")
    app.secret_key = cfg["controller"]["secret_key"]
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=cfg["controller"].get("public_url", "").startswith("https"),
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=cfg["controller"]["session_hours"]),
                      MAX_CONTENT_LENGTH=200 * 2**30)
    app.katam = {"cfg": cfg, "db": db, "iam": iam, "cluster": cluster, "tasks": tasks, "imgs": imgs,
                 "sdn": sdn, "dr": dr, "metering": metering, "ha": ha, "nexec": nexec,
                 "protection": protection, "updates": updates}

    # ---- background services (single leader when several controllers share Postgres) --
    def start_services():
        if sdn:
            try:
                sdn.sync()
            except Exception as e:  # noqa: BLE001
                log.error("SDN sync failed: %s", e)
        metering.start()
        ha.start()
        protection.start()

    if start_background:
        if db.engine.dialect.name == "postgresql":
            def leader():
                conn = db.engine.raw_connection()
                cur = conn.cursor()
                while True:
                    cur.execute("SELECT pg_try_advisory_lock(424242)")
                    if cur.fetchone()[0]:
                        log.info("this controller is the leader")
                        start_services()
                        while True:
                            time.sleep(3600)
                    time.sleep(10)
            threading.Thread(target=leader, daemon=True).start()
        else:
            start_services()

    # ---- errors -----------------------------------------------------------
    @app.errorhandler(BadRequest)
    @app.errorhandler(ClusterError)
    @app.errorhandler(runbooks.RunbookError)
    @app.errorhandler(ValueError)
    def e400(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(Locked)
    def e423(e):
        return jsonify(error=str(e), locked=True), 423

    @app.errorhandler(AuthError)
    def e401(e):
        return jsonify(error=str(e)), 401

    @app.errorhandler(Forbidden)
    def e403(e):
        return jsonify(error=str(e)), 403

    @app.errorhandler(NotFound)
    def e404(e):
        return jsonify(error=str(e)), 404

    @app.errorhandler(Busy)
    def e409(e):
        return jsonify(error=str(e)), 409

    @app.errorhandler(libvirt.libvirtError)
    def e502(e):
        return jsonify(error=e.get_error_message()), 502

    @app.errorhandler(ExecError)
    def e502x(e):
        return jsonify(error=str(e)), 502

    # ---- authentication ----------------------------------------------------
    @app.before_request
    def authenticate():
        g.p = None
        path = request.path
        if path.startswith("/static/") or path in ("/", "/login", "/auth/login", "/auth/config",
                                                   "/healthz") or path.startswith("/auth/saml/"):
            return
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer katam_"):
            g.p = iam.token_principal(auth[7:])
            return
        s = session.get("u")
        if not s:
            return jsonify(error="Sign in required"), 401
        # Header check blocks cross-site form posts riding on the session cookie.
        if request.method not in ("GET", "HEAD") and request.headers.get("X-Katam") != "1":
            return jsonify(error="Missing X-Katam header"), 400
        u = db.one(select(users).where(users.c.name == s["name"]))
        if not u or u["disabled"]:
            session.clear()
            return jsonify(error="Account disabled"), 401
        g.p = iam.principal(s["name"], s["source"], s.get("groups", []))

    def need(perm, project=None):
        if not g.p or not g.p.can(perm, project):
            raise Forbidden(f"You don't have {perm}" + (f" in project {project}" if project else ""))

    def actor():
        return g.p.name if g.p else "system"

    def all_projects():
        return [p["name"] for p in db.all(select(projects))]

    def visible(perm):
        return g.p.projects_with(perm, all_projects())

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, version=__version__)

    @app.get("/auth/config")
    def auth_config():
        return jsonify(local=cfg["auth"].get("local", True), ldap=cfg["auth"]["ldap"].get("enabled", False),
                       saml=cfg["auth"]["saml"].get("enabled", False),
                       saml_label=cfg["auth"]["saml"].get("button_label", "Sign in with SSO"))

    @app.post("/auth/login")
    def login():
        b = request.get_json(force=True) or {}
        try:
            p = iam.login(b.get("username"), b.get("password"))
        except AuthError as e:
            db.audit(b.get("username") or "?", "login", "katam", "failed", str(e))
            raise
        session.clear()
        session.permanent = True
        session["u"] = {"name": p.name, "source": p.source, "groups": p.groups}
        db.audit(p.name, "login", "katam", "ok", p.source)
        return jsonify(user=p.name, admin=p.is_admin)

    @app.post("/auth/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/auth/me")
    def me():
        return jsonify(user=g.p.name, source=g.p.source, groups=g.p.groups, admin=g.p.is_admin,
                       grants={k: sorted(v) for k, v in g.p.grants.items()})

    def _saml():
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        sc = cfg["auth"]["saml"]
        if not sc.get("enabled"):
            raise NotFound("SAML is not enabled")
        return OneLogin_Saml2_Auth(saml_request_data(request), saml_settings(sc, cfg["controller"]["public_url"]))

    @app.get("/auth/saml/login")
    def saml_login():
        return redirect(_saml().login(return_to="/"))

    @app.post("/auth/saml/acs")
    def saml_acs():
        a = _saml()
        a.process_response()
        if a.get_errors() or not a.is_authenticated():
            db.audit("?", "login.saml", "katam", "failed", ", ".join(a.get_errors()))
            return Response(f"SSO sign-in failed: {a.get_last_error_reason() or 'not authenticated'}", 401)
        sc = cfg["auth"]["saml"]
        attrs = a.get_attributes()
        name_attr = sc.get("username_attribute")
        name = (attrs.get(name_attr, [None])[0] if name_attr else None) or a.get_nameid()
        name = name.split("\\")[-1].lower()
        grp_attr = sc.get("groups_attribute", "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups")
        p = iam.saml_principal(name, attrs.get(grp_attr, []),
                               display=(attrs.get(sc.get("display_attribute", ""), [None]) or [None])[0])
        session.clear()
        session.permanent = True
        session["u"] = {"name": p.name, "source": "saml", "groups": p.groups}
        db.audit(p.name, "login", "katam", "ok", "saml")
        return redirect("/")

    @app.get("/auth/saml/metadata")
    def saml_metadata():
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
        s = OneLogin_Saml2_Settings(saml_settings(cfg["auth"]["saml"], cfg["controller"]["public_url"]),
                                    sp_validation_only=True)
        return Response(s.get_sp_metadata(), mimetype="text/xml")

    # ---- UI ----------------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ---- IAM ---------------------------------------------------------------
    @app.get("/api/v1/users")
    def users_list():
        need("iam.admin")
        return jsonify([{k: v for k, v in u.items() if k != "password_hash"} for u in db.all(select(users))])

    @app.post("/api/v1/users")
    def users_create():
        need("iam.admin")
        b = request.get_json(force=True)
        name = (b.get("name") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]{2,64}", name):
            raise BadRequest("User names: 2-64 of a-z 0-9 . _ -")
        if len(b.get("password") or "") < 12:
            raise BadRequest("Passwords need at least 12 characters")
        iam.create_user(name, b["password"], b.get("display"), b.get("email"))
        db.audit(actor(), "user.create", name)
        return jsonify(ok=True), 201

    @app.patch("/api/v1/users/<name>")
    def users_update(name):
        b = request.get_json(force=True)
        if name != g.p.name:
            need("iam.admin")
        if "password" in b:
            if len(b["password"]) < 12:
                raise BadRequest("Passwords need at least 12 characters")
            iam.set_password(name, b["password"])
        if "disabled" in b:
            need("iam.admin")
            db.run(update(users).where(users.c.name == name).values(disabled=bool(b["disabled"])))
        db.audit(actor(), "user.update", name, detail=sorted(k for k in b if k != "password"))
        return jsonify(ok=True)

    @app.get("/api/v1/groups")
    def groups_list():
        need("iam.admin")
        out = []
        for gr in db.all(select(groups)):
            gr["members"] = [m["user_name"] for m in db.all(select(group_members).where(group_members.c.group_name == gr["name"]))]
            out.append(gr)
        return jsonify(out)

    @app.put("/api/v1/groups/<name>")
    def groups_put(name):
        need("iam.admin")
        b = request.get_json(force=True)
        if not db.one(select(groups).where(groups.c.name == name)):
            db.run(insert(groups).values(name=name, description=b.get("description")))
        db.run(delete(group_members).where(group_members.c.group_name == name))
        for m in b.get("members", []):
            db.run(insert(group_members).values(group_name=name, user_name=m))
        db.audit(actor(), "group.update", name)
        return jsonify(ok=True)

    @app.get("/api/v1/role-bindings")
    def bindings_list():
        need("iam.admin")
        return jsonify(roles={k: sorted(v) for k, v in ROLES.items()}, bindings=db.all(select(bindings)))

    @app.post("/api/v1/role-bindings")
    def bindings_add():
        need("iam.admin")
        b = request.get_json(force=True)
        if b.get("project", "*") != "*" and b["project"] not in all_projects():
            raise BadRequest(f"No project {b['project']}")
        iam.bind(b["subject"], b["role"], b.get("project", "*"))
        db.audit(actor(), "binding.add", b["subject"], detail=b)
        return jsonify(ok=True), 201

    @app.delete("/api/v1/role-bindings/<int:bid>")
    def bindings_del(bid):
        need("iam.admin")
        iam.unbind(bid)
        db.audit(actor(), "binding.delete", str(bid))
        return jsonify(ok=True)

    @app.get("/api/v1/tokens")
    def tokens_list():
        q = select(tokens.c.id, tokens.c.user_name, tokens.c.name, tokens.c.expires, tokens.c.last_used, tokens.c.created)
        if not g.p.is_admin:
            q = q.where(tokens.c.user_name == g.p.name)
        return jsonify(db.all(q))

    @app.post("/api/v1/tokens")
    def tokens_create():
        if g.p.via_token:
            raise Forbidden("Create tokens from a signed-in session, not with another token")
        b = request.get_json(force=True)
        t = iam.create_token(g.p.name, b.get("name") or "token", b.get("days"))
        db.audit(actor(), "token.create", b.get("name") or "token")
        return jsonify(token=t, note="Shown once. Use it as: Authorization: Bearer <token>"), 201

    @app.delete("/api/v1/tokens/<tid>")
    def tokens_delete(tid):
        t = db.one(select(tokens).where(tokens.c.id == tid))
        if not t or (t["user_name"] != g.p.name and not g.p.is_admin):
            raise NotFound("No such token")
        db.run(delete(tokens).where(tokens.c.id == tid))
        db.audit(actor(), "token.delete", tid)
        return jsonify(ok=True)

    @app.get("/api/v1/projects")
    def projects_list():
        vis = visible("vm.read")
        out = []
        usage_by = _project_usage()
        for p in db.all(select(projects)):
            if p["name"] in vis:
                p["quota"] = json.loads(p["quota"] or "{}")
                p["usage"] = usage_by.get(p["name"], {})
                out.append(p)
        return jsonify(out)

    @app.post("/api/v1/projects")
    def projects_create():
        need("iam.admin")
        b = request.get_json(force=True)
        name = (b.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise BadRequest("Project names: lowercase letters, digits, hyphens")
        db.run(insert(projects).values(name=name, description=b.get("description"),
                                       quota=json.dumps(b.get("quota") or {}),
                                       cost_center=b.get("cost_center"), created=time.time()))
        db.audit(actor(), "project.create", name)
        return jsonify(ok=True), 201

    @app.patch("/api/v1/projects/<name>")
    def projects_update(name):
        need("iam.admin")
        b = request.get_json(force=True)
        vals = {}
        if "quota" in b:
            vals["quota"] = json.dumps(b["quota"])
        for k in ("description", "cost_center"):
            if k in b:
                vals[k] = b[k]
        db.run(update(projects).where(projects.c.name == name).values(**vals))
        db.audit(actor(), "project.update", name, detail=b)
        return jsonify(ok=True)

    def _project_usage():
        out = {}
        for v in cluster.list_vms():
            u = out.setdefault(v["project"], {"vms": 0, "vcpus": 0, "memory_gb": 0, "gpus": 0})
            u["vms"] += 1
            u["vcpus"] += v["vcpus"]
            u["memory_gb"] += v["memory_mb"] / 1024
            u["gpus"] += v.get("gpus", 0)
        return out

    @app.get("/api/v1/audit")
    def audit_list():
        need("audit.read")
        lim = min(int(request.args.get("limit", 200)), 2000)
        return jsonify(db.all(select(audit).order_by(audit.c.ts.desc()).limit(lim)))

    # ---- nodes -------------------------------------------------------------
    @app.get("/api/v1/nodes")
    def nodes():
        if not visible("vm.read"):
            raise Forbidden("No access")
        states = ha.states()
        out = cluster.nodes_summary()
        for n in out:
            st = states.get(n["name"], {})
            n["ha_status"] = st.get("status", "ok")
            n["ha_detail"] = st.get("detail", "")
            n["site"] = next(x for x in cfg["nodes"] if x["name"] == n["name"])["site"]
        return jsonify(out)

    @app.get("/api/v1/nodes/<node>/gpus")
    def node_gpus(node):
        if not visible("vm.read"):
            raise Forbidden("No access")
        return jsonify(cluster.node_gpus(node))

    @app.post("/api/v1/nodes/<node>/maintenance")
    def node_maint(node):
        need("iam.admin")
        on = bool((request.get_json(force=True) or {}).get("on", True))
        tid = tasks.submit("node.maintenance", f"node:{node}", lambda p: ha.maintenance(node, on, p), actor())
        return jsonify(task=tid), 202

    @app.post("/api/v1/nodes/<node>/recover")
    def node_recover(node):
        need("iam.admin")
        removed = ha.recover(node)
        db.audit(actor(), "node.recover", node, detail=removed)
        return jsonify(removed=removed)

    @app.post("/api/v1/nodes/<node>/confirm-down")
    def node_confirm(node):
        need("iam.admin")
        tid = ha.confirm_down(node)
        db.audit(actor(), "node.confirm_down", node)
        return jsonify(task=tid), 202

    # ---- catalog -----------------------------------------------------------
    @app.get("/api/v1/catalog")
    def catalog():
        vis = visible("vm.write")
        imgs_ = [i for i in db.all(select(images).where(images.c.status == "ready"))
                 if i["project"] in (None, *vis)]
        tpls = [t for t in db.all(select(templates)) if t["project"] in (None, *vis)]
        return jsonify(
            images=[{"name": i["name"], "label": i["label"], "os_type": i["os_type"],
                     "size_gb": i["size_gb"], "cloud_init": i["cloud_init"]} for i in imgs_],
            templates=[{"name": t["name"], "label": t["label"], "spec": json.loads(t["spec"])} for t in tpls],
            isos=cluster.list_isos(),
            networks=[{"name": n["name"], "label": n.get("label", n["name"]), "type": n["type"]} for n in cfg["networks"]],
            nodes=[n["name"] for n in cfg["nodes"] if n["site"] == "primary"],
            projects=sorted(vis),
            secgroups=[s["name"] for s in db.all(select(security_groups)) if s["project"] in (None, *vis)],
            limits=cfg["limits"], gpu_models=cluster.gpu_models(),
            default_user=cfg["cloud_init"]["default_user"], sdn=bool(sdn),
            export_formats=sorted(EXPORT_FORMATS))

    # ---- VMs ---------------------------------------------------------------
    def vm_or_404(name):
        for v in cluster.list_vms():
            if v["name"] == name:
                return v
        raise NotFound(f"No VM named {name}")

    def parse_create(body):
        body = dict(body)
        tpl_name = body.get("template")
        if tpl_name:
            t = db.one(select(templates).where(templates.c.name == tpl_name))
            if not t:
                raise BadRequest(f"No template named {tpl_name}")
            body = {**json.loads(t["spec"]), **{k: v for k, v in body.items() if v not in (None, "")}}
        lim = cfg["limits"]
        project = body.get("project") or "default"
        if project not in all_projects():
            raise BadRequest(f"No project {project}")
        need("vm.write", project)
        name = str(body.get("name", "")).strip().lower()
        if not NAME_RE.match(name):
            raise BadRequest("Name must be 2-40 characters: lowercase letters, digits and hyphens")

        image_name, iso = body.get("image") or None, body.get("iso") or None
        if bool(image_name) == bool(iso):
            raise BadRequest("Pick either an image (or template) or an install ISO")
        image_rec = None
        if image_name:
            image_rec = db.one(select(images).where(images.c.name == image_name))
            if not image_rec or image_rec["status"] != "ready":
                raise BadRequest(f"Image {image_name} isn't ready")
            if image_rec["project"] not in (None, project):
                raise Forbidden(f"Image {image_name} belongs to project {image_rec['project']}")
            image_rec = {**image_rec, "volumes": json.loads(image_rec["volumes"])}

        os_type = body.get("os_type") or (image_rec or {}).get("os_type") or "linux"
        if os_type not in ("linux", "windows", "other"):
            raise BadRequest("OS type must be linux, windows or other")
        firmware = body.get("firmware") or (image_rec or {}).get("firmware") or "bios"
        if firmware not in ("bios", "uefi"):
            raise BadRequest("Firmware must be bios or uefi")

        nets = body.get("networks") or []
        known = {n["name"] for n in cfg["networks"]}
        if not nets or any(n not in known for n in nets):
            raise BadRequest("Pick at least one network from the list")
        node = body.get("node") or None
        if node and node not in {n["name"] for n in cfg["nodes"]}:
            raise BadRequest(f"Unknown node '{node}'")
        ip = (body.get("ip") or "").strip() or None
        if ip:
            try:
                if "/" not in ip:
                    raise ValueError
                ipaddress.ip_interface(ip)
            except ValueError:
                raise BadRequest("Static IP needs CIDR form, e.g. 172.20.20.50/24")
        keys = body.get("ssh_keys") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.splitlines() if k.strip()]
        sgs = body.get("secgroups") or []
        if isinstance(sgs, str):
            sgs = [s for s in sgs.replace(",", " ").split() if s]
        min_disk = int(-(-((image_rec or {}).get("size_gb") or 0) // 1))
        req = {
            "name": name, "project": project, "os_type": "windows" if os_type == "windows" else "linux",
            "image_rec": image_rec, "iso": iso, "template": tpl_name,
            "vcpus": _int(body, "vcpus", 1, lim["max_vcpus"], "vCPUs"),
            "memory_mb": _int(body, "memory_gb", 1, lim["max_memory_gb"], "Memory (GB)") * 1024,
            "disk_gb": max(_int(body, "disk_gb", 1, lim["max_disk_gb"], "Disk (GB)"), min_disk),
            "data_disks": [int(x) for x in body.get("data_disks") or []],
            "networks": nets, "node": node, "ip": ip, "gateway": body.get("gateway"),
            "ssh_keys": keys, "password": body.get("password") or None, "firmware": firmware,
            "gpus": _int(body, "gpus", 0, 8, "GPUs", 0), "gpu_model": body.get("gpu_model") or None,
            "secgroups": sgs, "ha": bool(body.get("ha", True)),
            "start": bool(body.get("start", True)), "autostart": bool(body.get("autostart", False)),
            "description": (body.get("description") or "")[:200] or None, "owner": actor(),
        }
        _check_quota(req)
        return req

    def _check_quota(req):
        p = db.one(select(projects).where(projects.c.name == req["project"]))
        q = json.loads(p["quota"] or "{}")
        if not q:
            return
        u = _project_usage().get(req["project"], {"vms": 0, "vcpus": 0, "memory_gb": 0, "gpus": 0})
        checks = [("vms", u["vms"] + 1), ("vcpus", u["vcpus"] + req["vcpus"]),
                  ("memory_gb", u["memory_gb"] + req["memory_mb"] / 1024), ("gpus", u["gpus"] + req["gpus"])]
        for k, val in checks:
            if k in q and val > q[k]:
                raise BadRequest(f"Project {req['project']} quota exceeded: {k} would be {val:g} of {q[k]}")

    def create_vm_sync(body, progress):
        req = parse_create(body)
        return cluster.create_vm(req, progress)

    @app.get("/api/v1/vms")
    def vms():
        vis = visible("vm.read")
        return jsonify([v for v in cluster.list_vms() if v["project"] in vis])

    @app.get("/api/v1/vms/<name>")
    def vm_get(name):
        v = vm_or_404(name)
        need("vm.read", v["project"])
        return jsonify(v)

    @app.post("/api/v1/vms")
    def vm_create():
        req = parse_create(request.get_json(force=True))
        tid = tasks.submit("vm.create", req["name"], lambda p: cluster.create_vm(req, p), actor(), req["project"])
        return jsonify(task=tid), 202

    @app.patch("/api/v1/vms/<name>")
    def vm_patch(name):
        v = vm_or_404(name)
        need("vm.write", v["project"])
        b = request.get_json(force=True)
        if "autostart" in b:
            cluster.set_autostart(name, bool(b["autostart"]))
        if "ha" in b:
            cluster.set_meta(name, ha="1" if b["ha"] else "0")
        if "secgroups" in b:
            if not sdn:
                raise BadRequest("Security groups need SDN (sdn.enabled in config.yaml)")
            sdn.set_vm_groups(name, b["secgroups"])
            cluster.set_meta(name, secgroups=",".join(b["secgroups"]))
        db.audit(actor(), "vm.update", name, detail=b)
        return jsonify(ok=True)

    @app.post("/api/v1/vms/<name>/power/<action>")
    def vm_power(name, action):
        if action not in POWER_ACTIONS:
            raise BadRequest(f"Unknown action '{action}'")
        v = vm_or_404(name)
        need("vm.power", v["project"])
        tid = tasks.submit(f"vm.{action}", name, lambda p: (cluster.power(name, action), f"{action}: {name}")[1],
                           actor(), v["project"])
        return jsonify(task=tid), 202

    @app.post("/api/v1/vms/<name>/migrate")
    def vm_migrate(name):
        v = vm_or_404(name)
        need("vm.migrate", v["project"])
        dest = (request.get_json(force=True) or {}).get("node")
        if dest not in cluster.nodes:
            raise BadRequest("Pick a destination node")
        if ha.state(dest) != "ok":
            raise BadRequest(f"{dest} is {ha.state(dest)}")
        tid = tasks.submit("vm.migrate", name, lambda p: cluster.migrate_vm(name, dest, p), actor(), v["project"])
        return jsonify(task=tid), 202

    @app.delete("/api/v1/vms/<name>")
    def vm_delete(name):
        v = vm_or_404(name)
        need("vm.write", v["project"])
        protection.guard_vm_delete(name, actor())   # refuses if snapshots are locked
        tid = tasks.submit("vm.delete", name, lambda p: cluster.delete_vm(name, p), actor(), v["project"])
        return jsonify(task=tid), 202

    @app.post("/api/v1/vms/<name>/export")
    def vm_export(name):
        v = vm_or_404(name)
        need("vm.export", v["project"])
        fmt = (request.get_json(force=True) or {}).get("format", "ova")
        tid = tasks.submit("vm.export", name, lambda p: imgs.export_vm(name, fmt, p), actor(), v["project"])
        return jsonify(task=tid), 202

    @app.post("/api/v1/vms/<name>/capture")
    def vm_capture(name):
        v = vm_or_404(name)
        need("image.write", v["project"])
        b = request.get_json(force=True) or {}
        img = (b.get("name") or "").lower()
        if not NAME_RE.match(img):
            raise BadRequest("Give the new image a name: lowercase letters, digits, hyphens")
        tid = tasks.submit("vm.capture", name, lambda p: imgs.capture(name, img, b.get("project") or v["project"], p),
                           actor(), v["project"])
        return jsonify(task=tid), 202

    @app.post("/api/v1/vms/<name>/console")
    def vm_console(name):
        v = vm_or_404(name)
        need("vm.console", v["project"])
        host, port, vnc_pw = cluster.console(name)
        tdir = cfg["controller"]["console_token_dir"]
        now = time.time()
        for f in os.listdir(tdir):
            pth = os.path.join(tdir, f)
            if now - os.path.getmtime(pth) > 7200:
                os.remove(pth)
        token = secrets.token_urlsafe(16)
        with open(os.path.join(tdir, token), "w") as f:
            f.write(f"{token}: {host}:{port}\n")
        qs = {"path": f"websockify?token={token}", "autoconnect": "1", "resize": "scale"}
        if vnc_pw:
            qs["password"] = vnc_pw
        db.audit(actor(), "vm.console", name)
        return jsonify(url=f"{cfg['controller']['novnc_url'].rstrip('/')}/vnc.html?{urlencode(qs)}")

    # ---- images, templates, exports ----------------------------------------
    @app.get("/api/v1/images")
    def images_list():
        vis = visible("image.read")
        return jsonify([{**i, "volumes": json.loads(i["volumes"] or "[]")} for i in db.all(select(images))
                        if i["project"] in (None, *vis)])

    @app.post("/api/v1/images")
    def images_create():
        if request.content_type and request.content_type.startswith("multipart/"):
            meta, f = request.form, request.files.get("file")
            if not f:
                raise BadRequest("Attach a file")
            source = {"upload": staging_path(cfg, f.filename)}
            f.save(source["upload"])
        else:
            meta = request.get_json(force=True)
            if not (meta.get("url") or "").startswith(("http://", "https://")):
                raise BadRequest("Give an http(s) URL or upload a file")
            source = {"url": meta["url"]}
        project = meta.get("project") or None
        if project:
            need("image.write", project)
        else:
            need("iam.admin")  # shared images
        name = (meta.get("name") or "").lower()
        if not NAME_RE.match(name):
            raise BadRequest("Image names: lowercase letters, digits, hyphens")
        imgs.begin(name, project, meta.get("label"))
        opts = dict(os_type=meta.get("os_type", "linux"), firmware=meta.get("firmware", "bios"),
                    cloud_init=str(meta.get("cloud_init", "true")).lower() in ("1", "true", "yes", "on"),
                    v2v=str(meta.get("v2v", "false")).lower() in ("1", "true", "yes", "on"),
                    kind=meta.get("kind", "disk"))
        tid = tasks.submit("image.import", f"image:{name}", lambda p: imgs.import_image(name, source, p, **opts),
                           actor(), project)
        return jsonify(task=tid), 202

    @app.delete("/api/v1/images/<name>")
    def images_delete(name):
        i = db.one(select(images).where(images.c.name == name))
        if not i:
            raise NotFound(f"No image {name}")
        need("image.write", i["project"]) if i["project"] else need("iam.admin")
        tid = tasks.submit("image.delete", f"image:{name}", lambda p: (imgs.delete_image(name), f"Deleted {name}")[1],
                           actor(), i["project"])
        return jsonify(task=tid), 202

    @app.get("/api/v1/templates")
    def templates_list():
        vis = visible("template.read")
        return jsonify([{**t, "spec": json.loads(t["spec"])} for t in db.all(select(templates))
                        if t["project"] in (None, *vis)])

    @app.put("/api/v1/templates/<name>")
    def templates_put(name):
        b = request.get_json(force=True)
        project = b.get("project") or None
        need("template.write", project) if project else need("iam.admin")
        if not NAME_RE.match(name):
            raise BadRequest("Template names: lowercase letters, digits, hyphens")
        spec = b.get("spec") or {}
        if not (spec.get("image") or spec.get("iso")):
            raise BadRequest("A template's spec needs an image or iso")
        row = dict(label=b.get("label") or name, project=project, spec=json.dumps(spec))
        if db.one(select(templates).where(templates.c.name == name)):
            db.run(update(templates).where(templates.c.name == name).values(**row))
        else:
            db.run(insert(templates).values(name=name, created=time.time(), **row))
        db.audit(actor(), "template.save", name)
        return jsonify(ok=True)

    @app.delete("/api/v1/templates/<name>")
    def templates_delete(name):
        t = db.one(select(templates).where(templates.c.name == name))
        if not t:
            raise NotFound(f"No template {name}")
        need("template.write", t["project"]) if t["project"] else need("iam.admin")
        db.run(delete(templates).where(templates.c.name == name))
        db.audit(actor(), "template.delete", name)
        return jsonify(ok=True)

    @app.get("/api/v1/exports")
    def exports_list():
        if not visible("vm.export"):
            raise Forbidden("No access")
        return jsonify(imgs.list_exports())

    @app.get("/api/v1/exports/<path:fname>")
    def exports_get(fname):
        if not visible("vm.export"):
            raise Forbidden("No access")
        return send_from_directory(cfg["controller"]["exports_dir"], fname, as_attachment=True)

    # ---- networks and security groups --------------------------------------
    @app.get("/api/v1/networks")
    def networks():
        if not visible("network.read"):
            raise Forbidden("No access")
        return jsonify(cfg["networks"])

    @app.get("/api/v1/security-groups")
    def sg_list():
        vis = visible("secgroup.read")
        return jsonify([{**s, "rules": json.loads(s["rules"])} for s in db.all(select(security_groups))
                        if s["project"] in (None, *vis)])

    @app.put("/api/v1/security-groups/<name>")
    def sg_put(name):
        if not sdn:
            raise BadRequest("Security groups need SDN (sdn.enabled in config.yaml)")
        b = request.get_json(force=True)
        project = b.get("project") or None
        need("secgroup.write", project) if project else need("iam.admin")
        rules = sdn.save_group(name, project, b.get("description"), b.get("rules") or [])
        db.audit(actor(), "secgroup.save", name, detail=rules)
        return jsonify(rules=rules)

    @app.delete("/api/v1/security-groups/<name>")
    def sg_delete(name):
        if not sdn:
            raise BadRequest("SDN is not enabled")
        s = db.one(select(security_groups).where(security_groups.c.name == name))
        if not s:
            raise NotFound(f"No group {name}")
        need("secgroup.write", s["project"]) if s["project"] else need("iam.admin")
        sdn.delete_group(name)
        db.audit(actor(), "secgroup.delete", name)
        return jsonify(ok=True)

    # ---- tasks -------------------------------------------------------------
    @app.get("/api/v1/tasks")
    def task_list():
        if g.p.can("task.read", "*"):
            return jsonify(tasks.recent())
        vis = visible("task.read")
        return jsonify([t for t in tasks.recent(limit=200) if t["project"] in vis or t["actor"] == g.p.name][:40])

    @app.get("/api/v1/tasks/<tid>")
    def task_get(tid):
        t = tasks.get(tid)
        if not t:
            raise NotFound("No such task")
        if not (g.p.can("task.read", "*") or t["actor"] == g.p.name or g.p.can("task.read", t["project"])):
            raise Forbidden("No access to this task")
        return jsonify(t)

    # ---- runbooks ----------------------------------------------------------
    def hooks(principal_name):
        def power(vm, action):
            v = vm_or_404(vm)
            need("vm.power", v["project"])
            cluster.power(vm, action)

        def secure(perm, fn):
            def wrapped(vm, *a):
                need(perm, vm_or_404(vm)["project"])
                return fn(vm, *a)
            return wrapped

        return {
            "list_vms": cluster.list_vms,
            "create_vm": create_vm_sync,
            "power": power,
            "delete_vm": secure("vm.write", cluster.delete_vm),
            "migrate_vm": secure("vm.migrate", cluster.migrate_vm),
            "export_vm": secure("vm.export", imgs.export_vm),
            "domain": lambda vm: (need("vm.write", vm_or_404(vm)["project"]), cluster._must(vm)[1])[1],
            "set_secgroups": lambda vm, gs: (sdn.set_vm_groups(vm, gs), cluster.set_meta(vm, secgroups=",".join(gs))),
            "dr_run": lambda plan, mode, p: (need("dr.run"), dr.run(plan, mode, p))[1],
            "run_runbook": lambda n, params, p: runbooks.Runner(hooks(principal_name)).run(
                runbooks.parse(db.one(select(runbooks_t).where(runbooks_t.c.name == n))["source"]), params, p),
        }

    @app.get("/api/v1/runbooks")
    def rb_list():
        if not visible("runbook.read"):
            raise Forbidden("No access")
        return jsonify(db.all(select(runbooks_t)))

    @app.get("/api/v1/runbooks/examples")
    def rb_examples():
        return jsonify(runbooks.EXAMPLES)

    @app.put("/api/v1/runbooks/<name>")
    def rb_put(name):
        need("iam.admin")
        src = request.get_data(as_text=True)
        if request.is_json:
            src = request.get_json()["source"]
        rb = runbooks.parse(src)
        if rb["name"] != name:
            raise BadRequest(f"The YAML's name ({rb['name']}) must match the URL ({name})")
        row = dict(description=rb.get("description"), source=src, updated=time.time())
        if db.one(select(runbooks_t).where(runbooks_t.c.name == name)):
            db.run(update(runbooks_t).where(runbooks_t.c.name == name).values(**row))
        else:
            db.run(insert(runbooks_t).values(name=name, **row))
        db.audit(actor(), "runbook.save", name)
        return jsonify(ok=True, steps=len(rb["steps"]))

    @app.delete("/api/v1/runbooks/<name>")
    def rb_delete(name):
        need("iam.admin")
        db.run(delete(runbooks_t).where(runbooks_t.c.name == name))
        return jsonify(ok=True)

    @app.post("/api/v1/runbooks/<name>/run")
    def rb_run(name):
        if not visible("runbook.run"):
            raise Forbidden("You can't run runbooks")
        row = db.one(select(runbooks_t).where(runbooks_t.c.name == name))
        if not row:
            raise NotFound(f"No runbook {name}")
        b = request.get_json(force=True) or {}
        rb = runbooks.parse(row["source"])
        p_obj = g.p
        if b.get("dry_run"):
            return jsonify(plan=runbooks.Runner(hooks(p_obj.name)).plan(rb, b.get("params")))

        def go(progress):
            # Runbook steps run in a worker thread; carry the caller's identity into it.
            with app.test_request_context():
                g.p = p_obj
                return runbooks.Runner(hooks(p_obj.name)).run(rb, b.get("params"), progress)
        tid = tasks.submit("runbook", f"runbook:{name}", go, actor())
        return jsonify(task=tid), 202

    # ---- DR ----------------------------------------------------------------
    @app.get("/api/v1/dr/plans")
    def dr_list():
        if not visible("dr.read"):
            raise Forbidden("No access")
        return jsonify([{**p, "spec": json.loads(p["spec"]),
                         "last_result": json.loads(p["last_result"]) if p["last_result"] else None}
                        for p in db.all(select(dr_plans))])

    @app.put("/api/v1/dr/plans/<name>")
    def dr_put(name):
        need("iam.admin")
        dr.save_plan(name, request.get_json(force=True))
        db.audit(actor(), "dr.plan.save", name)
        return jsonify(ok=True)

    @app.delete("/api/v1/dr/plans/<name>")
    def dr_delete(name):
        need("iam.admin")
        db.run(delete(dr_plans).where(dr_plans.c.name == name))
        return jsonify(ok=True)

    @app.post("/api/v1/dr/plans/<name>/run")
    def dr_run(name):
        mode = (request.get_json(force=True) or {}).get("mode", "test")
        need("iam.admin") if mode in ("planned", "disaster") else need("dr.read")
        if mode in ("planned", "disaster") and (request.get_json(force=True) or {}).get("confirm") != name:
            raise BadRequest(f"Confirm a {mode} failover by sending confirm: \"{name}\"")
        tid = tasks.submit(f"dr.{mode}", f"dr:{name}", lambda p: dr.run(name, mode, p), actor())
        return jsonify(task=tid), 202

    # ---- FinOps and reports ------------------------------------------------
    def _period():
        days = float(request.args.get("days", 30))
        end = time.time()
        return end - days * 86400, end

    @app.get("/api/v1/finops/summary")
    def finops_summary():
        vis = visible("finops.read")
        if not vis:
            raise Forbidden("No access")
        start, end = _period()
        rep = finops.cost_report(db, cfg, start, end, None if g.p.can("finops.read", "*") else vis)
        rep["rightsizing"] = finops.rightsizing(rep, cfg["finops"]["rates"])
        rep["rates"] = cfg["finops"]["rates"]
        return jsonify(rep)

    @app.post("/api/v1/finops/sample")
    def finops_sample():
        need("iam.admin")
        return jsonify(samples=metering.sample())

    @app.get("/api/v1/reports")
    def reports_list():
        return jsonify([{"name": k, "description": v} for k, v in reports.REPORTS.items()])

    @app.get("/api/v1/reports/<name>")
    def report(name):
        if name not in reports.REPORTS:
            raise NotFound(f"No report {name}")
        need("audit.read") if name == "audit" else None
        vis = visible("report.read")
        if not vis:
            raise Forbidden("No access")
        start, end = _period()
        ctx = {"cluster": cluster, "db": db, "cfg": cfg, "nexec": nexec, "protection": protection}
        cols, rows, note = reports.build(name, ctx, start, end, None if g.p.can("report.read", "*") else vis)
        fmt = request.args.get("format", "json")
        stamp = time.strftime("%Y%m%d")
        if fmt == "csv":
            return Response(reports.to_csv(cols, rows), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=katam-{name}-{stamp}.csv"})
        if fmt == "html":
            return Response(reports.to_html(f"{name.title()} report", cols, rows, note, start, end),
                            mimetype="text/html")
        return jsonify(columns=cols, rows=rows, note=note)

    # ---- Kubernetes CSI ----------------------------------------------------
    @app.get("/api/v1/kubernetes/ceph-csi")
    def ceph_csi():
        if not visible("vm.write"):
            raise Forbidden("No access")
        st = cfg["storage"]
        if st["type"] != "rbd":
            raise BadRequest("Kubernetes CSI needs Ceph (storage.type: rbd)")
        k = st["rbd"].get("csi") or {}
        if not (st["rbd"].get("fsid") and k.get("user") and k.get("key")):
            raise BadRequest("Set storage.rbd.fsid and storage.rbd.csi.{user,key,pool} in config.yaml "
                             "(ceph-bootstrap.sh prints them)")
        values = {
            "csiConfig": [{"clusterID": st["rbd"]["fsid"],
                           "monitors": [f"{m}:6789" for m in st["rbd"]["monitors"]]}],
            "secret": {"create": True, "name": "csi-rbd-secret",
                       "userID": k["user"], "userKey": k["key"]},
            "storageClass": {"create": True, "name": "katam-rbd", "clusterID": st["rbd"]["fsid"],
                             "pool": k.get("pool", "kubernetes"), "reclaimPolicy": "Delete",
                             "allowVolumeExpansion": True, "annotations": {
                                 "storageclass.kubernetes.io/is-default-class": "true"}},
        }
        text = ("# helm repo add ceph-csi https://ceph.github.io/csi-charts\n"
                "# helm install ceph-csi-rbd ceph-csi/ceph-csi-rbd -n ceph-csi-rbd --create-namespace -f values.yaml\n"
                + yaml.safe_dump(values, sort_keys=False))
        db.audit(actor(), "csi.values", "kubernetes")
        return Response(text, mimetype="text/yaml",
                        headers={"Content-Disposition": "attachment; filename=katam-ceph-csi-values.yaml"})

    # ---- IaC: declarative apply ---------------------------------------------
    @app.post("/api/v1/apply")
    def apply_stack():
        """Make the platform match a stack: creates missing VMs, templates and security
        groups, updates security-group membership. Never deletes unless prune: true."""
        raw = request.get_data(as_text=True)
        stack = yaml.safe_load(raw) or {}
        dry = request.args.get("dry_run") in ("1", "true")
        project = stack.get("project", "default")
        plan, actions = [], []
        for sg in stack.get("security_groups", []):
            plan.append({"op": "upsert", "kind": "security_group", "name": sg["name"]})
            actions.append(lambda sg=sg: sdn.save_group(sg["name"], project, sg.get("description"), sg.get("rules", []))
                           if sdn else (_ for _ in ()).throw(BadRequest("SDN is not enabled")))
        for t in stack.get("templates", []):
            plan.append({"op": "upsert", "kind": "template", "name": t["name"]})
            def save_t(t=t):
                need("template.write", project)
                row = dict(label=t.get("label", t["name"]), project=project, spec=json.dumps(t["spec"]))
                if db.one(select(templates).where(templates.c.name == t["name"])):
                    db.run(update(templates).where(templates.c.name == t["name"]).values(**row))
                else:
                    db.run(insert(templates).values(name=t["name"], created=time.time(), **row))
            actions.append(save_t)
        existing = {v["name"]: v for v in cluster.list_vms()}
        wanted = set()
        task_ids = []
        for vm in stack.get("vms", []):
            vm = {"project": project, **vm}
            wanted.add(vm["name"])
            if vm["name"] in existing:
                cur = existing[vm["name"]]
                if "secgroups" in vm and sorted(vm["secgroups"]) != sorted(cur["secgroups"]):
                    plan.append({"op": "update", "kind": "vm", "name": vm["name"], "field": "secgroups"})
                    actions.append(lambda vm=vm: (sdn.set_vm_groups(vm["name"], vm["secgroups"]),
                                                  cluster.set_meta(vm["name"], secgroups=",".join(vm["secgroups"]))))
                else:
                    plan.append({"op": "unchanged", "kind": "vm", "name": vm["name"]})
                continue
            req = parse_create(vm)  # validates and checks permission + quota now
            plan.append({"op": "create", "kind": "vm", "name": vm["name"]})
            actions.append(lambda req=req: task_ids.append(
                tasks.submit("vm.create", req["name"], lambda p: cluster.create_vm(req, p), actor(), req["project"])))
        if stack.get("prune"):
            for name, v in existing.items():
                if v["project"] == project and name not in wanted:
                    need("vm.write", project)
                    plan.append({"op": "delete", "kind": "vm", "name": name})
                    actions.append(lambda name=name: task_ids.append(
                        tasks.submit("vm.delete", name, lambda p: cluster.delete_vm(name, p), actor(), project)))
        if not dry:
            for a in actions:
                a()
            db.audit(actor(), "stack.apply", project, detail=plan)
        return jsonify(dry_run=dry, plan=plan, tasks=task_ids)

    # ---- protection: snapshots, policies, vault --------------------------------
    @app.get("/api/v1/vms/<name>/snapshots")
    def snap_list(name):
        """Snapshots of one VM."""
        v = vm_or_404(name)
        need("vm.read", v["project"])
        return jsonify(snapshots=protection.list(vm=name), vault=protection.vault_list(vm=name))

    @app.post("/api/v1/vms/<name>/snapshots")
    def snap_create(name):
        """Take a snapshot now; optionally lock it and copy it to the vault."""
        v = vm_or_404(name)
        need("vm.write", v["project"])
        b = request.get_json(force=True) or {}
        who = actor()

        def go(p):
            row = protection.create(name, b.get("name"), who, "manual", None, b.get("lock_days", 0), p)
            if b.get("vault"):
                p(protection.vault_copy(row["id"], b.get("vault_days"), p))
            return f"Snapshot {row['id']} of {name} taken"
        tid = tasks.submit("snapshot.create", name, go, who, v["project"])
        return jsonify(task=tid), 202

    @app.delete("/api/v1/snapshots/<sid>")
    def snap_delete(sid):
        s = protection.get(sid)
        need("vm.write", s["project"])
        protection.delete(sid, actor())
        return jsonify(ok=True)

    @app.post("/api/v1/snapshots/<sid>/restore")
    def snap_restore(sid):
        """Roll a VM back to a snapshot (discards later changes)."""
        s = protection.get(sid)
        need("vm.write", s["project"])
        if (request.get_json(force=True) or {}).get("confirm") != s["vm"]:
            raise BadRequest(f"Restoring discards changes made since the snapshot. Confirm with confirm: \"{s['vm']}\"")
        who = actor()
        tid = tasks.submit("snapshot.restore", s["vm"], lambda p: protection.restore(sid, who, p), who, s["project"])
        return jsonify(task=tid), 202

    @app.post("/api/v1/snapshots/<sid>/vault")
    def snap_vault(sid):
        """Copy a snapshot to the immutable vault."""
        s = protection.get(sid)
        need("vm.write", s["project"])
        days = (request.get_json(force=True) or {}).get("days")
        tid = tasks.submit("vault.copy", f"vault:{sid}", lambda p: protection.vault_copy(sid, days, p), actor(), s["project"])
        return jsonify(task=tid), 202

    @app.get("/api/v1/protection/policies")
    def pol_list():
        if not visible("vm.read"):
            raise Forbidden("No access")
        return jsonify(protection.policies())

    @app.put("/api/v1/protection/policies/<name>")
    def pol_put(name):
        """Create or change a snapshot schedule."""
        need("iam.admin")
        if not NAME_RE.match(name):
            raise BadRequest("Policy names: lowercase letters, digits, hyphens")
        protection.save_policy(name, request.get_json(force=True) or {})
        db.audit(actor(), "policy.save", name)
        return jsonify(ok=True)

    @app.delete("/api/v1/protection/policies/<name>")
    def pol_delete(name):
        need("iam.admin")
        db.run(delete(protection_policies).where(protection_policies.c.name == name))
        db.audit(actor(), "policy.delete", name)
        return jsonify(ok=True)

    @app.get("/api/v1/protection/vault")
    def vault_info():
        vis = visible("vm.read")
        if not vis:
            raise Forbidden("No access")
        return jsonify(status=protection.vault_status(),
                       copies=protection.vault_list(projects=None if g.p.can("vm.read", "*") else vis))

    @app.post("/api/v1/protection/vault/<cid>/restore")
    def vault_restore(cid):
        """Bring a vault copy back as an image."""
        need("iam.admin")
        img = ((request.get_json(force=True) or {}).get("image") or "").lower()
        if not NAME_RE.match(img):
            raise BadRequest("Give the restored image a name")
        tid = tasks.submit("vault.restore", f"vault-restore:{cid}", lambda p: protection.vault_restore(cid, img, p), actor())
        return jsonify(task=tid), 202

    @app.get("/api/v1/protection/coverage")
    def coverage():
        vis = visible("vm.read")
        return jsonify(protection.coverage(None if g.p.can("vm.read", "*") else vis))

    # ---- updates ------------------------------------------------------------
    @app.get("/api/v1/updates")
    def updates_check():
        """Compare this version with the release feed."""
        need("iam.admin")
        return jsonify(updates.check(force=request.args.get("refresh") == "1"))

    @app.post("/api/v1/updates/rollout")
    def updates_rollout():
        """Upgrade nodes one at a time (maintenance, apt upgrade, optional reboot)."""
        need("iam.admin")
        b = request.get_json(force=True) or {}
        tid = tasks.submit("updates.rollout", "cluster:updates",
                           lambda p: updates.rollout(p, include_os=bool(b.get("include_os")), reboot=bool(b.get("reboot"))),
                           actor())
        return jsonify(task=tid), 202

    # ---- OpenAPI (generated from the routes) --------------------------------
    @app.get("/api/v1/openapi.json")
    def openapi():
        paths = {}
        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith("/api/v1/"):
                continue
            path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule.rule)
            fn = app.view_functions[rule.endpoint]
            for m in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                op = {"operationId": rule.endpoint, "summary": (fn.__doc__ or rule.endpoint).strip().split("\n")[0],
                      "responses": {"200": {"description": "OK"}}}
                params = re.findall(r"{([^}]+)}", path)
                if params:
                    op["parameters"] = [{"name": p_, "in": "path", "required": True, "schema": {"type": "string"}}
                                        for p_ in params]
                paths.setdefault(path, {})[m.lower()] = op
        return jsonify({"openapi": "3.0.3",
                        "info": {"title": "Katam Virtualization API", "version": __version__},
                        "components": {"securitySchemes": {"token": {"type": "http", "scheme": "bearer"}}},
                        "security": [{"token": []}], "paths": paths})

    return app, cfg


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app, cfg = create_app()
    from waitress import serve
    serve(app, host=cfg["controller"]["listen"], port=cfg["controller"]["port"], threads=24,
          max_request_body_size=200 * 2**30)
