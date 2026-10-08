"""Prometheus metrics (/metrics) and Grafana annotations.

Prometheus scrapes /metrics on every controller (or on the virtual IP); Grafana charts them with
the bundled dashboard (/usr/share/katam/grafana/katam-overview.json). Inventory comes from the
latest metering sample, so a scrape never waits on libvirt for every VM; node figures are
cached for metrics.cache_s seconds.

Annotations: with grafana.url and grafana.token set, Katam posts events (node fenced, HA
restarts, leader changes, DR failovers, upgrades, runbook runs, Git syncs) to Grafana's
annotation API, tagged "katam", so they show up as markers on every dashboard.
"""
import ipaddress
import logging
import queue
import threading
import time

import requests

from sqlalchemy import func

from . import __version__
from .db import audit, node_power, node_state, select, tasks, usage

log = logging.getLogger("katam.metrics")

NODE_STATES = ("ok", "suspect", "fenced", "maintenance")


def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Exposition:
    def __init__(self):
        self.lines, self._seen = [], set()

    def add(self, name, help_, kind, value, **labels):
        if value is None:
            return
        if name not in self._seen:
            self._seen.add(name)
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {kind}")
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        v = float(value)
        num = str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
        self.lines.append(f"{name}{{{lab}}} {num}" if lab else f"{name} {num}")

    def text(self):
        return "\n".join(self.lines) + "\n"


