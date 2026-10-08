"""DR orchestration.

Two protection modes, set per DR site in config.yaml:
  stretch      one Ceph cluster across both sites (synchronous, RPO 0). Failover restarts VMs
               from their saved definitions on DR-site nodes.
  rbd-mirror   a separate Ceph cluster at the DR site fed by snapshot mirroring (RPO = schedule).
               Failover promotes the mirrored images, then defines and starts the VMs there.

A plan lists VMs in boot order with optional delays and maps primary networks to DR networks.
Modes: test (clones in an isolated network, production untouched), planned (demote then
promote: no data loss), disaster (force promote).
"""
import json
import time
import xml.etree.ElementTree as ET

from .db import dr_plans, insert, select, update, vm_backups
from .driver import ClusterError


class DR:
    def __init__(self, cfg, db, cluster, nexec):
        self.cfg, self.db, self.cluster, self.x = cfg, db, cluster, nexec

    def site(self, name):
        s = self.cfg["dr"]["sites"].get(name)
        if not s:
            raise ClusterError(f"No DR site '{name}' in config.yaml")
        return s

    # ---- plans ------------------------------------------------------------
    def save_plan(self, name, spec):
        site = self.site(spec.get("site", "dr"))
        vms = spec.get("vms") or []
        if not vms:
            raise ClusterError("A plan needs at least one VM")
        for v in vms:
            if not self.db.one(select(vm_backups).where(vm_backups.c.vm == v["name"])):
                raise ClusterError(f"No saved definition for {v['name']} yet; wait for the next metering pass")
        if site["mode"] == "rbd-mirror":
            self._enable_mirroring([v["name"] for v in vms], site)
        row = {"spec": json.dumps(spec)}
        if self.db.one(select(dr_plans).where(dr_plans.c.name == name)):
            self.db.run(update(dr_plans).where(dr_plans.c.name == name).values(**row))
        else:
            self.db.run(insert(dr_plans).values(name=name, **row))

    def _images_for(self, vm):
        xml = self.db.one(select(vm_backups).where(vm_backups.c.vm == vm))["xml"]
        out = []
        for disk in ET.fromstring(xml).findall("devices/disk[@device='disk']"):
            src = disk.find("source")
            if src is not None and src.get("protocol") == "rbd":
                out.append(src.get("name"))
        return out

    def _enable_mirroring(self, vms, site):
        node = self.cfg["storage"]["import_node"]
        interval = site.get("snapshot_interval", "5m")
        for vm in vms:
            for img in self._images_for(vm):
                self.x.run(node, ["rbd", "mirror", "image", "enable", img, "snapshot"], sudo=True, check=False)
        pool = self.cfg["storage"]["rbd"]["ceph_pool"]
        self.x.run(node, ["rbd", "mirror", "snapshot", "schedule", "add", "--pool", pool, interval],
                   sudo=True, check=False)

    # ---- run --------------------------------------------------------------
    def run(self, plan_name, mode, progress):
        if mode not in ("test", "planned", "disaster", "cleanup-test"):
            raise ClusterError("Mode must be test, planned, disaster or cleanup-test")
        plan = self.db.one(select(dr_plans).where(dr_plans.c.name == plan_name))
        if not plan:
            raise ClusterError(f"No DR plan named {plan_name}")
        spec = json.loads(plan["spec"])
        site = self.site(spec.get("site", "dr"))
        if mode == "cleanup-test":
            return self._cleanup_test(spec, site, progress)

        t0 = time.time()
        rpo = None
        started = []
        dr_nodes = [n["name"] for n in self.cfg["nodes"] if n["site"] == spec.get("site", "dr")]
        if not dr_nodes:
            raise ClusterError("No nodes with that DR site in config.yaml")
        suffix = "-drtest" if mode == "test" else ""

        if site["mode"] == "rbd-mirror":
            ceph_node = site["ceph_node"]
            if mode == "planned":
                progress("Planned migration: shutting down primaries and demoting images")
                for v in spec["vms"]:
                    try:
                        self.cluster.power(v["name"], "shutdown")
                    except ClusterError:
                        pass
                self._wait_stopped([v["name"] for v in spec["vms"]], 600, progress)
                for v in spec["vms"]:
                    for img in self._images_for(v["name"]):
                        self.x.run(self.cfg["storage"]["import_node"],
                                   ["rbd", "mirror", "image", "snapshot", img], sudo=True)
                        self.x.run(self.cfg["storage"]["import_node"],
                                   ["rbd", "mirror", "image", "demote", img], sudo=True)
                time.sleep(float(site.get("demote_settle_s", 30)))
            for v in spec["vms"]:
                for img in self._images_for(v["name"]):
                    if mode == "test":
                        snaps = json.loads(self.x.run(ceph_node, ["rbd", "snap", "ls", "--all", "--format", "json", img], sudo=True))
                        mirror = [s for s in snaps if s.get("namespace", {}).get("type") == "mirror"]
                        if not mirror:
                            raise ClusterError(f"No mirror snapshot of {img} at the DR site yet")
                        last = mirror[-1]
                        if last.get("timestamp"):
                            age = time.time() - time.mktime(time.strptime(last["timestamp"][:24], "%a %b %d %H:%M:%S %Y"))
                            rpo = max(rpo or 0, age)
                        progress(f"Cloning {img} from mirror snapshot {last['id']}")
                        self.x.run(ceph_node, ["rbd", "clone", "--rbd-default-clone-format", "2",
                                               "--snap-id", str(last["id"]), img, img + suffix], sudo=True)
                    else:
                        progress(f"Promoting {img}")
                        cmd = ["rbd", "mirror", "image", "promote", img]
                        if mode == "disaster":
                            cmd.insert(4, "--force")
                        self.x.run(ceph_node, cmd, sudo=True)
        elif mode == "test":
            raise ClusterError("Test failover needs rbd-mirror mode; in stretch mode use a planned run in a window")

        for v in sorted(spec["vms"], key=lambda v: v.get("order", 100)):
            row = self.db.one(select(vm_backups).where(vm_backups.c.vm == v["name"]))
            xml = self._rewrite(row["xml"], spec, site, mode, suffix)
            mem = int(ET.fromstring(xml).findtext("memory")) // 1024
            dest = self._place(dr_nodes, mem)
            progress(f"Starting {v['name']}{suffix} on {dest}")
            self.cluster.define_from_xml(dest, xml, start=True)
            started.append(v["name"] + suffix)
            if v.get("delay_s"):
                time.sleep(float(v["delay_s"]))
        rto = time.time() - t0
        result = {"mode": mode, "at": time.time(), "started": started, "rto_s": round(rto),
                  "rpo_s": round(rpo) if rpo is not None else (0 if site["mode"] == "stretch" else None)}
        self.db.run(update(dr_plans).where(dr_plans.c.name == plan_name)
                    .values(last_test=time.time(), last_result=json.dumps(result)))
        return (f"{mode} run of {plan_name}: {len(started)} VM(s) started in {round(rto)}s"
                + (f", data age {round(rpo)}s" if rpo is not None else ""))

    def _rewrite(self, xml, spec, site, mode, suffix):
        root = ET.fromstring(xml)
        root.find("name").text = root.findtext("name") + suffix
        uuid_el = root.find("uuid")
        if uuid_el is not None and mode == "test":
            root.remove(uuid_el)
        rbd = (site.get("storage") or {}).get("rbd")
        for disk in root.findall("devices/disk"):
            src = disk.find("source")
            if src is None:
                continue
            if src.get("file") and disk.get("device") == "cdrom":
                disk.remove(src)
            elif src.get("protocol") == "rbd" and rbd and site["mode"] == "rbd-mirror":
                src.set("name", src.get("name") + suffix)
                for h in src.findall("host"):
                    src.remove(h)
                for m in rbd["monitors"]:
                    ET.SubElement(src, "host", name=m, port=str(rbd.get("port", 6789)))
                sec = disk.find("auth/secret")
                if sec is not None:
                    sec.set("uuid", rbd["secret_uuid"])
        nmap = spec.get("test_network_map" if mode == "test" else "network_map", {})
        for iface in root.findall("devices/interface"):
            src = iface.find("source")
            if src is None:
                continue
            new = nmap.get(src.get("bridge")) or nmap.get("*")
            if new:
                src.set("bridge", new)
                vp = iface.find("virtualport")
                if vp is not None:
                    iface.remove(vp)
            if mode == "test":
                mac = iface.find("mac")
                if mac is not None:
                    iface.remove(mac)  # avoid MAC clashes with production
        gfx = root.find("devices/graphics")
        if gfx is not None and gfx.get("port") not in (None, "-1"):
            gfx.set("port", "-1"); gfx.set("autoport", "yes")
        return ET.tostring(root, encoding="unicode")

    def _place(self, nodes, mem):
        best, room = None, -1
        sch = self.cfg["scheduler"]
        for s in self.cluster.nodes_summary():
            if s["name"] in nodes and s["online"]:
                r = s["memory_mb"] - sch["host_reserve_mb"] - s["committed_mb"]
                if r > room:
                    best, room = s["name"], r
        if not best or room < mem:
            raise ClusterError("Not enough memory on DR-site nodes")
        return best

    def _wait_stopped(self, names, timeout, progress):
        end = time.time() + timeout
        while time.time() < end:
            running = [v["name"] for v in self.cluster.list_vms() if v["name"] in names and v["state"] != "stopped"]
            if not running:
                return
            time.sleep(5)
        raise ClusterError(f"Still running after {timeout}s: {', '.join(running)}")

    def _cleanup_test(self, spec, site, progress):
        removed = 0
        for v in spec["vms"]:
            hit = self.cluster.find(v["name"] + "-drtest")
            if hit:
                node, d = hit
                if d.isActive():
                    d.destroy()
                self.cluster._undefine(d)
                removed += 1
            if site["mode"] == "rbd-mirror":
                for img in self._images_for(v["name"]):
                    self.x.run(site["ceph_node"], ["rbd", "rm", img + "-drtest"], sudo=True, check=False)
        progress(f"Removed {removed} test VM(s)")
        return f"Cleaned up {removed} test VM(s)"
