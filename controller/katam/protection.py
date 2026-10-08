"""Data protection: VM snapshots, retention locks, schedules, and an immutable vault.

Three layers, from fastest to safest:
  snapshots    Ceph RBD snapshots (or qcow2 internal snapshots in local-disk mode). Instant,
               quiesced through qemu-guest-agent when it is running in the guest.
  locks        A locked snapshot can't be deleted through Katam before its lock expires, by
               anyone, and a VM with locked snapshots can't be deleted. On Ceph the snapshot is
               also protected (rbd snap protect). Someone with root on a node can still remove
               it with rbd commands, so locks stop mistakes and compromised Katam accounts.
  vault        Copies of snapshots written to S3-compatible storage with Object Lock in
               COMPLIANCE mode. Until the retention date nobody can delete or overwrite them,
               including the storage administrator and root on any Katam machine. Keep the
               bucket outside the cluster's administrative domain.
"""
import datetime as dt
import json
import logging
import os
import posixpath
import secrets
import threading
import time

import libvirt

from .db import delete, insert, protection_policies, select, snapshots, update, vault_copies, vm_backups
from .driver import ClusterError

log = logging.getLogger("katam.protection")


class Locked(ClusterError):
    pass


class Protection:
    def __init__(self, cfg, db, cluster, nexec, tasks, images=None):
        self.cfg, self.db, self.cluster, self.x, self.tasks, self.images = cfg, db, cluster, nexec, tasks, images
        self.p = cfg["protection"]
        self.rbd = cfg["storage"]["type"] == "rbd"

    # ---- snapshots ------------------------------------------------------
    def _lock_ts(self, days):
        days = float(days or 0)
        if days < 0 or days > float(self.p["max_lock_days"]):
            raise ClusterError(f"Lock must be between 0 and {self.p['max_lock_days']} days")
        return time.time() + days * 86400 if days else None

    def create(self, vm, name=None, by="system", kind="manual", policy=None, lock_days=0, progress=lambda m: None):
        node, d = self.cluster._must(vm)
        meta = self.cluster._meta(d)
        _, disks = self.cluster.disk_sources(vm)
        if not disks:
            raise ClusterError(f"{vm} has no disks to snapshot")
        sid = secrets.token_hex(6)
        snap = f"katam-{sid}"
        locked_until = self._lock_ts(lock_days)
        frozen = False
        if d.isActive():
            try:
                d.fsFreeze()
                frozen = True
                progress("Guest filesystems frozen through the guest agent")
            except libvirt.libvirtError:
                progress("No guest agent response; taking a crash-consistent snapshot")
        try:
            if self.rbd:
                for kind_, src in disks:
                    if kind_ != "rbd":
                        continue
                    self.x.run(node, ["rbd", "snap", "create", f"{src}@{snap}"], sudo=True)
            else:
                xml = (f"<domainsnapshot><name>{snap}</name>"
                       f"<description>Katam snapshot {name or ''}</description></domainsnapshot>")
                flags = libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
                d.snapshotCreateXML(xml, flags)
        finally:
            if frozen:
                try:
                    d.fsThaw()
                except libvirt.libvirtError as e:
                    log.error("thaw of %s failed: %s", vm, e)
        if self.rbd and locked_until:
            for kind_, src in disks:
                if kind_ == "rbd":
                    self.x.run(node, ["rbd", "snap", "protect", f"{src}@{snap}"], sudo=True, check=False)
        row = dict(id=sid, vm=vm, project=meta.get("project") or "default",
                   name=name or time.strftime("%Y-%m-%d %H:%M"), kind=kind, policy=policy,
                   disks=json.dumps(disks), consistent=frozen, locked_until=locked_until,
                   created=time.time(), created_by=by, status="ok")
        self.db.run(insert(snapshots).values(**row))
        self.db.audit(by, "snapshot.create", vm, "ok", {"id": sid, "locked_until": locked_until})
        progress(f"Snapshot {sid} of {vm}" + (" (locked)" if locked_until else ""))
        return row

    def get(self, sid):
        r = self.db.one(select(snapshots).where(snapshots.c.id == sid))
        if not r:
            raise ClusterError(f"No snapshot {sid}")
        return r

    def list(self, vm=None, projects=None):
        q = select(snapshots).order_by(snapshots.c.created.desc())
        if vm:
            q = q.where(snapshots.c.vm == vm)
        rows = self.db.all(q)
        if projects is not None:
            rows = [r for r in rows if r["project"] in projects]
        now = time.time()
        for r in rows:
            r["disks"] = json.loads(r["disks"] or "[]")
            r["locked"] = bool(r["locked_until"] and r["locked_until"] > now)
        return rows

    def delete(self, sid, by="system"):
        r = self.get(sid)
        if r["locked_until"] and r["locked_until"] > time.time():
            until = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["locked_until"]))
            self.db.audit(by, "snapshot.delete", r["vm"], "denied", f"locked until {until}")
            raise Locked(f"Snapshot {sid} is locked until {until} and can't be deleted")
        snap = f"katam-{sid}"
        hit = self.cluster.find(r["vm"])
        if self.rbd:
            node = hit[0] if hit else self.cfg["storage"]["import_node"]
            for kind_, src in json.loads(r["disks"]):
                if kind_ == "rbd":
                    self.x.run(node, ["rbd", "snap", "unprotect", f"{src}@{snap}"], sudo=True, check=False)
                    self.x.run(node, ["rbd", "snap", "rm", f"{src}@{snap}"], sudo=True, check=False)
        elif hit:
            try:
                hit[1].snapshotLookupByName(snap).delete(0)
            except libvirt.libvirtError as e:
                log.warning("libvirt snapshot %s already gone: %s", snap, e)
        self.db.run(delete(snapshots).where(snapshots.c.id == sid))
        self.db.audit(by, "snapshot.delete", r["vm"], "ok", sid)

    def restore(self, sid, by="system", progress=lambda m: None):
        r = self.get(sid)
        node, d = self.cluster._must(r["vm"])
        snap = f"katam-{sid}"
        if self.rbd:
            if d.isActive():
                raise ClusterError("Shut the VM down before restoring a snapshot")
            for kind_, src in json.loads(r["disks"]):
                if kind_ == "rbd":
                    progress(f"Rolling {src} back to {r['name']}")
                    self.x.run(node, ["rbd", "snap", "rollback", f"{src}@{snap}"], sudo=True, timeout=6 * 3600)
        else:
            d.revertToSnapshot(d.snapshotLookupByName(snap), 0)
        self.db.audit(by, "snapshot.restore", r["vm"], "ok", sid)
        return f"{r['vm']} restored to snapshot {r['name']}"

    def guard_vm_delete(self, vm, by):
        """Refuse to delete a VM that has locked snapshots; remove its unlocked ones."""
        rows = self.list(vm=vm)
        locked = [r for r in rows if r["locked"]]
        if locked:
            last = max(r["locked_until"] for r in locked)
            raise Locked(f"{vm} has {len(locked)} locked snapshot(s) until "
                         f"{time.strftime('%Y-%m-%d', time.localtime(last))}; it can't be deleted before then")
        for r in rows:
            self.delete(r["id"], by)

    # ---- policies --------------------------------------------------------
    def save_policy(self, name, spec):
        if float(spec.get("interval_h", 0)) < 0.25:
            raise ClusterError("interval_h must be at least 0.25 (15 minutes)")
        if int(spec.get("keep", 0)) < 1:
            raise ClusterError("keep must be at least 1")
        self._lock_ts(spec.get("lock_days", 0))
        if spec.get("vault") and not self.p["vault"].get("enabled"):
            raise ClusterError("The vault isn't configured (protection.vault in config.yaml)")
        if not spec.get("project") and not spec.get("vms"):
            raise ClusterError("A policy needs a project or a list of vms")
        row = dict(project=spec.get("project"), vms=json.dumps(spec.get("vms") or []),
                   interval_h=float(spec["interval_h"]), keep=int(spec["keep"]),
                   lock_days=float(spec.get("lock_days") or 0), vault=bool(spec.get("vault")),
                   vault_days=float(spec.get("vault_days") or 30), enabled=bool(spec.get("enabled", True)))
        if self.db.one(select(protection_policies).where(protection_policies.c.name == name)):
            self.db.run(update(protection_policies).where(protection_policies.c.name == name).values(**row))
        else:
            self.db.run(insert(protection_policies).values(name=name, **row))

    def policies(self):
        out = self.db.all(select(protection_policies))
        for p in out:
            p["vms"] = json.loads(p["vms"] or "[]")
        return out

    def _targets(self, pol):
        vms = self.cluster.list_vms()
        names = set(pol["vms"])
        return [v["name"] for v in vms if v["name"] in names or (pol["project"] and v["project"] == pol["project"])]

    def tick(self, now=None):
        now = now or time.time()
        ran = []
        for pol in self.policies():
            if not pol["enabled"] or (pol["last_run"] and now - pol["last_run"] < pol["interval_h"] * 3600 - 30):
                continue
            self.db.run(update(protection_policies).where(protection_policies.c.name == pol["name"]).values(last_run=now))
            for vm in self._targets(pol):
                try:
                    row = self.create(vm, by=f"policy:{pol['name']}", kind="scheduled", policy=pol["name"],
                                      lock_days=pol["lock_days"])
                    ran.append(row["id"])
                    if pol["vault"]:
                        self.tasks.submit("vault.copy", f"vault:{row['id']}",
                                          lambda p, sid=row["id"], days=pol["vault_days"]: self.vault_copy(sid, days, p),
                                          actor=f"policy:{pol['name']}", project=row["project"])
                except Exception as e:  # noqa: BLE001 - one VM's failure mustn't stop the rest
                    log.error("policy %s: snapshot of %s failed: %s", pol["name"], vm, e)
                    self.db.audit(f"policy:{pol['name']}", "snapshot.create", vm, "failed", str(e))
                self._prune(pol, vm)
        return ran

    def _prune(self, pol, vm):
        rows = [r for r in self.list(vm=vm) if r["policy"] == pol["name"] and r["kind"] == "scheduled"]
        for r in rows[pol["keep"]:]:
            if r["locked"]:
                continue  # expires later; pruned on a future run
            try:
                self.delete(r["id"], by=f"policy:{pol['name']}")
            except Exception as e:  # noqa: BLE001
                log.warning("prune of %s failed: %s", r["id"], e)

    def start(self):
        def loop():
            while True:
                time.sleep(float(self.p["scheduler_interval_s"]))
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("protection scheduler failed")
        threading.Thread(target=loop, name="katam-protection", daemon=True).start()

    # ---- immutable vault ---------------------------------------------------
    def _s3(self):
        v = self.p["vault"]
        if not v.get("enabled"):
            raise ClusterError("The vault isn't configured (protection.vault in config.yaml)")
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=v.get("endpoint"), region_name=v.get("region", "us-east-1"),
                            aws_access_key_id=v["access_key"], aws_secret_access_key=v["secret_key"],
                            verify=v.get("verify_tls", True),
                            config=Config(s3={"addressing_style": v.get("addressing_style", "path")}))

    def vault_status(self):
        v = self.p["vault"]
        if not v.get("enabled"):
            return {"enabled": False}
        s3 = self._s3()
        try:
            conf = s3.get_object_lock_configuration(Bucket=v["bucket"])["ObjectLockConfiguration"]
            ok = conf.get("ObjectLockEnabled") == "Enabled"
        except Exception as e:  # noqa: BLE001
            return {"enabled": True, "bucket": v["bucket"], "object_lock": False, "error": str(e)}
        return {"enabled": True, "bucket": v["bucket"], "object_lock": ok,
                "error": None if ok else "Object Lock is not enabled on this bucket; copies would not be immutable"}

    def vault_copy(self, sid, days=None, progress=lambda m: None):
        v = self.p["vault"]
        st = self.vault_status()
        if not st.get("object_lock"):
            raise ClusterError(st.get("error") or "Vault bucket has no Object Lock")
        r = self.get(sid)
        days = float(days or v.get("retention_days", 30))
        retain = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
        hit = self.cluster.find(r["vm"])
        node = hit[0] if hit else self.cfg["storage"]["import_node"]
        cid = secrets.token_hex(6)
        self.db.run(insert(vault_copies).values(id=cid, snapshot_id=sid, vm=r["vm"], project=r["project"],
                                                objects="[]", retain_until=retain.timestamp(),
                                                created=time.time(), status="copying"))
        s3 = self._s3()
        prefix = f"{v.get('prefix', 'katam/')}{r['vm']}/{sid}/"
        lock = {"ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": retain, "ChecksumAlgorithm": "SHA256"}
        objects = []
        work = posixpath.join(self.cfg["storage"]["node_staging"], f"vault-{cid}")
        local_dir = os.path.join(self.cfg["controller"]["staging_dir"], f"vault-{cid}")
        os.makedirs(local_dir, exist_ok=True)
        try:
            self.x.run(node, ["mkdir", "-p", work], sudo=True)
            snap = f"katam-{sid}"
            for i, (kind_, src) in enumerate(json.loads(r["disks"])):
                out = posixpath.join(work, f"disk{i}.qcow2")
                progress(f"Compressing disk {i} from the snapshot")
                if kind_ == "rbd":
                    self.x.run(node, ["qemu-img", "convert", "-c", "-O", "qcow2", f"rbd:{src}@{snap}", out],
                               sudo=True, timeout=12 * 3600)
                else:
                    self.x.run(node, ["qemu-img", "convert", "-c", "-O", "qcow2", "-l", f"snapshot.name={snap}",
                                      src, out], sudo=True, timeout=12 * 3600)
                self.x.run(node, ["chmod", "644", out], sudo=True)
                local = os.path.join(local_dir, f"disk{i}.qcow2")
                self.x.pull(node, out, local)
                key = f"{prefix}disk{i}.qcow2"
                progress(f"Uploading disk {i} to the vault (locked until {retain:%Y-%m-%d})")
                s3.upload_file(local, v["bucket"], key, ExtraArgs=lock)
                head = s3.head_object(Bucket=v["bucket"], Key=key)
                objects.append({"key": key, "version": head.get("VersionId"), "size": head["ContentLength"]})
                os.remove(local)
            backup = self.db.one(select(vm_backups).where(vm_backups.c.vm == r["vm"]))
            meta = json.dumps({"vm": r["vm"], "project": r["project"], "snapshot": r["name"], "created": r["created"],
                               "consistent": r["consistent"], "xml": backup["xml"] if backup else None}).encode()
            s3.put_object(Bucket=v["bucket"], Key=f"{prefix}vm.json", Body=meta, **lock)
            self.db.run(update(vault_copies).where(vault_copies.c.id == cid).values(objects=json.dumps(objects), status="ok"))
            self.db.audit("system", "vault.copy", r["vm"], "ok", {"copy": cid, "retain_until": retain.isoformat()})
            return f"Vault copy {cid} of {r['vm']}: {len(objects)} disk(s), immutable until {retain:%Y-%m-%d}"
        except Exception:
            self.db.run(update(vault_copies).where(vault_copies.c.id == cid).values(status="failed"))
            raise
        finally:
            self.x.run(node, ["rm", "-rf", work], sudo=True, check=False)
            for f in os.listdir(local_dir):
                os.remove(os.path.join(local_dir, f))
            os.rmdir(local_dir)

    def vault_list(self, vm=None, projects=None):
        q = select(vault_copies).order_by(vault_copies.c.created.desc())
        if vm:
            q = q.where(vault_copies.c.vm == vm)
        rows = self.db.all(q)
        if projects is not None:
            rows = [r for r in rows if r["project"] in projects]
        for r in rows:
            r["objects"] = json.loads(r["objects"] or "[]")
        return rows

    def vault_restore(self, cid, image_name, progress=lambda m: None):
        """Bring a vault copy back as image(s); create a VM from it afterwards."""
        if not self.images:
            raise ClusterError("Image service unavailable")
        c = self.db.one(select(vault_copies).where(vault_copies.c.id == cid))
        if not c or c["status"] != "ok":
            raise ClusterError(f"No completed vault copy {cid}")
        v = self.p["vault"]
        s3 = self._s3()
        made = []
        for i, o in enumerate(json.loads(c["objects"])):
            name = image_name if i == 0 else f"{image_name}-d{i}"
            local = os.path.join(self.cfg["controller"]["staging_dir"], f"restore-{cid}-{i}.qcow2")
            progress(f"Downloading {o['key']}")
            extra = {"VersionId": o["version"]} if o.get("version") else None
            s3.download_file(v["bucket"], o["key"], local, ExtraArgs=extra)
            self.images.begin(name, c["project"], f"Restored {c['vm']} ({cid})")
            self.images.import_image(name, {"upload": local}, progress, cloud_init=False)
            made.append(name)
        return f"Restored {c['vm']} from the vault as image(s): {', '.join(made)}. Create a VM from it to finish."

    def coverage(self, projects=None):
        now = time.time()
        snaps = self.list(projects=projects)
        vault = [c for c in self.vault_list(projects=projects) if c["status"] == "ok"]
        pols = self.policies()
        out = []
        for vm in self.cluster.list_vms():
            if projects is not None and vm["project"] not in projects:
                continue
            mine = [s for s in snaps if s["vm"] == vm["name"]]
            vcopies = [c for c in vault if c["vm"] == vm["name"]]
            covered_by = [p["name"] for p in pols if p["enabled"] and vm["name"] in self._targets(p)]
            last = max((s["created"] for s in mine), default=None)
            out.append({"vm": vm["name"], "project": vm["project"], "policies": " ".join(covered_by),
                        "snapshots": len(mine), "locked": sum(1 for s in mine if s["locked"]),
                        "last_snapshot_h": round((now - last) / 3600, 1) if last else None,
                        "vault_copies": len(vcopies),
                        "vault_until": time.strftime("%Y-%m-%d", time.localtime(max(c["retain_until"] for c in vcopies))) if vcopies else None})
        return out
