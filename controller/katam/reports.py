"""Reports as rows, rendered to CSV or printable HTML (print to PDF from the browser)."""
import csv
import io
import json
import time
from html import escape

from . import finops
from .db import audit, dr_plans, select

REPORTS = {
    "inventory": "Every VM with its placement, size, network protection and HA status",
    "cost": "Cost per project and per VM for the period, from metered usage",
    "rightsizing": "Idle, oversized and long-stopped VMs with suggested actions",
    "capacity": "Memory, vCPU and GPU headroom per node, plus Ceph usage",
    "dr": "DR plans with their last test result, and VMs no plan protects",
    "segmentation": "Which VMs are micro-segmented and by which security groups",
    "audit": "Who did what, from the audit log",
    "protection": "Snapshot schedules, locked snapshots and immutable vault copies per VM",
}


def build(name, ctx, start, end, projects):
    cluster, db, cfg = ctx["cluster"], ctx["db"], ctx["cfg"]
    vms = [v for v in cluster.list_vms() if projects is None or v["project"] in projects]
    if name == "inventory":
        cols = ["name", "project", "node", "state", "vcpus", "memory_gb", "gpus", "ips", "ha",
                "autostart", "secgroups", "template", "created"]
        rows = [{**v, "memory_gb": round(v["memory_mb"] / 1024, 1), "ips": " ".join(v["ips"]),
                 "secgroups": " ".join(v["secgroups"])} for v in vms]
        return cols, rows, None
    if name in ("cost", "rightsizing"):
        rep = finops.cost_report(db, cfg, start, end, projects)
        cur = rep["currency"]
        if name == "rightsizing":
            cols = ["vm", "project", "finding", "action", "monthly_saving"]
            return cols, finops.rightsizing(rep, cfg["finops"]["rates"]), f"Savings in {cur} per month"
        cols = ["project", "vm", "running_hours", "vcpu_hours", "gpu_hours", "cpu_avg", "compute",
                "storage", "total"]
        rows = [{**v} for v in rep["vms"]]
        for p in rep["projects"]:
            rows.append({"project": p["project"], "vm": "(project total)", "vcpu_hours": p["vcpu_hours"],
                         "compute": p["compute"], "storage": p["storage"], "total": p["total"]})
        note = (f"Amounts in {cur}. Infrastructure power for the period: {rep['power_kwh']} kWh, "
                f"{rep['power_cost']} {cur} (not included in the totals above).")
        return cols, rows, note
    if name == "capacity":
        cols = ["name", "online", "memory_gb", "committed_gb", "reserve_gb", "headroom_gb",
                "threads", "vcpus_allocated", "vcpu_ratio", "gpu_total", "gpu_free", "vm_count"]
        rows = []
        for n in cluster.nodes_summary():
            if not n["online"]:
                rows.append({"name": n["name"], "online": False})
                continue
            rows.append({"name": n["name"], "online": True, "memory_gb": round(n["memory_mb"] / 1024),
                         "committed_gb": round(n["committed_mb"] / 1024), "reserve_gb": round(n["reserve_mb"] / 1024),
                         "headroom_gb": round((n["memory_mb"] - n["committed_mb"] - n["reserve_mb"]) / 1024),
                         "threads": n["threads"], "vcpus_allocated": n["vcpus_allocated"],
                         "vcpu_ratio": round(n["vcpus_allocated"] / max(n["threads"], 1), 2),
                         "gpu_total": n.get("gpu_total", 0), "gpu_free": n.get("gpu_free", 0),
                         "vm_count": n["vm_count"]})
        note = None
        if cfg["storage"]["type"] == "rbd" and ctx.get("nexec"):
            try:
                df = json.loads(ctx["nexec"].run(cfg["storage"]["import_node"], ["ceph", "df", "-f", "json"], sudo=True))
                s = df["stats"]
                note = (f"Ceph raw: {round(s['total_used_raw_bytes'] / 2**40, 2)} of "
                        f"{round(s['total_bytes'] / 2**40, 2)} TiB used "
                        f"({round(100 * s['total_used_raw_bytes'] / max(s['total_bytes'], 1), 1)}%).")
            except Exception:  # noqa: BLE001
                note = "Ceph usage unavailable."
        return cols, rows, note
    if name == "dr":
        cols = ["plan", "site", "vms", "last_run", "mode", "rto_s", "rpo_s"]
        rows, protected = [], set()
        for p in db.all(select(dr_plans)):
            spec = json.loads(p["spec"])
            res = json.loads(p["last_result"]) if p["last_result"] else {}
            protected.update(v["name"] for v in spec["vms"])
            rows.append({"plan": p["name"], "site": spec.get("site", "dr"),
                         "vms": " ".join(v["name"] for v in spec["vms"]),
                         "last_run": time.strftime("%Y-%m-%d %H:%M", time.localtime(res["at"])) if res else "never",
                         "mode": res.get("mode"), "rto_s": res.get("rto_s"), "rpo_s": res.get("rpo_s")})
        unprotected = [v["name"] for v in vms if v["name"] not in protected]
        note = "Not in any DR plan: " + (", ".join(unprotected) if unprotected else "none")
        return cols, rows, note
    if name == "segmentation":
        cols = ["name", "project", "networks", "segmented", "secgroups"]
        ovn_nets = {n["name"] for n in cfg["networks"] if n["type"] != "bridge"}
        rows = []
        for v in vms:
            nets = [n for n in (v.get("networks") or "").split(",") if n]
            seg = bool(nets) and all(n in ovn_nets for n in nets)
            rows.append({"name": v["name"], "project": v["project"], "networks": " ".join(nets),
                         "segmented": "yes" if seg else "no", "secgroups": " ".join(v["secgroups"])})
        covered = sum(1 for r in rows if r["segmented"] == "yes")
        return cols, rows, f"{covered} of {len(rows)} VMs are on micro-segmented networks."
    if name == "protection":
        cols = ["vm", "project", "policies", "snapshots", "locked", "last_snapshot_h", "vault_copies", "vault_until"]
        rows = ctx["protection"].coverage(projects)
        bare = [r["vm"] for r in rows if not r["snapshots"] and not r["vault_copies"]]
        return cols, rows, "No snapshots or vault copies: " + (", ".join(bare) if bare else "none")
    if name == "audit":
        cols = ["time", "actor", "action", "target", "result", "detail"]
        q = select(audit).where((audit.c.ts >= start) & (audit.c.ts < end)).order_by(audit.c.ts.desc()).limit(5000)
        rows = [{**r, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))} for r in db.all(q)]
        return cols, rows, None
    raise KeyError(name)


def to_csv(cols, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return buf.getvalue()


def to_html(title, cols, rows, note, start, end):
    period = f"{time.strftime('%d %b %Y', time.localtime(start))} to {time.strftime('%d %b %Y', time.localtime(end))}"
    head = "".join(f"<th>{escape(c.replace('_', ' '))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{escape('' if r.get(c) is None else str(r.get(c)))}</td>" for c in cols)
                   + "</tr>" for r in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>body{{font:14px/1.45 Barlow,system-ui,sans-serif;color:#16212B;margin:2rem}}
h1{{font-size:1.5rem;margin:0}}p{{color:#5A6A77}}table{{border-collapse:collapse;width:100%;margin-top:1rem}}
th,td{{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #C6D0D8;font-size:13px}}
th{{color:#5A6A77;font-weight:500}}@media print{{body{{margin:0}}}}</style></head><body>
<h1>Katam Virtualization: {escape(title)}</h1><p>{escape(period)}. Generated {time.strftime('%d %b %Y %H:%M')}.</p>
{f'<p>{escape(note)}</p>' if note else ''}<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>"""