class Metrics:
    def __init__(self, cfg, db, cluster, ha, leadership, protection=None, updates=None):
        self.cfg, self.db, self.cluster, self.ha = cfg, db, cluster, ha
        self.lead, self.protection, self.updates = leadership, protection, updates
        self.m = cfg["metrics"]
        self._nodes = (0.0, [])
        self._lock = threading.Lock()

    # ---- access control -------------------------------------------------------
    def allowed(self, remote_addr, auth_header):
        if not self.m.get("enabled", True):
            return False
        tok = self.m.get("token")
        if tok and auth_header == f"Bearer {tok}":
            return True
        try:
            ip = ipaddress.ip_address((remote_addr or "").split("%")[0])
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(n, strict=False) for n in self.m.get("allow") or [])

    # ---- collection ---------------------------------------------------------------
    def _node_summaries(self):
        with self._lock:
            ts, data = self._nodes
            if time.time() - ts < float(self.m.get("cache_s", 15)):
                return data
            data = self.cluster.nodes_summary()
            self._nodes = (time.time(), data)
            return data

    def render(self):
        with self._lock:
            ts, text = getattr(self, "_out", (0.0, ""))
            if time.time() - ts < float(self.m.get("cache_s", 15)) and text:
                return text
        text = self._render()
        with self._lock:
            self._out = (time.time(), text)
        return text

    def _render(self):
        e = Exposition()
        info = self.lead.info() if self.lead else {"member": "-", "role": "leader", "mode": "single"}
        e.add("katam_info", "Controller version and role", "gauge", 1, version=__version__,
              member=info["member"], role=info["role"], mode=info["mode"])
        e.add("katam_controller_is_leader", "1 on the controller that runs cluster-wide services", "gauge",
              1 if info["role"] == "leader" else 0, member=info["member"])
        if info.get("term") is not None:
            e.add("katam_leader_term", "Election term of the current leader (rises on every failover)",
                  "gauge", info["term"])
        if info["mode"] == "etcd":
            e.add("katam_cluster_members", "Controllers currently registered in etcd", "gauge",
                  len(info.get("members") or []))
            for h in info.get("etcd") or []:
                e.add("katam_etcd_endpoint_healthy", "etcd endpoint health seen from this controller",
                      "gauge", 1 if h["healthy"] else 0, endpoint=h["endpoint"])

        # nodes
        states = {r["node"]: r["status"] for r in self.db.all(select(node_state))}
        sites = {n["name"]: n.get("site", "primary") for n in self.cfg["nodes"]}
        for s in self._node_summaries():
            n, lab = s["name"], {"node": s["name"], "site": sites.get(s["name"], "primary")}
            e.add("katam_node_up", "1 if libvirt on the node answers", "gauge", 1 if s["online"] else 0, **lab)
            for st in NODE_STATES:
                e.add("katam_node_state", "HA state of the node (one series per state)", "gauge",
                      1 if states.get(n, "ok") == st else 0, state=st, **lab)
            if s["online"]:
                e.add("katam_node_memory_bytes", "Physical memory", "gauge", s["memory_mb"] * 2**20, **lab)
                e.add("katam_node_memory_free_bytes", "Free memory reported by the host", "gauge",
                      s["free_mb"] * 2**20, **lab)
                e.add("katam_node_memory_committed_bytes", "Memory allocated to VMs", "gauge",
                      s["committed_mb"] * 2**20, **lab)
                e.add("katam_node_cpu_threads", "Hardware threads", "gauge", s["threads"], **lab)
                e.add("katam_node_vcpus_allocated", "vCPUs allocated to VMs", "gauge", s["vcpus_allocated"], **lab)
                e.add("katam_node_vms", "VMs defined on the node", "gauge", s["vm_count"], **lab)
                e.add("katam_node_vms_running", "VMs running on the node", "gauge", s["running"], **lab)
                e.add("katam_node_gpus", "GPUs available for passthrough", "gauge", s["gpu_total"], **lab)
                e.add("katam_node_gpus_free", "GPUs not assigned to a VM", "gauge", s["gpu_free"], **lab)
        last_power = {}
        for r in self.db.all(select(node_power).where(node_power.c.ts > time.time() - 3600)
                             .order_by(node_power.c.ts)):
            last_power[r["node"]] = r["watts"]
        for n, w in last_power.items():
            e.add("katam_node_power_watts", "Power draw read from the BMC", "gauge", w, node=n)

        # VMs, from the latest metering sample
        latest = self.db.one(select(func.max(usage.c.ts).label("ts")))["ts"]
        rates = self.cfg["finops"]["rates"]
        cur = self.cfg["finops"]["currency"]
        per_project = {}
        if latest:
            e.add("katam_metering_last_sample_timestamp_seconds", "When VMs were last sampled", "gauge", latest)
            for v in self.db.all(select(usage).where(usage.c.ts >= latest - 1)):
                lab = {"vm": v["vm"], "project": v["project"], "node": v["node"]}
                e.add("katam_vm_running", "1 if the VM is running", "gauge", 1 if v["running"] else 0, **lab)
                e.add("katam_vm_vcpus", "vCPUs", "gauge", v["vcpus"], **lab)
                e.add("katam_vm_memory_bytes", "Memory", "gauge", v["memory_mb"] * 2**20, **lab)
                e.add("katam_vm_disk_bytes", "Provisioned disk", "gauge", (v["disk_gb"] or 0) * 2**30, **lab)
                e.add("katam_vm_gpus", "Passed-through GPUs", "gauge", v["gpus"] or 0, **lab)
                if v["cpu_util"] is not None:
                    e.add("katam_vm_cpu_utilization", "CPU use over the last sample, 0-1", "gauge",
                          v["cpu_util"], **lab)
                run = 1 if v["running"] else 0
                hourly = run * (v["vcpus"] * rates["vcpu_hour"] + v["memory_mb"] / 1024 * rates["ram_gb_hour"]
                                + (v["gpus"] or 0) * rates["gpu_hour"]) + \
                    (v["disk_gb"] or 0) * rates["disk_gb_month"] / 730
                p = per_project.setdefault(v["project"], [0, 0, 0.0])
                p[0] += 1
                p[1] += run
                p[2] += hourly
        for proj, (vms, running, hourly) in sorted(per_project.items()):
            e.add("katam_project_vms", "VMs in the project", "gauge", vms, project=proj)
            e.add("katam_project_vms_running", "Running VMs in the project", "gauge", running, project=proj)
            e.add("katam_project_cost_per_hour", "Current cost rate from the rate card", "gauge",
                  round(hourly, 4), project=proj, currency=cur)

        # tasks, HA events, runbooks
        day = time.time() - 86400
        for r in self.db.all(select(tasks.c.kind, tasks.c.status, func.count().label("n"))
                             .where(tasks.c.created > day).group_by(tasks.c.kind, tasks.c.status)):
            e.add("katam_tasks_24h", "Tasks started in the last 24 hours", "gauge", r["n"],
                  kind=r["kind"], status=r["status"])
        running = self.db.one(select(func.count().label("n")).select_from(tasks)
                              .where(tasks.c.status == "running"))["n"]
        e.add("katam_tasks_running", "Tasks running now", "gauge", running)
        for action in ("node.fenced", "leader.elected", "dr.failover", "runbook.sync"):
            n = self.db.one(select(func.count().label("n")).select_from(audit)
                            .where(audit.c.action == action))["n"]
            e.add("katam_events_total", "Events since the database was created", "counter", n, event=action)

        if self.protection:
            try:
                cov = self.protection.coverage()
                e.add("katam_vms_without_snapshots", "VMs with no snapshot at all", "gauge",
                      sum(1 for c in cov if not c["snapshots"]))
                e.add("katam_vms_without_policy", "VMs not covered by a protection policy", "gauge",
                      sum(1 for c in cov if not c["policies"]))
            except Exception as ex:  # noqa: BLE001
                log.debug("coverage for metrics failed: %s", ex)
        if self.updates and self.updates._cache[1]:      # only what the UI/timer already fetched
            u = self.updates.check()
            e.add("katam_update_available", "1 when the update feed has a newer release", "gauge",
                  1 if u.get("available") else 0, current=__version__, latest=u.get("latest") or "")
        return e.text()


