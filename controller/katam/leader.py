"""Control-plane leadership: which controller runs the cluster-wide services.

Three modes (cluster.mode in config.yaml):

  single    One controller. It is always the leader (the default).
  postgres  Several controllers share one PostgreSQL; a session advisory lock picks the leader.
            Simple, but that PostgreSQL is a single point of failure.
  etcd      Every controller is an etcd client. The leader holds a key bound to a short lease
            (lease_ttl_s, default 10 s) and renews it; if it stops renewing, the key expires and
            another controller wins the next election. Each win gets a new, higher "term" (the
            etcd revision that created the key). Use it with replicated PostgreSQL (Patroni).

Safety rules, so two controllers never act as leader at the same time:
  - a leader that can't renew its lease steps down after 60 % of the TTL, before the lease
    can expire and another controller can be elected;
  - actions that can't be undone (fencing a node, restarting its VMs elsewhere, DR failover)
    call ensure() first, which re-reads the leader key from etcd and checks the term;
  - only the leader holds the virtual IP (cluster.vip), so users always reach the leader.
"""
import base64
import json
import logging
import os
import socket
import subprocess
import threading
import time

import requests

from . import __version__

log = logging.getLogger("katam.leader")


class NotLeader(Exception):
    pass


def _b64(s):
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


def _unb64(s):
    return base64.b64decode(s).decode()


class Etcd:
    """Minimal etcd v3 client over the built-in JSON gateway (no extra Python packages)."""

    def __init__(self, ecfg):
        self.endpoints = [e.rstrip("/") for e in ecfg.get("endpoints") or []]
        if not self.endpoints:
            raise ValueError("cluster.etcd.endpoints is empty")
        self.s = requests.Session()
        # etcd is on the management network: never route it through an HTTP proxy, and don't
        # let REQUESTS_CA_BUNDLE from the environment replace the cluster CA.
        self.s.trust_env = False
        if ecfg.get("ca"):
            self.s.verify = ecfg["ca"]
        if ecfg.get("cert") and ecfg.get("key"):
            self.s.cert = (ecfg["cert"], ecfg["key"])
        self.timeout = float(ecfg.get("timeout_s", 2.5))
        self._i = 0

    def post(self, path, body):
        last = None
        for k in range(len(self.endpoints)):
            ep = self.endpoints[(self._i + k) % len(self.endpoints)]
            try:
                r = self.s.post(f"{ep}/v3/{path}", json=body, timeout=self.timeout)
                if r.status_code == 200:
                    self._i = (self._i + k) % len(self.endpoints)
                    return r.json()
                last = f"{ep}: HTTP {r.status_code} {r.text[:200]}"
            except requests.RequestException as e:
                last = f"{ep}: {e}"
        raise ConnectionError(f"No etcd endpoint answered ({last})")

    def grant(self, ttl):
        return self.post("lease/grant", {"TTL": int(ttl)})["ID"]

    def keepalive(self, lease):
        r = self.post("lease/keepalive", {"ID": lease}).get("result", {})
        return int(r.get("TTL", 0))           # 0 or missing: the lease has expired

    def revoke(self, lease):
        self.post("lease/revoke", {"ID": lease})

    def put(self, key, value, lease=None):
        body = {"key": _b64(key), "value": _b64(value)}
        if lease:
            body["lease"] = lease
        return self.post("kv/put", body)

    def get(self, key):
        kvs = self.post("kv/range", {"key": _b64(key)}).get("kvs") or []
        return kvs[0] if kvs else None

    def prefix(self, prefix):
        end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        return self.post("kv/range", {"key": _b64(prefix), "range_end": _b64(end)}).get("kvs") or []

    def create_if_absent(self, key, value, lease):
        """Atomic: write key only if it doesn't exist. Returns (created, current_kv)."""
        r = self.post("kv/txn", {
            "compare": [{"key": _b64(key), "target": "CREATE", "result": "EQUAL", "create_revision": "0"}],
            "success": [{"request_put": {"key": _b64(key), "value": _b64(value), "lease": lease}}],
            "failure": [{"request_range": {"key": _b64(key)}}]})
        if r.get("succeeded"):
            return True, {"create_revision": r["header"]["revision"], "value": _b64(value)}
        kvs = r["responses"][0]["response_range"].get("kvs") or [None]
        return False, kvs[0]

    def delete_if(self, key, create_revision):
        self.post("kv/txn", {
            "compare": [{"key": _b64(key), "target": "CREATE", "result": "EQUAL",
                         "create_revision": str(create_revision)}],
            "success": [{"request_delete_range": {"key": _b64(key)}}]})

    def health(self):
        out = []
        for ep in self.endpoints:
            try:
                r = self.s.get(f"{ep}/health", timeout=self.timeout)
                out.append({"endpoint": ep, "healthy": r.ok and r.json().get("health") in ("true", True)})
            except (requests.RequestException, ValueError) as e:
                out.append({"endpoint": ep, "healthy": False, "error": str(e)[:120]})
        return out


