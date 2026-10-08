"""Metering (VM allocation, CPU use, node power) and cost/rightsizing analysis."""
import logging
import math
import threading
import time

from .db import insert, node_power, select, update, usage, vm_backups
from .redfish import Redfish

log = logging.getLogger("katam.finops")


class Metering:
    def __init__(self, cfg, db, cluster):
        self.cfg, self.db, self.cluster = cfg, db, cluster
        self.interval = float(cfg["finops"]["sample_interval_s"])
        self._last_cpu = {}
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="katam-metering", daemon=True).start()

    def _loop(self):
        while not self._stop.wait(5 if not self._last_cpu else self.interval):
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - metering must never stop
                log.exception("metering sample failed")

    def sample(self, interval=None):
        interval = interval or self.interval
        now = time.time()
        inv = self.cluster.snapshot_inventory()
        rows = []
        for vm in inv:
            running = vm["state"] == "running"
            util = None
            prev = self._last_cpu.get(vm["uuid"])
            if prev and running:
                dt = now - prev[0]
                if dt > 0:
                    util = max(0.0, min(1.0, (vm["cpu_time_ns"] - prev[1]) / (dt * 1e9 * max(vm["vcpus"], 1))))
            self._last_cpu[vm["uuid"]] = (now, vm["cpu_time_ns"])
            rows.append(dict(ts=now, vm=vm["name"], project=vm["project"], node=vm["node"],
                             running=running, vcpus=vm["vcpus"], memory_mb=vm["memory_mb"],
                             disk_gb=vm["disk_gb"], gpus=vm.get("gpus", 0), cpu_util=util,
                             interval_s=interval))
            self._backup(vm, now)
        if rows:
            with self.db.engine.begin() as c:
                c.execute(insert(usage), rows)
        for n in self.cfg["nodes"]:
            if n.get("bmc"):
                try:
                    w = Redfish(n["bmc"]).watts()
                    if w is not None:
                        self.db.run(insert(node_power).values(ts=now, node=n["name"], watts=float(w),
                                                              interval_s=interval))
                except Exception as e:  # noqa: BLE001
                    log.warning("power reading from %s failed: %s", n["name"], e)
        return len(rows)

    def _backup(self, vm, now):
        row = dict(node=vm["node"], xml=vm["xml"], project=vm["project"], ha=vm["ha"],
                   autostart=vm["autostart"], running=vm["state"] == "running",
                   gpu=bool(vm.get("gpus")), updated=now)
        if self.db.one(select(vm_backups.c.vm).where(vm_backups.c.vm == vm["name"])):
            self.db.run(update(vm_backups).where(vm_backups.c.vm == vm["name"]).values(**row))
        else:
            self.db.run(insert(vm_backups).values(vm=vm["name"], **row))


def cost_report(db, cfg, start, end, projects=None):
    r = cfg["finops"]["rates"]
    q = select(usage).where((usage.c.ts >= start) & (usage.c.ts < end))
    if projects is not None:
        q = q.where(usage.c.project.in_(list(projects)))
    per_vm = {}
    for s in db.all(q):
        h = s["interval_s"] / 3600
        run = 1 if s["running"] else 0
        compute = run * h * (s["vcpus"] * r["vcpu_hour"] + s["memory_mb"] / 1024 * r["ram_gb_hour"]
                             + (s["gpus"] or 0) * r["gpu_hour"])
        storage = (s["disk_gb"] or 0) * r["disk_gb_month"] * h / 730
        v = per_vm.setdefault(s["vm"], {"vm": s["vm"], "project": s["project"], "compute": 0.0,
                                        "storage": 0.0, "running_hours": 0.0, "vcpu_hours": 0.0,
                                        "gpu_hours": 0.0, "utils": [], "vcpus": s["vcpus"],
                                        "memory_mb": s["memory_mb"], "disk_gb": s["disk_gb"]})
        v["compute"] += compute
        v["storage"] += storage
        v["running_hours"] += run * h
        v["vcpu_hours"] += run * h * s["vcpus"]
        v["gpu_hours"] += run * h * (s["gpus"] or 0)
        v.update(vcpus=s["vcpus"], memory_mb=s["memory_mb"], disk_gb=s["disk_gb"])
        if s["cpu_util"] is not None:
            v["utils"].append(s["cpu_util"])
    projects_out = {}
    for v in per_vm.values():
        v["total"] = round(v["compute"] + v["storage"], 2)
        v["compute"], v["storage"] = round(v["compute"], 2), round(v["storage"], 2)
        u = sorted(v.pop("utils"))
        v["cpu_avg"] = round(sum(u) / len(u), 3) if u else None
        v["cpu_p95"] = round(u[min(len(u) - 1, int(len(u) * 0.95))], 3) if u else None
        p = projects_out.setdefault(v["project"], {"project": v["project"], "vms": 0, "compute": 0.0,
                                                   "storage": 0.0, "total": 0.0, "vcpu_hours": 0.0})
        p["vms"] += 1
        for k in ("compute", "storage", "total", "vcpu_hours"):
            p[k] = round(p[k] + v[k], 2)
    power_kwh = sum(x["watts"] * x["interval_s"] / 3600 / 1000 for x in db.all(
        select(node_power).where((node_power.c.ts >= start) & (node_power.c.ts < end))))
    return {"currency": cfg["finops"]["currency"], "start": start, "end": end,
            "projects": sorted(projects_out.values(), key=lambda x: -x["total"]),
            "vms": sorted(per_vm.values(), key=lambda x: -x["total"]),
            "power_kwh": round(power_kwh, 2),
            "power_cost": round(power_kwh * r["kwh"], 2)}


def rightsizing(report, rates):
    recs = []
    span_h = (report["end"] - report["start"]) / 3600
    for v in report["vms"]:
        if v["running_hours"] == 0 and span_h >= 24:
            recs.append({"vm": v["vm"], "project": v["project"], "finding": "Stopped for the whole period",
                         "action": "Delete it or export and archive it; its disk is still billed",
                         "monthly_saving": round(v["storage"] * 730 / max(span_h, 1), 2)})
            continue
        if v["cpu_avg"] is None:
            continue
        if v["cpu_avg"] < 0.05 and v["running_hours"] > 24:
            recs.append({"vm": v["vm"], "project": v["project"],
                         "finding": f"Average CPU {v['cpu_avg']:.0%}", "action": "Idle: stop it or merge its workload",
                         "monthly_saving": round(v["compute"] * 730 / max(v["running_hours"], 1), 2)})
        elif v["cpu_p95"] is not None and v["cpu_p95"] < 0.4 and v["vcpus"] > 2:
            target = max(2, math.ceil(v["cpu_p95"] * v["vcpus"] / 0.6))
            if target < v["vcpus"]:
                recs.append({"vm": v["vm"], "project": v["project"],
                             "finding": f"95th percentile CPU {v['cpu_p95']:.0%} of {v['vcpus']} vCPUs",
                             "action": f"Reduce to {target} vCPUs",
                             "monthly_saving": round((v["vcpus"] - target) * 730 * rates["vcpu_hour"], 2)})
    return recs
