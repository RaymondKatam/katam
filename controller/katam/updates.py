"""Update channel: check the release feed and upgrade the whole platform with one click.

One click upgrades, in order: every node one at a time (VMs move away first, Katam packages
and optionally Ubuntu updates, a reboot if asked, VMs come back), each controller that runs on
those nodes (restarted right after its packages), and finally this controller itself through
the root-owned katam-upgrade.path unit (the service can't become root on its own).
"""
import json
import logging
import os
import socket
import subprocess
import time

import requests

from . import __version__
from .driver import ClusterError

log = logging.getLogger("katam.updates")


def _v(s):
    out = []
    for part in str(s).lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


FEED_FILE = "/usr/share/katam/update-feed"          # written by packages built for a channel
UPGRADE_DIR = "/var/lib/katam/upgrade"              # katam-upgrade.path watches it
KATAM_PKGS = ["katam-node", "katam-controller", "katam-witness", "katam-archive-keyring"]


def local_addresses():
    out = {"127.0.0.1", "localhost", socket.gethostname()}
    try:
        r = subprocess.run(["ip", "-o", "addr"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) > 3:
                out.add(parts[3].split("/")[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return out


class Updates:
    def __init__(self, cfg, ha, nexec, upgrade_dir=UPGRADE_DIR, feed_file=FEED_FILE):
        self.cfg, self.ha, self.x = cfg, ha, nexec
        self.u = dict(cfg.get("updates") or {})
        self.dir = upgrade_dir
        if not self.u.get("feed_url") and os.path.exists(feed_file):
            with open(feed_file) as f:
                self.u["feed_url"] = f.read().strip() or None
        self._cache = (0, None)

    def self_status(self):
        """Result of the last self-upgrade (written by katam-upgrade.service)."""
        try:
            with open(os.path.join(self.dir, "result.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def request_self_upgrade(self, include_os=False):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, ".request")
        with open(tmp, "w") as f:
            json.dump({"requested": time.time(), "include_os": bool(include_os)}, f)
        os.replace(tmp, os.path.join(self.dir, "request"))

    def check(self, force=False):
        url = self.u.get("feed_url")
        base = {"current": __version__, "channel": self.u.get("channel", "stable"), "configured": bool(url)}
        if not url:
            return {**base, "available": False}
        ts, data = self._cache
        if force or not data or time.time() - ts > float(self.u.get("check_interval_h", 6)) * 3600:
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                data = r.json()
                self._cache = (time.time(), data)
            except Exception as e:  # noqa: BLE001 - offline sites still work
                return {**base, "available": False, "error": f"Couldn't read the update feed: {e}"}
        entries = (data.get("channels") or {}).get(base["channel"]) or []
        if not entries:
            return {**base, "available": False}
        latest = max(entries, key=lambda e: _v(e["version"]))
        newer = _v(latest["version"]) > _v(__version__)
        blocked = latest.get("min_upgrade_from") and _v(__version__) < _v(latest["min_upgrade_from"])
        return {**base, "available": newer, "latest": latest["version"], "released": latest.get("released"),
                "notes": latest.get("notes"), "iso": latest.get("iso"),
                "blocked": bool(blocked and newer),
                "blocked_reason": f"Upgrade to {latest['min_upgrade_from']} first" if blocked and newer else None}

    def rollout(self, progress, include_os=False, reboot=False, controller=True):
        """One click: nodes one at a time (evacuate, upgrade, optional reboot, return), then this
        controller. The node that hosts this controller goes last."""
        mine = local_addresses()
        nodes = [n["name"] for n in self.cfg["nodes"] if n["site"] == "primary"]
        here = [n["name"] for n in self.cfg["nodes"] if n["host"] in mine]
        nodes = [n for n in nodes if n not in here] + [n for n in nodes if n in here]
        bad = [n for n in nodes if self.ha.state(n) not in ("ok",)]
        if bad:
            raise ClusterError(f"Fix these nodes first (not ok): {', '.join(bad)}")
        progress(f"Rolling upgrade of {len(nodes)} node(s), one at a time")
        apt = ["apt-get", "-y", "-q", "-o", "Dpkg::Options::=--force-confold", "-o", "Dpkg::Options::=--force-confdef"]
        done = []
        for node in nodes:
            if len(nodes) > 1:
                progress(f"{node}: entering maintenance (moving VMs away)")
                self.ha.maintenance(node, True, progress)
            try:
                progress(f"{node}: refreshing package lists")
                self.x.run(node, ["apt-get", "update", "-q"], sudo=True, timeout=600)
                if include_os:
                    progress(f"{node}: installing all updates (OS and Katam)")
                    self.x.run(node, apt + ["upgrade"], sudo=True, timeout=3600)
                else:
                    progress(f"{node}: upgrading Katam packages")
                    self.x.run(node, apt + ["install", "--only-upgrade", *KATAM_PKGS], sudo=True, timeout=1800)
                if node not in here and self.x.run(node, ["systemctl", "is-enabled", "katam"],
                                                   check=False).strip() == "enabled":
                    progress(f"{node}: restarting its Katam controller")
                    self.x.run(node, ["systemctl", "restart", "katam"], sudo=True, check=False)
                    self.x.run(node, ["systemctl", "restart", "katam-console"], sudo=True, check=False)
                needs = self.x.run(node, ["stat", "/var/run/reboot-required"], sudo=True, check=False).strip()
                if needs and reboot:
                    progress(f"{node}: rebooting")
                    self.x.run(node, ["reboot"], sudo=True, check=False)
                    time.sleep(30)
                    self._wait_back(node, progress)
                elif needs:
                    progress(f"{node}: a reboot is needed to finish; reboot it in a window")
            except Exception:
                progress(f"{node}: upgrade failed; it stays in maintenance for you to check")
                raise
            if len(nodes) > 1:
                self.ha.maintenance(node, False, progress)
            done.append(node)
            progress(f"{node}: done")
        msg = f"Upgraded {len(done)} node(s): {', '.join(done) or 'none'}."
        if controller:
            progress("This controller: upgrading and restarting (the page reconnects by itself)")
            self.request_self_upgrade(include_os)
            msg += " This controller is upgrading and will restart in a minute."
        return msg

    def _wait_back(self, node, progress, timeout=900):
        end = time.time() + timeout
        while time.time() < end:
            if self.ha._alive(node):
                progress(f"{node}: back online")
                return
            time.sleep(10)
        raise ClusterError(f"{node} didn't come back within {timeout // 60} minutes")
