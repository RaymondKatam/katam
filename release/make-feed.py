#!/usr/bin/env python3
"""Write releases.json, the update feed Katam controllers poll.

  release/make-feed.py --version 0.3.1 --channel stable --base https://USER.github.io/katam \
      --iso-url URL --sha256 HASH --notes-url URL --feed site/releases.json
Keeps older entries, newest first.
"""
import argparse, json, os, time

ap = argparse.ArgumentParser()
for a in ("version", "channel", "base", "iso-url", "sha256", "notes-url", "feed"):
    ap.add_argument(f"--{a}", required=a in ("version", "feed"))
ap.add_argument("--min-upgrade-from", default=None)
a = ap.parse_args()
feed = {"product": "katam", "channels": {}}
if os.path.exists(a.feed):
    with open(a.feed) as f:
        feed = json.load(f)
ch = a.channel or "stable"
entries = [e for e in feed["channels"].get(ch, []) if e["version"] != a.version]
entries.insert(0, {
    "version": a.version, "released": time.strftime("%Y-%m-%d"),
    "iso": a.iso_url, "sha256": a.sha256, "notes": a.notes_url,
    "apt": f"{a.base.rstrip('/')}/apt" if a.base else None,
    "min_upgrade_from": a.min_upgrade_from,
})
feed["channels"][ch] = entries[:30]
feed["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
os.makedirs(os.path.dirname(a.feed) or ".", exist_ok=True)
with open(a.feed, "w") as f:
    json.dump(feed, f, indent=2)
print(f"{a.feed}: {ch} -> {a.version}")