class GrafanaAnnotator:
    """Posts audit events to Grafana as annotations, off the request path."""

    EVENTS = {"node.suspect", "node.fenced", "node.ok", "node.maintenance", "node.confirm_down",
              "node.recover", "ha.restart", "leader.elected", "leader.stepped_down", "dr.failover",
              "dr.watch", "dr", "updates.rollout", "runbook", "runbook.sync", "snapshot.restore",
              "vault.restore"}

    def __init__(self, cfg):
        g = cfg["grafana"]
        self.url = (g.get("url") or "").rstrip("/")
        self.token = g.get("token")
        self.uid = g.get("dashboard_uid")
        self.verify = g.get("verify_tls", True)
        self.q = queue.Queue(maxsize=1000)
        self.enabled = bool(self.url and self.token)
        if self.enabled:
            threading.Thread(target=self._worker, name="katam-grafana", daemon=True).start()

    def __call__(self, ev):
        if not self.enabled:
            return
        if ev["action"] not in self.EVENTS and not ev["action"].startswith("dr."):
            return
        try:
            self.q.put_nowait(ev)
        except queue.Full:
            pass

    def payload(self, ev):
        text = f"{ev['action']} {ev['target']}: {ev['result']}"
        if ev.get("detail"):
            text += f"<br>{str(ev['detail'])[:400]}"
        body = {"time": int(time.time() * 1000), "text": text,
                "tags": ["katam", ev["action"].split(".")[0], ev["result"]]}
        if self.uid:
            body["dashboardUID"] = self.uid
        return body

    def _worker(self):
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {self.token}"
        s.verify = self.verify
        while True:
            ev = self.q.get()
            try:
                r = s.post(f"{self.url}/api/annotations", json=self.payload(ev), timeout=10)
                if r.status_code >= 300:
                    log.warning("Grafana annotation rejected: %s %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.warning("Grafana annotation failed: %s", e)
