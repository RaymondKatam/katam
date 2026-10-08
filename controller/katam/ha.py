"""Node health, fencing through iBMC Redfish, HA restart, witness checks, maintenance mode."""
import json
import logging
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout

from .db import insert, node_state, select, update, vm_backups
from .driver import ClusterError
from .redfish import Redfish

log = logging.getLogger("katam.ha")


class HAMonitor:
    def __init__(self, cfg, db, cluster, tasks):
        self.cfg, self.db, self.cluster, self.tasks = cfg, db, cluster, tasks
        self.h = cfg["ha"]
        self.fail = {}
        self._pool = ThreadPoolExecutor(max_workers=8)
        for n in cfg["nodes"]:
            if not db.one(select(node_state).where(node_state.c.node == n["name"])):
                db.run(insert(node_state).values(node=n["name"], status="ok", detail="", updated=time.time()))

    # ---- state ----------------------------------------------------------
    def state(self, node):
        r = self.db.one(select(node_state).where(node_state.c.node == node))
        return r["status"] if r else "ok"

    def states(self):
        return {r["node"]: r for r in self.db.all(select(node_state))}

    def _set(self, node, status, detail=""):
        self.db.run(update(node_state).where(node_state.c.node == node)
                    .values(status=status, detail=detail, updated=time.time()))
        self.db.audit("ha", f"node.{status}", node, "ok", detail)
        log.warning("node %s -> %s: %s", node, status, detail)

    # ---- loop -----------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, name="katam-ha", daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(float(self.h["interval_s"]))
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("HA tick failed")

    def _alive(self, node):
        def probe():
            self.cluster.conn(node).getInfo()
            return True
        try:
            return self._pool.submit(probe).result(timeout=8)
        except (FutTimeout, Exception):  # noqa: BLE001
            with self.cluster._lock:
                self.cluster._conns.pop(node, None)
            return False

    def tick(self):
        for n in self.cfg["nodes"]:
            name, st = n["name"], self.state(n["name"])
            if st in ("maintenance",):
                continue
            alive = self._alive(name)
            if alive:
                was = self.fail.pop(name, 0)
                if st == "suspect":
                    self._set(name, "ok", "Reachable again")
                if was and st == "ok":
                    self._start_autostart(name)
                continue
            if st == "fenced":
                continue
            self.fail[name] = self.fail.get(name, 0) + 1
            if self.fail[name] == 1:
                self._set(name, "suspect", "Not answering")
            if self.fail[name] >= int(self.h["failures_before_fence"]) and self.h.get("enabled"):
                self.handle_failure(name)

    # ---- failure handling ----------------------------------------------
    def witness_sees(self, node):
        url = self.h.get("witness_url")
        if not url:
            return None
        host = next(n for n in self.cfg["nodes"] if n["name"] == node)["host"]
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/probe?host={host}&port=22",
                                     headers={"Authorization": f"Bearer {self.h.get('witness_token', '')}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return bool(json.loads(r.read())["reachable"])
        except Exception as e:  # noqa: BLE001
            log.warning("witness unreachable: %s", e)
            return None

    def handle_failure(self, node):
        seen = self.witness_sees(node)
        if seen:
            self._set(node, "suspect", "Witness can still reach this node; the controller may be partitioned. Not fencing.")
            return
        if self.h.get("witness_url") and seen is None:
            self._set(node, "suspect", "Witness unreachable too; refusing to fence without a second opinion.")
            return
        n = next(x for x in self.cfg["nodes"] if x["name"] == node)
        if not n.get("bmc"):
            self._set(node, "suspect", "No BMC configured; confirm the node is off, then use Confirm down.")
            return
        try:
            self.fence(node)
        except Exception as e:  # noqa: BLE001
            self._set(node, "suspect", f"Fencing failed: {e}. Not restarting VMs.")
            return
        self.tasks.submit("ha.restart", f"node:{node}", lambda p: self.restart_vms(node, p), actor="ha")

    def fence(self, node):
        n = next(x for x in self.cfg["nodes"] if x["name"] == node)
        rf = Redfish(n["bmc"])
        rf.reset("ForceOff")
        for _ in range(30):
            if (rf.power_state() or "").lower() == "off":
                self._set(node, "fenced", "Powered off through iBMC Redfish")
                return
            time.sleep(2)
        raise ClusterError("Node did not report power off within 60s")

    def confirm_down(self, node):
        """Operator confirms a node without BMC access is really off."""
        self._set(node, "fenced", "Confirmed down by an operator")
        return self.tasks.submit("ha.restart", f"node:{node}", lambda p: self.restart_vms(node, p), actor="ha")

    def restart_vms(self, node, progress):
        if self.cfg["storage"]["type"] != "rbd":
            return "Local-disk mode: VMs can't restart elsewhere; they will start when the node returns"
        rows = [r for r in self.db.all(select(vm_backups).where(vm_backups.c.node == node))
                if r["ha"] and r["running"] and not r["gpu"]]
        started, skipped = [], []
        for r in rows:
            mem = _mem_mb(r["xml"])
            try:
                dest = self._place(mem, exclude=node)
                progress(f"Restarting {r['vm']} on {dest}")
                self.cluster.define_from_xml(dest, r["xml"], start=True)
                self.db.run(update(vm_backups).where(vm_backups.c.vm == r["vm"]).values(node=dest))
                started.append(r["vm"])
                time.sleep(float(self.h["restart_delay_s"]))
            except Exception as e:  # noqa: BLE001
                progress(f"Could not restart {r['vm']}: {e}")
                skipped.append(r["vm"])
        return f"Restarted {len(started)} VM(s) from {node}" + (f"; failed: {', '.join(skipped)}" if skipped else "")

    def _place(self, memory_mb, exclude):
        ok = [n["name"] for n in self.cfg["nodes"]
              if n["name"] != exclude and n["site"] == "primary" and self.state(n["name"]) == "ok"]
        best, room = None, -1
        sch = self.cfg["scheduler"]
        for s in self.cluster.nodes_summary():
            if s["name"] in ok and s["online"]:
                r = s["memory_mb"] * sch["memory_overcommit"] - sch["host_reserve_mb"] - s["committed_mb"]
                if r > room:
                    best, room = s["name"], r
        if not best or room < memory_mb:
            raise ClusterError("No surviving node has enough memory")
        return best

    def recover(self, node):
        """Bring a fenced node back: drop stale VM copies before anything can start them."""
        if not self._alive(node):
            raise ClusterError(f"{node} isn't reachable yet; power it on first")
        moved = [r["vm"] for r in self.db.all(select(vm_backups).where(vm_backups.c.node != node))]
        removed = self.cluster.undefine_stale(node, moved)
        self._set(node, "ok", f"Recovered; removed {len(removed)} stale definition(s)")
        self._start_autostart(node)
        return removed

    def _start_autostart(self, node):
        for vm in self.cluster._node_vms(node):
            if vm["autostart"] and vm["state"] == "stopped":
                try:
                    self.cluster.power(vm["name"], "start")
                    log.info("autostarted %s on %s", vm["name"], node)
                except Exception as e:  # noqa: BLE001
                    log.warning("autostart of %s failed: %s", vm["name"], e)

    # ---- maintenance -----------------------------------------------------
    def maintenance(self, node, on, progress):
        if not on:
            self._set(node, "ok", "Left maintenance")
            self._start_autostart(node)
            return f"{node} is back in service"
        self._set(node, "maintenance", "Evacuating")
        left = []
        for vm in self.cluster._node_vms(node):
            if vm["state"] != "running":
                continue
            if vm.get("gpus"):
                left.append(vm["name"])
                continue
            dest = self._place(vm["memory_mb"], exclude=node)
            progress(f"Moving {vm['name']} to {dest}")
            self.cluster.migrate_vm(vm["name"], dest, progress)
        msg = f"{node} is in maintenance"
        if left:
            msg += f"; GPU VMs still running there: {', '.join(left)}"
        self._set(node, "maintenance", msg)
        return msg


def _mem_mb(xml):
    import xml.etree.ElementTree as ET
    el = ET.fromstring(xml).find("memory")
    unit = (el.get("unit") or "KiB").lower()
    v = int(el.text)
    return v // 1024 if unit in ("kib", "k") else v if unit in ("mib", "m") else v * 1024


def tcp_reachable(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
