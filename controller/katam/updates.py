"""Update channel: check the release feed and roll new versions out to nodes one at a time."""
import logging
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


class Updates:
    def __init__(self, cfg, ha, nexec):
        self.cfg, self.ha, self.x = cfg, ha, nexec
        self.u = cfg.get("updates") or {}
        self._cache = (0, None)

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

    def rollout(self, progress, include_os=False, reboot=False):
        """Upgrade nodes one at a time: maintenance (evacuate), apt upgrade, optional reboot, back in service."""
        nodes = [n["name"] for n in self.cfg["nodes"] if n["site"] == "primary"]
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
                    self.x.run(node, apt + ["install", "--only-upgrade", "katam-node", "katam-archive-keyring"],
                               sudo=True, timeout=1800)
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
        return f"Upgraded {len(done)} node(s): {', '.join(done)}. Upgrade the controller with: sudo apt update && sudo apt upgrade"

    def _wait_back(self, node, progress, timeout=900):
        end = time.time() + timeout
        while time.time() < end:
            if self.ha._alive(node):
                progress(f"{node}: back online")
                return
            time.sleep(10)
        raise ClusterError(f"{node} didn't come back within {timeout // 60} minutes")
