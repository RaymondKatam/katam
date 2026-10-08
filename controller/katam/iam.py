"""Users, groups, roles, API tokens, and external identity (Active Directory/LDAP, SAML)."""
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field

from werkzeug.security import check_password_hash, generate_password_hash

from .db import (bindings, delete, group_members, insert, projects, select, tokens, update,
                 users)

log = logging.getLogger("katam.iam")

ROLES = {
    "admin": {"*"},
    "operator": {"vm.read", "vm.write", "vm.power", "vm.console", "vm.migrate", "vm.export",
                 "image.read", "image.write", "template.read", "template.write",
                 "network.read", "secgroup.read", "secgroup.write", "task.read",
                 "report.read", "finops.read", "runbook.read", "runbook.run", "dr.read"},
    "viewer": {"vm.read", "image.read", "template.read", "network.read", "secgroup.read",
               "task.read", "report.read", "finops.read", "runbook.read", "dr.read"},
    "auditor": {"vm.read", "image.read", "template.read", "network.read", "secgroup.read",
                "task.read", "report.read", "finops.read", "audit.read", "runbook.read",
                "dr.read"},
}


class AuthError(Exception):
    pass


class Forbidden(Exception):
    pass


@dataclass
class Principal:
    name: str
    source: str
    groups: list = field(default_factory=list)
    grants: dict = field(default_factory=dict)   # project ("*" = all) -> set(perms)
    via_token: bool = False

    def can(self, perm, project=None):
        for scope in ("*", project):
            if scope is None:
                continue
            perms = self.grants.get(scope, set())
            if "*" in perms or perm in perms:
                return True
        return False

    def projects_with(self, perm, all_projects):
        if self.can(perm, "*"):
            return set(all_projects)
        return {p for p in all_projects if self.can(perm, p)}

    @property
    def is_admin(self):
        return "*" in self.grants.get("*", set())


class IAM:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg

    # ---- bootstrap ---------------------------------------------------
    def bootstrap(self):
        b = self.cfg["controller"].get("bootstrap_admin") or {}
        if not self.db.one(select(projects).where(projects.c.name == "default")):
            self.db.run(insert(projects).values(name="default", description="Default project",
                                                quota="{}", created=time.time()))
        if b.get("user") and b.get("password"):
            if not self.db.one(select(users).where(users.c.name == b["user"])):
                self.create_user(b["user"], b["password"], display="Bootstrap admin")
                self.bind(f"user:{b['user']}", "admin", "*")
                log.info("created bootstrap admin %s", b["user"])

    # ---- users and bindings -----------------------------------------
    def create_user(self, name, password, display=None, email=None, source="local"):
        self.db.run(insert(users).values(
            name=name, display=display or name, email=email, source=source,
            password_hash=generate_password_hash(password) if password else None,
            disabled=False, created=time.time()))

    def set_password(self, name, password):
        self.db.run(update(users).where(users.c.name == name)
                    .values(password_hash=generate_password_hash(password)))

    def bind(self, subject, role, project):
        if role not in ROLES:
            raise ValueError(f"Unknown role {role}; choose from {', '.join(ROLES)}")
        if not (subject.startswith("user:") or subject.startswith("group:")):
            raise ValueError("Subject must start with user: or group:")
        existing = self.db.one(select(bindings).where(
            (bindings.c.subject == subject) & (bindings.c.role == role) & (bindings.c.project == project)))
        if not existing:
            self.db.run(insert(bindings).values(subject=subject, role=role, project=project))

    def unbind(self, binding_id):
        self.db.run(delete(bindings).where(bindings.c.id == binding_id))

    def local_groups(self, user):
        return [r["group_name"] for r in
                self.db.all(select(group_members).where(group_members.c.user_name == user))]

    def principal(self, name, source, external_groups=(), via_token=False):
        groups = sorted(set(self.local_groups(name)) | set(external_groups))
        subjects = [f"user:{name}"] + [f"group:{g}" for g in groups]
        grants = {}
        for b in self.db.all(select(bindings).where(bindings.c.subject.in_(subjects))):
            grants.setdefault(b["project"], set()).update(ROLES[b["role"]])
        return Principal(name=name, source=source, groups=groups, grants=grants, via_token=via_token)

    # ---- password login (local, then directory) ---------------------
    def login(self, username, password):
        username = (username or "").strip()
        if not username or not password:
            raise AuthError("Enter a username and password")
        u = self.db.one(select(users).where(users.c.name == username))
        if u and u["disabled"]:
            raise AuthError("This account is disabled")
        if u and u["source"] == "local" and self.cfg["auth"].get("local", True):
            if u["password_hash"] and check_password_hash(u["password_hash"], password):
                return self.principal(username, "local")
            raise AuthError("Wrong username or password")
        if self.cfg["auth"]["ldap"].get("enabled"):
            info = ldap_authenticate(self.cfg["auth"]["ldap"], username, password)
            self._upsert_external(info["username"], "ldap", info.get("display"), info.get("email"))
            return self.principal(info["username"], "ldap", info["groups"])
        raise AuthError("Wrong username or password")

    def _upsert_external(self, name, source, display=None, email=None):
        u = self.db.one(select(users).where(users.c.name == name))
        if not u:
            self.db.run(insert(users).values(name=name, display=display or name, email=email,
                                             source=source, disabled=False, created=time.time()))
        elif u["disabled"]:
            raise AuthError("This account is disabled in Katam")

    def saml_principal(self, name, groups, display=None, email=None):
        self._upsert_external(name, "saml", display, email)
        return self.principal(name, "saml", groups)

    # ---- API tokens --------------------------------------------------
    @staticmethod
    def _hash(secret):
        return hashlib.sha256(secret.encode()).hexdigest()

    def create_token(self, user, name, days=None):
        tid = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        self.db.run(insert(tokens).values(
            id=tid, user_name=user, name=name, hash=self._hash(secret),
            expires=time.time() + days * 86400 if days else None, created=time.time()))
        return f"katam_{tid}_{secret}"

    def token_principal(self, raw):
        try:
            _, tid, secret = raw.split("_", 2)
        except ValueError:
            raise AuthError("Malformed API token")
        t = self.db.one(select(tokens).where(tokens.c.id == tid))
        if not t or not hmac.compare_digest(t["hash"], self._hash(secret)):
            raise AuthError("Invalid API token")
        if t["expires"] and t["expires"] < time.time():
            raise AuthError("API token expired")
        u = self.db.one(select(users).where(users.c.name == t["user_name"]))
        if not u or u["disabled"]:
            raise AuthError("Token owner is disabled")
        self.db.run(update(tokens).where(tokens.c.id == tid).values(last_used=time.time()))
        # Directory group membership is refreshed at each interactive login; tokens use local
        # groups plus direct user bindings.
        return self.principal(u["name"], u["source"], via_token=True)