class Leadership:
    def __init__(self, cfg, db=None, vip_cmd=None):
        c = cfg.get("cluster") or {}
        self.mode = c.get("mode", "single")
        self.member = c.get("member") or socket.gethostname()
        self.url = c.get("advertise_url") or f"http://{socket.gethostname()}:{cfg['controller']['port']}"
        self.db = db
        self.vip = (c.get("vip") or {}) if (c.get("vip") or {}).get("address") else None
        self.vip_cmd = vip_cmd or (self.vip or {}).get("helper") or ["/usr/lib/katam/katam-vip"]
        if isinstance(self.vip_cmd, str):
            self.vip_cmd = self.vip_cmd.split()
        self.ttl = float((c.get("etcd") or {}).get("lease_ttl_s", 10))
        self.prefix = ((c.get("etcd") or {}).get("prefix") or "/katam").rstrip("/")
        self._leader = self.mode == "single"
        self.term = 1 if self._leader else 0
        self.since = time.time() if self._leader else None
        self.current = {"member": self.member, "url": self.url, "term": self.term} if self._leader else None
        self.last_error = None
        self._elected, self._demoted = [], []
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._lease = None
        self._last_ok = 0.0
        self._hold_until = 0.0
        self._pg = None
        self.etcd = Etcd(c.get("etcd") or {}) if self.mode == "etcd" else None
        if self.etcd:                         # one slow endpoint must not eat the whole lease
            self.etcd.timeout = min(self.etcd.timeout, max(self.ttl / 5, 0.5))
        if self.mode not in ("single", "postgres", "etcd"):
            raise ValueError("cluster.mode must be single, postgres or etcd")

    # ---- public ------------------------------------------------------------
    def is_leader(self):
        return self._leader

    def on_elected(self, fn):
        self._elected.append(fn)
        if self._leader:
            threading.Thread(target=self._safe, args=(fn,), daemon=True).start()

    def on_demoted(self, fn):
        self._demoted.append(fn)

    def ensure(self, action="this action"):
        """Raise NotLeader unless this controller is still the leader, checked at the source."""
        if not self.still_leader():
            raise NotLeader(f"This controller is no longer the leader; {action} was not carried out")
        return self.term

    def still_leader(self):
        if self.mode == "single":
            return True
        if not self._leader:
            return False
        if self.mode == "postgres":
            return self._pg_check()
        if time.time() - self._last_ok > self.ttl * 0.6:
            return False
        try:
            kv = self.etcd.get(self._key("leader"))
        except ConnectionError:
            return False
        return bool(kv) and int(kv["create_revision"]) == self.term and \
            json.loads(_unb64(kv["value"])).get("member") == self.member

    def info(self):
        out = {"mode": self.mode, "member": self.member, "role": "leader" if self._leader else "follower",
               "term": self.term if self._leader else (self.current or {}).get("term"),
               "leader": (self.current or {}).get("member"), "leader_url": (self.current or {}).get("url"),
               "since": self.since, "lease_ttl_s": self.ttl if self.mode == "etcd" else None,
               "vip": (self.vip or {}).get("address"), "last_error": self.last_error, "members": []}
        if self.mode == "etcd":
            try:
                out["members"] = sorted((json.loads(_unb64(kv["value"])) for kv in
                                         self.etcd.prefix(self._key("members/"))), key=lambda m: m["member"])
            except (ConnectionError, ValueError) as e:
                out["last_error"] = str(e)
            out["etcd"] = self.etcd.health()
        else:
            out["members"] = [{"member": self.member, "url": self.url, "version": __version__}]
        return out

    def members(self):
        if self.mode != "etcd":
            return {self.member}
        try:
            return {json.loads(_unb64(kv["value"]))["member"] for kv in self.etcd.prefix(self._key("members/"))}
        except (ConnectionError, ValueError, KeyError):
            return None                       # unknown: callers must not act on it

    def start(self):
        if self.mode == "single":
            self._vip(True)
            return
        self._vip(False)                      # never start up holding the VIP
        target = self._etcd_loop if self.mode == "etcd" else self._pg_loop
        threading.Thread(target=target, name="katam-leader", daemon=True).start()
        if self.mode == "etcd":
            threading.Thread(target=self._watchdog, name="katam-leader-watchdog", daemon=True).start()

    def _watchdog(self):
        """Step down on time even while an etcd call is blocked: the lease may expire on the
        etcd side ttl seconds after the last renewal, so stop acting well before that."""
        while not self._stop.is_set():
            if self._leader and time.time() - self._last_ok > self.ttl * 0.6:
                self._demote("lease renewal overdue")
            self._stop.wait(0.2)

    def resign(self, hold_s=None):
        """Hand leadership to another controller (planned maintenance of this one)."""
        if self.mode == "single":
            raise NotLeader("Single-controller mode has nobody to hand over to")
        if not self._leader:
            raise NotLeader("This controller isn't the leader")
        self._hold_until = time.time() + (hold_s if hold_s is not None else self.ttl * 3)
        if self.mode == "etcd":
            try:
                self.etcd.delete_if(self._key("leader"), self.term)
            except ConnectionError:
                pass
        elif self._pg is not None:
            try:
                self._pg.cursor().execute("SELECT pg_advisory_unlock(424242)")
            except Exception:  # noqa: BLE001
                pass
        self._demote("resigned (step down requested)")

    def stop(self):
        """Step down cleanly (service stop): release the key so another controller takes over now."""
        self._stop.set()
        if self.mode == "etcd" and self._lease:
            try:
                self.etcd.revoke(self._lease)
            except ConnectionError:
                pass
        self._demote("controller stopping")

    # ---- transitions -----------------------------------------------------------
    def _key(self, k):
        return f"{self.prefix}/{k}"

    def _safe(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("leadership callback failed")

    def _promote(self, term):
        with self._lock:
            if self._leader:
                return
            self._leader, self.term, self.since = True, int(term), time.time()
            self.current = {"member": self.member, "url": self.url, "term": self.term}
        log.warning("elected leader (term %s)", self.term)
        if self.db:
            self.db.audit(f"controller:{self.member}", "leader.elected", self.member, "ok", {"term": self.term})
        self._vip(True)
        for fn in self._elected:
            threading.Thread(target=self._safe, args=(fn,), daemon=True).start()

    def _demote(self, why):
        with self._lock:
            if not self._leader:
                return
            self._leader, self.since = False, None
            if self.current and self.current.get("member") == self.member:
                self.current = None           # until etcd says who leads now
        log.warning("stepped down as leader: %s", why)
        self._vip(False)
        for fn in self._demoted:
            self._safe(fn)
        if self.db:
            try:
                self.db.audit(f"controller:{self.member}", "leader.stepped_down", self.member, "ok", why)
            except Exception:  # noqa: BLE001  (database may be the reason)
                pass

    def _vip(self, up):
        if not self.vip:
            return
        argv = [*self.vip_cmd, "add" if up else "del", self.vip["address"], self.vip["interface"]]
        try:
            subprocess.run(argv, check=True, capture_output=True, timeout=15)
            log.info("virtual IP %s %s", self.vip["address"], "added" if up else "removed")
        except (subprocess.SubprocessError, OSError) as e:
            self.last_error = f"virtual IP {'add' if up else 'remove'} failed: {e}"
            log.error(self.last_error)

    # ---- etcd ---------------------------------------------------------------------
    def _register(self, started):
        key = self._key(f"members/{self.member}")
        cur = self.etcd.get(key)
        if cur and str(cur.get("lease")) != str(self._lease):
            other = json.loads(_unb64(cur["value"]))
            if other.get("url") != self.url:
                log.error("another controller (%s) also calls itself %s: give each one its own "
                          "cluster.member", other.get("url"), self.member)
        self.etcd.put(key, json.dumps({"member": self.member, "url": self.url, "version": __version__,
                                       "started": started}), lease=self._lease)
        self._registered = time.time()

    def _etcd_loop(self):
        tick = max(self.ttl / 4, 0.5)
        me = json.dumps({"member": self.member, "url": self.url})
        started = time.time()
        self._registered = 0.0
        while not self._stop.is_set():
            try:
                if not self._lease:
                    self._lease = self.etcd.grant(self.ttl)
                    self._last_ok = time.time()
                    self._register(started)
                elif self.etcd.keepalive(self._lease) > 0:
                    self._last_ok = time.time()
                    if time.time() - self._registered > 30:
                        self._register(started)      # re-assert membership now and then
                else:                         # expired while we were away
                    self._lease = None
                    self._demote("lease expired")
                    continue
                if time.time() < self._hold_until:   # just resigned: let someone else win
                    kv = self.etcd.get(self._key("leader"))
                    if kv:
                        self.current = {**json.loads(_unb64(kv["value"])), "term": int(kv["create_revision"])}
                    self._stop.wait(tick)
                    continue
                created, kv = self.etcd.create_if_absent(self._key("leader"), me, self._lease)
                if created:
                    self._promote(kv["create_revision"])
                else:
                    cur = json.loads(_unb64(kv["value"])) if kv else None
                    if cur and cur.get("member") == self.member and str(kv.get("lease")) == str(self._lease):
                        self._promote(kv["create_revision"])  # ours (e.g. after a brief blip)
                    elif self._leader:
                        self._demote(f"{cur and cur.get('member')} holds the leader key")
                    if cur:
                        self.current = {**cur, "term": int(kv["create_revision"])}
                self.last_error = None
            except (ConnectionError, ValueError, KeyError) as e:
                self.last_error = str(e)
                log.warning("etcd: %s", e)
                if self._leader and time.time() - self._last_ok > self.ttl * 0.6:
                    self._demote("can't reach etcd to renew the lease")
                if time.time() - self._last_ok > self.ttl:
                    self._lease = None
            if self._leader and time.time() - self._last_ok > self.ttl * 0.6:
                self._demote("lease renewal overdue")
            self._stop.wait(tick)

    # ---- postgres -------------------------------------------------------------------
    def _pg_loop(self):
        while not self._stop.is_set():
            if time.time() < self._hold_until:
                self._stop.wait(1)
                continue
            try:
                if self._pg is None:
                    self._pg = self.db.engine.raw_connection()
                cur = self._pg.cursor()
                if not self._leader:
                    cur.execute("SELECT pg_try_advisory_lock(424242)")
                    if cur.fetchone()[0]:
                        self._promote(int(time.time()))
                    else:
                        self.current = None
                else:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                self._demote(f"database connection lost: {e}")
                try:
                    self._pg.close()
                except Exception:  # noqa: BLE001
                    pass
                self._pg = None
            self._stop.wait(5)

    def _pg_check(self):
        try:
            cur = self._pg.cursor()
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=424242 "
                        "AND pid=pg_backend_pid() AND granted")
            return cur.fetchone()[0] > 0
        except Exception:  # noqa: BLE001
            return False


def vip_helper_main(argv=None):
    """/usr/lib/katam/katam-vip add|del ADDR/PREFIX IFACE. katam.service grants the controller
    CAP_NET_ADMIN and CAP_NET_RAW for exactly this; nothing else runs as root."""
    import ipaddress
    import sys
    argv = argv or sys.argv[1:]
    if len(argv) != 3 or argv[0] not in ("add", "del"):
        raise SystemExit("usage: katam-vip add|del ADDR/PREFIX IFACE")
    op, addr, iface = argv
    ipaddress.ip_interface(addr)
    if not iface.replace("-", "").replace(".", "").replace("_", "").isalnum() or len(iface) > 15:
        raise SystemExit("bad interface name")
    if op == "add":
        subprocess.run(["ip", "addr", "replace", addr, "dev", iface], check=True)
        ip = addr.split("/")[0]
        if ":" not in ip and os.path.exists("/usr/bin/arping"):
            subprocess.run(["arping", "-U", "-c", "3", "-I", iface, ip], capture_output=True)
    else:
        subprocess.run(["ip", "addr", "del", addr, "dev", iface], capture_output=True)
