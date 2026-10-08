"""YAML runbooks: ordered steps with checks, retries, rollback and dry runs.

name: patch-window
params:
  vm: {required: true}
steps:
  - name: stop
    action: vm.power
    args: {vm: "${params.vm}", action: shutdown}
  - name: wait-stopped
    action: wait.state
    args: {vm: "${params.vm}", state: stopped, timeout: 300}
    on_failure: rollback          # stop (default) | continue | rollback
  - name: health
    action: http.check
    args: {url: "http://172.20.20.50/healthz", expect_status: 200}
    retries: 5
    retry_delay: 10
    rollback:
      action: vm.power
      args: {vm: "${params.vm}", action: start}
"""
import base64
import json
import re
import socket
import time
import urllib.request

import yaml

from .driver import ClusterError

VAR = re.compile(r"\$\{([a-zA-Z0-9_.\-]+)\}")


class RunbookError(Exception):
    pass


def parse(source):
    try:
        rb = yaml.safe_load(source)
    except yaml.YAMLError as e:
        raise RunbookError(f"Not valid YAML: {e}")
    if not isinstance(rb, dict) or not rb.get("name") or not isinstance(rb.get("steps"), list):
        raise RunbookError("A runbook needs a name and a list of steps")
    names = set()
    for i, st in enumerate(rb["steps"]):
        if not isinstance(st, dict) or not st.get("action"):
            raise RunbookError(f"Step {i + 1} needs an action")
        st.setdefault("name", f"step{i + 1}")
        if not isinstance(st["name"], str):
            raise RunbookError(f"Step {i + 1}: put the name in quotes (YAML reads {st['name']!r} as a "
                               "boolean or number, e.g. on/off/yes/no)")
        if st["name"] in names:
            raise RunbookError(f"Duplicate step name {st['name']}")
        names.add(st["name"])
        if st["action"] not in ACTIONS:
            raise RunbookError(f"Step {st['name']}: unknown action {st['action']}. "
                               f"Available: {', '.join(sorted(ACTIONS))}")
        if st.get("on_failure", "stop") not in ("stop", "continue", "rollback"):
            raise RunbookError(f"Step {st['name']}: on_failure must be stop, continue or rollback")
    return rb


def _resolve(value, ctx):
    if isinstance(value, str):
        def sub(m):
            cur = ctx
            for part in m.group(1).split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    raise RunbookError(f"Unknown variable ${{{m.group(1)}}}")
            return str(cur)
        whole = VAR.fullmatch(value)
        if whole:  # keep non-string types when the value is just one variable
            cur = ctx
            for part in whole.group(1).split("."):
                if not (isinstance(cur, dict) and part in cur):
                    raise RunbookError(f"Unknown variable ${{{whole.group(1)}}}")
                cur = cur[part]
            return cur
        return VAR.sub(sub, value)
    if isinstance(value, list):
        return [_resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, ctx) for k, v in value.items()}
    return value


class Runner:
    """hooks: callables the app provides, all acting as the calling principal."""

    def __init__(self, hooks):
        self.h = hooks

    def plan(self, rb, params):
        params = self._params(rb, params)
        ctx = {"params": params, "steps": {s["name"]: {"output": f"<output of {s['name']}>"} for s in rb["steps"]}}
        out = []
        for st in rb["steps"]:
            try:
                args = _resolve(st.get("args", {}), ctx)
            except RunbookError as e:
                args = {"error": str(e)}
            out.append({"name": st["name"], "action": st["action"], "args": args,
                        "on_failure": st.get("on_failure", "stop"), "retries": st.get("retries", 0)})
        return out

    @staticmethod
    def _params(rb, given):
        given = dict(given or {})
        for k, spec in (rb.get("params") or {}).items():
            spec = spec or {}
            if k not in given:
                if "default" in spec:
                    given[k] = spec["default"]
                elif spec.get("required"):
                    raise RunbookError(f"Parameter {k} is required")
        return given

    def run(self, rb, params, progress):
        params = self._params(rb, params)
        ctx = {"params": params, "steps": {}}
        done = []           # steps that ran, for rollback (latest first)
        failures = []
        for st in rb["steps"]:
            args = _resolve(st.get("args", {}), ctx)
            attempts = int(st.get("retries", 0)) + 1
            err = None
            for attempt in range(1, attempts + 1):
                try:
                    progress(f"[{st['name']}] {st['action']}" + (f" (attempt {attempt})" if attempt > 1 else ""))
                    output = ACTIONS[st["action"]](self.h, args, progress)
                    ctx["steps"][st["name"]] = {"output": output, "status": "ok"}
                    done.append(st)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001 - reported per step
                    err = e.get_error_message() if hasattr(e, "get_error_message") else str(e)
                    if attempt < attempts:
                        time.sleep(float(st.get("retry_delay", 5)))
            if err is None:
                continue
            ctx["steps"][st["name"]] = {"output": None, "status": "failed", "error": err}
            progress(f"[{st['name']}] failed: {err}")
            policy = st.get("on_failure", "stop")
            if policy == "continue":
                failures.append(st["name"])
                continue
            if policy == "rollback":
                done.append(st)
                self._rollback(done, ctx, progress)
            raise RunbookError(f"Step {st['name']} failed: {err}")
        if failures:
            return f"Finished with failed steps: {', '.join(failures)}"
        return f"Runbook {rb['name']} finished: {len(rb['steps'])} steps"

    def _rollback(self, done, ctx, progress):
        for st in reversed(done):
            rb = st.get("rollback")
            if not rb:
                continue
            try:
                progress(f"[{st['name']}] rolling back with {rb['action']}")
                ACTIONS[rb["action"]](self.h, _resolve(rb.get("args", {}), ctx), progress)
            except Exception as e:  # noqa: BLE001
                progress(f"[{st['name']}] rollback failed: {e}")


