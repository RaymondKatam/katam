"""katamctl: command-line client for the Katam API (scripts, CI, IaC pipelines).

  export KATAM_URL=https://katam.example.com KATAM_TOKEN=katam_xxxx
  katamctl vms
  katamctl apply -f stack.yaml --dry-run
  katamctl power web-01 reboot
  katamctl export web-01 --format ova --wait
  katamctl image import ubuntu-2404 --url https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
  katamctl runbook run restart-with-health-check -p vm=web-01 -p host=172.20.20.51
  katamctl report cost --days 30 --format csv > cost.csv
"""
import argparse
import json
import os
import sys
import time

import requests


class Client:
    def __init__(self):
        self.url = os.environ.get("KATAM_URL", "http://localhost:8080").rstrip("/")
        token = os.environ.get("KATAM_TOKEN")
        if not token:
            sys.exit("Set KATAM_TOKEN (create one under Access > API tokens in the UI)")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.s.verify = os.environ.get("KATAM_CA", True)

    def call(self, method, path, raw=False, **kw):
        r = self.s.request(method, f"{self.url}/api/v1{path}", timeout=600, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error")
            except ValueError:
                msg = r.text
            sys.exit(f"Error {r.status_code}: {msg}")
        return r if raw else r.json()

    def wait(self, tid):
        while True:
            t = self.call("GET", f"/tasks/{tid}")
            if t["status"] != "running":
                print(t["log"] or t["message"])
                if t["status"] != "done":
                    sys.exit(1)
                return t
            time.sleep(2)


def table(rows, cols):
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows else len(c) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "") if r.get(c) is not None else "").ljust(widths[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser(prog="katamctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("vms")
    sub.add_parser("nodes")
    sub.add_parser("tasks")
    sub.add_parser("images")
    sub.add_parser("templates")
    p = sub.add_parser("apply"); p.add_argument("-f", required=True); p.add_argument("--dry-run", action="store_true"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("create"); p.add_argument("name"); p.add_argument("--template"); p.add_argument("--image")
    p.add_argument("--project", default="default"); p.add_argument("--vcpus", type=int); p.add_argument("--memory-gb", type=int)
    p.add_argument("--disk-gb", type=int); p.add_argument("--network", action="append"); p.add_argument("--ip"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("power"); p.add_argument("name"); p.add_argument("action", choices=["start", "shutdown", "poweroff", "reboot", "pause", "resume"])
    p = sub.add_parser("delete"); p.add_argument("name"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("migrate"); p.add_argument("name"); p.add_argument("node"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("export"); p.add_argument("name"); p.add_argument("--format", default="ova"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("download"); p.add_argument("file")
    p = sub.add_parser("image"); isub = p.add_subparsers(dest="icmd", required=True)
    q = isub.add_parser("import"); q.add_argument("name"); q.add_argument("--url"); q.add_argument("--file")
    q.add_argument("--project"); q.add_argument("--os-type", default="linux"); q.add_argument("--firmware", default="bios")
    q.add_argument("--no-cloud-init", action="store_true"); q.add_argument("--v2v", action="store_true"); q.add_argument("--wait", action="store_true")
    p = sub.add_parser("runbook"); rsub = p.add_subparsers(dest="rcmd", required=True)
    q = rsub.add_parser("push"); q.add_argument("-f", required=True)
    q = rsub.add_parser("run"); q.add_argument("name"); q.add_argument("-p", action="append", default=[]); q.add_argument("--dry-run", action="store_true"); q.add_argument("--wait", action="store_true")
    p = sub.add_parser("report"); p.add_argument("name"); p.add_argument("--days", type=float, default=30); p.add_argument("--format", default="csv")
    p = sub.add_parser("dr"); p.add_argument("plan"); p.add_argument("mode", choices=["test", "cleanup-test", "planned", "disaster"]); p.add_argument("--wait", action="store_true")
    a = ap.parse_args()
    c = Client()

    def done(resp):
        if getattr(a, "wait", False) and resp.get("task"):
            c.wait(resp["task"])
        else:
            print(json.dumps(resp, indent=2))

    if a.cmd == "vms":
        table(c.call("GET", "/vms"), ["name", "project", "node", "state", "vcpus", "memory_mb", "gpus", "ips"])
    elif a.cmd == "nodes":
        table(c.call("GET", "/nodes"), ["name", "site", "online", "ha_status", "memory_mb", "committed_mb", "vm_count", "gpu_free"])
    elif a.cmd == "tasks":
        table(c.call("GET", "/tasks"), ["id", "kind", "target", "status", "actor", "message"])
    elif a.cmd == "images":
        table(c.call("GET", "/images"), ["name", "project", "status", "os_type", "firmware", "size_gb", "source"])
    elif a.cmd == "templates":
        table(c.call("GET", "/templates"), ["name", "project", "label"])
    elif a.cmd == "apply":
        with open(a.f) as f:
            body = f.read()
        r = c.call("POST", "/apply" + ("?dry_run=1" if a.dry_run else ""), data=body,
                   headers={"Content-Type": "application/yaml"})
        table(r["plan"], ["op", "kind", "name"])
        if a.wait:
            for t in r["tasks"]:
                c.wait(t)
    elif a.cmd == "create":
        body = {k: v for k, v in {"name": a.name, "template": a.template, "image": a.image, "project": a.project,
                                  "vcpus": a.vcpus, "memory_gb": a.memory_gb, "disk_gb": a.disk_gb,
                                  "networks": a.network, "ip": a.ip}.items() if v is not None}
        done(c.call("POST", "/vms", json=body))
    elif a.cmd == "power":
        done(c.call("POST", f"/vms/{a.name}/power/{a.action}"))
    elif a.cmd == "delete":
        done(c.call("DELETE", f"/vms/{a.name}"))
    elif a.cmd == "migrate":
        done(c.call("POST", f"/vms/{a.name}/migrate", json={"node": a.node}))
    elif a.cmd == "export":
        done(c.call("POST", f"/vms/{a.name}/export", json={"format": a.format}))
    elif a.cmd == "download":
        r = c.call("GET", f"/exports/{a.file}", raw=True, stream=True)
        with open(a.file, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        print(f"Saved {a.file}")
    elif a.cmd == "image":
        meta = {"name": a.name, "project": a.project or "", "os_type": a.os_type, "firmware": a.firmware,
                "cloud_init": "false" if a.no_cloud_init else "true", "v2v": "true" if a.v2v else "false"}
        if a.url:
            done(c.call("POST", "/images", json={**meta, "url": a.url}))
        elif a.file:
            with open(a.file, "rb") as f:
                done(c.call("POST", "/images", data=meta, files={"file": (os.path.basename(a.file), f)}))
        else:
            sys.exit("Give --url or --file")
    elif a.cmd == "runbook":
        if a.rcmd == "push":
            import yaml
            with open(a.f) as f:
                src = f.read()
            name = yaml.safe_load(src)["name"]
            print(c.call("PUT", f"/runbooks/{name}", json={"source": src}))
        else:
            params = dict(x.split("=", 1) for x in a.p)
            r = c.call("POST", f"/runbooks/{a.name}/run", json={"params": params, "dry_run": a.dry_run})
            if a.dry_run:
                print(json.dumps(r["plan"], indent=2))
            else:
                done(r)
    elif a.cmd == "report":
        r = c.call("GET", f"/reports/{a.name}?days={a.days}&format={a.format}", raw=True)
        sys.stdout.write(r.text)
    elif a.cmd == "dr":
        body = {"mode": a.mode}
        if a.mode in ("planned", "disaster"):
            if input(f"Type the plan name to confirm a {a.mode} failover: ") != a.plan:
                sys.exit("Not confirmed")
            body["confirm"] = a.plan
        done(c.call("POST", f"/dr/plans/{a.plan}/run", json=body))


if __name__ == "__main__":
    main()