# ---- Active Directory / LDAP -----------------------------------------
def ldap_authenticate(lc, username, password):
    """Bind as a service account, find the user, bind as the user, return groups (CNs)."""
    import ssl

    from ldap3 import ALL, SUBTREE, Connection, Server, Tls
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    tls = None
    if lc["url"].startswith("ldaps://") or lc.get("start_tls"):
        tls = Tls(validate=ssl.CERT_REQUIRED if lc.get("verify_tls", True) else ssl.CERT_NONE,
                  ca_certs_file=lc.get("ca_file"))
    server = Server(lc["url"], get_info=ALL, tls=tls, connect_timeout=8)
    try:
        svc = Connection(server, user=lc["bind_dn"], password=lc["bind_password"], auto_bind=False)
        if lc.get("start_tls"):
            svc.open(); svc.start_tls()
        if not svc.bind():
            raise AuthError("Directory service account bind failed; check auth.ldap.bind_dn")
        flt = lc.get("user_filter", "(&(objectClass=user)(sAMAccountName={username}))")
        svc.search(lc["base_dn"], flt.format(username=escape_filter_chars(username)),
                   SUBTREE, attributes=["distinguishedName", "displayName", "mail",
                                        lc.get("group_attribute", "memberOf")])
        if len(svc.entries) != 1:
            raise AuthError("Wrong username or password")
        entry = svc.entries[0]
        user_dn = entry.entry_dn
        user_conn = Connection(server, user=user_dn, password=password, auto_bind=False)
        if lc.get("start_tls"):
            user_conn.open(); user_conn.start_tls()
        if not user_conn.bind():
            raise AuthError("Wrong username or password")
        user_conn.unbind()
        groups = []
        attr = lc.get("group_attribute", "memberOf")
        for dn in (entry[attr].values if attr in entry else []):
            first = dn.split(",", 1)[0]
            groups.append(first.split("=", 1)[1] if "=" in first else first)
        svc.unbind()
    except LDAPException as e:
        raise AuthError(f"Directory error: {e}")
    return {"username": username.lower(),
            "display": str(entry.displayName) if "displayName" in entry else username,
            "email": str(entry.mail) if "mail" in entry else None,
            "groups": groups}


# ---- SAML 2.0 ---------------------------------------------------------
def saml_settings(sc, public_url):
    base = public_url.rstrip("/")
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sc.get("sp_entity_id", f"{base}/auth/saml/metadata"),
            "assertionConsumerService": {
                "url": f"{base}/auth/saml/acs",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"},
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
        },
        "idp": {
            "entityId": sc["idp_entity_id"],
            "singleSignOnService": {"url": sc["idp_sso_url"],
                                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"},
            "x509cert": sc["idp_x509cert"],
        },
        "security": {"wantAssertionsSigned": True, "wantMessagesSigned": False},
    }


def saml_request_data(req):
    return {
        "https": "on" if req.scheme == "https" or req.headers.get("X-Forwarded-Proto") == "https" else "off",
        "http_host": req.headers.get("X-Forwarded-Host", req.host),
        "script_name": req.path,
        "get_data": req.args.copy(),
        "post_data": req.form.copy(),
    }
