"""PR/DR watch: decide when the DR site should take over, with the witness as tie-breaker.

Runs on the DR site's controller (on its leader, when the site has several controllers).
Every interval_s it checks the primary site's controller address:

  primary answers                         -> ok
  primary silent, witness reaches it      -> partitioned: the link between the sites is down,
                                             the primary is fine; do nothing (no split-brain)
  primary silent, witness can't answer    -> no second opinion; do nothing
  primary silent, witness can't reach it  -> primary down; after after_s seconds either wait for
                                             an admin to approve (approve: true, the default) or
                                             run the DR plan in disaster mode automatically
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

from .ha import tcp_reachable

log = logging.getLogger("katam.drwatch")
KEY = "dr.watch"


class DRWatch:
    gate = staticmethod(lambda: True)
    guard = staticmethod(lambda action: None)

    def __init__(self, cfg, db, dr, tasks):
        self.w = cfg["dr"]["watch"]
        self.db, self.dr, self.tasks = db, dr, tasks
        self.enabled = bool(self.w.get("enabled"))
        self._down_since = None

    def status(self):
        st = self.db.kv_get(KEY, {}) or {}
        return {"enabled": self.enabled, "plan": self.w.get("plan"), "primary": self.w.get("primary_host"),
                "after_s": self.w.get("after_s"), "approve": self.w.get("approve", True),
                "state": st.get("state", "ok" if self.enabled else "disabled"), **st}

    def _set(self, state, detail):
        cur = self.db.kv_get(KEY, {}) or {}
        if cur.get("state") != state:
            self.db.audit("dr-watch", "dr.watch", self.w.get("primary_host") or "primary", state, detail)
            log.warning("DR watch: %s (%s)", state, detail)
            cur = {"state": state, "since": time.time()}
        self.db.kv_set(KEY, {**cur, "detail": detail, "checked": time.time()})

    def witness_sees_primary(self):
        q = urllib.parse.urlencode({"host": self.w["primary_host"], "port": int(self.w.get("primary_port", 8080))})
        req = urllib.request.Request(f"{self.w['witness_url'].rstrip('/')}/v1/probe?{q}",
                                     headers={"Authorization": f"Bearer {self.w.get('witness_token') or ''}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return bool(json.loads(r.read())["reachable"])
        except Exception as e:  # noqa: BLE001
            log.warning("witness unreachable: %s", e)
            return None

    def tick(self, now=None):
        now = now or time.time()
        st = (self.db.kv_get(KEY, {}) or {}).get("state")
        if st in ("failing_over", "failed_over"):
            return st
        if tcp_reachable(self.w["primary_host"], int(self.w.get("primary_port", 8080))):
            self._down_since = None
            self._set("ok", "Primary site controller answers")
            return "ok"
        seen = self.witness_sees_primary()
        if seen is None:
            self._down_since = None
            self._set("no_witness", "Primary not answering and the witness can't be reached; not acting")
            return "no_witness"
        if seen:
            self._down_since = None
            self._set("partitioned", "Witness still reaches the primary: the link between sites is down. "
                                     "Not failing over.")
            return "partitioned"
        self._down_since = self._down_since or now
        down = now - self._down_since
        if down < float(self.w.get("after_s", 120)):
            self._set("primary_unreachable", f"Primary and witness agree it's down for {int(down)}s")
            return "primary_unreachable"
        if self.w.get("approve", True):
            self._set("awaiting_approval", "Primary site is down (confirmed by the witness). "
                                           "Approve the failover to start the DR plan.")
            return "awaiting_approval"
        self.failover("dr-watch")
        return "failing_over"

    def failover(self, actor):
        self.guard("DR failover")
        plan = self.w["plan"]
        self._set("failing_over", f"Running DR plan {plan} in disaster mode (started by {actor})")
        self.db.audit(actor, "dr.failover", plan, "ok", "started")

        def go(progress):
            msg = self.dr.run(plan, "disaster", progress)
            self._set("failed_over", f"DR plan {plan} finished: {msg}")
            return msg
        return self.tasks.submit("dr.failover", f"dr:{plan}", go, actor=actor)

    def approve(self, actor):
        st = (self.db.kv_get(KEY, {}) or {}).get("state")
        if st != "awaiting_approval":
            raise ValueError(f"Nothing to approve: the DR watch state is {st or 'ok'}")
        return self.failover(actor)

    def reset(self, actor):
        """After failback: start watching again."""
        self._down_since = None
        self.db.kv_set(KEY, {"state": "ok", "since": time.time(), "detail": f"Reset by {actor}"})
        self.db.audit(actor, "dr.watch", "reset", "ok")

    def start(self):
        if not self.enabled:
            return

        def loop():
            while True:
                time.sleep(float(self.w.get("interval_s", 15)))
                if not self.gate():
                    self._down_since = None
                    continue
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("DR watch tick failed")
        threading.Thread(target=loop, name="katam-drwatch", daemon=True).start()