# ---- actions -------------------------------------------------------------
def _vm_state(h, vm):
    for v in h["list_vms"]():
        if v["name"] == vm:
            return v["state"]
    return None


def a_vm_create(h, a, p):
    return h["create_vm"](a, p)


def a_vm_power(h, a, p):
    h["power"](a["vm"], a["action"])
    return a["action"]


def a_vm_delete(h, a, p):
    return h["delete_vm"](a["vm"], p)


def a_vm_migrate(h, a, p):
    return h["migrate_vm"](a["vm"], a["node"], p)


def a_vm_export(h, a, p):
    return h["export_vm"](a["vm"], a.get("format", "ova"), p)


def a_wait_state(h, a, p):
    end = time.time() + float(a.get("timeout", 300))
    while time.time() < end:
        st = _vm_state(h, a["vm"])
        if st == a["state"]:
            return st
        time.sleep(3)
    raise RunbookError(f"{a['vm']} did not reach {a['state']} in {a.get('timeout', 300)}s")


def a_sleep(h, a, p):
    time.sleep(float(a.get("seconds", 5)))
    return a.get("seconds", 5)


def a_http_check(h, a, p):
    req = urllib.request.Request(a["url"], method=a.get("method", "GET"))
    try:
        with urllib.request.urlopen(req, timeout=float(a.get("timeout", 10))) as r:
            status, body = r.status, r.read(65536).decode(errors="replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, ""
    if status != int(a.get("expect_status", 200)):
        raise RunbookError(f"{a['url']} returned {status}")
    if a.get("contains") and a["contains"] not in body:
        raise RunbookError(f"{a['url']} response doesn't contain {a['contains']!r}")
    return status


def a_tcp_check(h, a, p):
    with socket.create_connection((a["host"], int(a["port"])), timeout=float(a.get("timeout", 5))):
        return "open"


def a_guest_exec(h, a, p):
    """Run a command in the guest through qemu-guest-agent."""
    import libvirt_qemu
    d = h["domain"](a["vm"])
    cmd = a["command"] if isinstance(a["command"], list) else ["/bin/sh", "-c", a["command"]]
    req = {"execute": "guest-exec", "arguments": {"path": cmd[0], "arg": cmd[1:], "capture-output": True}}
    pid = json.loads(libvirt_qemu.qemuAgentCommand(d, json.dumps(req), 30, 0))["return"]["pid"]
    end = time.time() + float(a.get("timeout", 120))
    while time.time() < end:
        st = json.loads(libvirt_qemu.qemuAgentCommand(
            d, json.dumps({"execute": "guest-exec-status", "arguments": {"pid": pid}}), 30, 0))["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data", "")).decode(errors="replace")
            if st.get("exitcode", 0) != int(a.get("expect_exit", 0)):
                err = base64.b64decode(st.get("err-data", "")).decode(errors="replace")
                raise RunbookError(f"Exit code {st.get('exitcode')}: {err.strip()[-300:]}")
            return out.strip()
        time.sleep(1)
    raise RunbookError("Guest command timed out")


def a_secgroups(h, a, p):
    h["set_secgroups"](a["vm"], a["groups"])
    return a["groups"]


def a_dr(h, a, p):
    return h["dr_run"](a["plan"], a.get("mode", "test"), p)


def a_call(h, a, p):
    return h["run_runbook"](a["name"], a.get("params", {}), p)


ACTIONS = {
    "vm.create": a_vm_create, "vm.power": a_vm_power, "vm.delete": a_vm_delete,
    "vm.migrate": a_vm_migrate, "vm.export": a_vm_export, "wait.state": a_wait_state,
    "sleep": a_sleep, "http.check": a_http_check, "tcp.check": a_tcp_check,
    "guest.exec": a_guest_exec, "secgroup.apply": a_secgroups, "dr.run": a_dr,
    "runbook.call": a_call,
}

EXAMPLES = {
    "restart-with-health-check": """name: restart-with-health-check
description: Reboot a VM and confirm it answers on a TCP port afterwards
params:
  vm: {required: true}
  host: {required: true}
  port: {default: 22}
steps:
  - name: reboot
    action: vm.power
    args: {vm: "${params.vm}", action: reboot}
  - name: settle
    action: sleep
    args: {seconds: 20}
  - name: port-open
    action: tcp.check
    args: {host: "${params.host}", port: "${params.port}"}
    retries: 10
    retry_delay: 6
""",
    "export-for-vmware": """name: export-for-vmware
description: Shut a VM down, export it as an OVA for VMware, start it again
params:
  vm: {required: true}
steps:
  - name: shutdown
    action: vm.power
    args: {vm: "${params.vm}", action: shutdown}
  - name: stopped
    action: wait.state
    args: {vm: "${params.vm}", state: stopped, timeout: 600}
    on_failure: rollback
    rollback:
      action: vm.power
      args: {vm: "${params.vm}", action: start}
  - name: export
    action: vm.export
    args: {vm: "${params.vm}", format: ova}
    on_failure: continue
  - name: start-again
    action: vm.power
    args: {vm: "${params.vm}", action: start}
""",
}
