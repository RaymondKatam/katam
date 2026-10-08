"""Runbooks in Git (GitOps).

The repository is the source of truth: each *.yaml / *.yml file under runbooks.git.path is one
runbook. Katam pulls the branch on a timer and when the Git server calls the webhook
(POST /api/v1/hooks/git), validates every file, and then applies all of them together. One bad
file fails the whole sync and leaves the previous runbooks in place, so a typo never
half-applies. Runbooks that came from Git are read-only in the UI and API; change them with a
commit. With prune on, a runbook deleted from the repository is deleted from Katam.

CI pipelines use the same checks before merging (`katamctl runbook lint`, or the server-side
POST /api/v1/runbooks/validate), then the merge triggers the sync.
"""
import glob
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import threading
import time

from . import runbooks as rbmod
from .db import delete, insert, runbook_origins, runbooks, select, update

log = logging.getLogger("katam.gitops")
STATUS_KEY = "gitops.runbooks"


class GitError(Exception):
    pass


def verify_signature(secret, body, headers):
    """GitHub/Gitea/Forgejo (HMAC-SHA256 of the body) or GitLab (shared token)."""
    if not secret:
        return False
    sig = headers.get("X-Hub-Signature-256") or ""
    if sig.startswith("sha256="):
        want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want)
    gitea = headers.get("X-Gitea-Signature") or headers.get("X-Forgejo-Signature")
    if gitea:
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(gitea, want)
    gl = headers.get("X-Gitlab-Token")
    if gl:
        return hmac.compare_digest(gl, secret)
    return False


def pushed_branch(payload):
    ref = (payload or {}).get("ref") or ""
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None


def load_dir(root):
    """Parse every runbook file under root. Returns {name: (relpath, source, parsed)}; raises
    RunbookError listing every problem at once."""
    found, errors = {}, []
    files = sorted(glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True) +
                   glob.glob(os.path.join(root, "**", "*.yml"), recursive=True))
    for f in files:
        rel = os.path.relpath(f, root)
        if any(part.startswith(".") for part in rel.split(os.sep)):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                src = fh.read()
            rb = rbmod.parse(src)
            name = rb["name"]
            if not rbmod.NAME_RE.match(name):
                raise rbmod.RunbookError(f"name {name!r} must be lowercase letters, digits and dashes")
            if name in found:
                raise rbmod.RunbookError(f"name {name!r} is also used by {found[name][0]}")
            found[name] = (rel, src, rb)
        except rbmod.RunbookError as e:
            errors.append(f"{rel}: {e}")
    if errors:
        raise rbmod.RunbookError("; ".join(errors))
    return found


class GitOps:
    def __init__(self, cfg, db):
        self.g = cfg["runbooks"]["git"]
        self.db = db
        self.enabled = bool(self.g.get("url"))
        self.workdir = self.g.get("workdir") or "/var/lib/katam/gitops/runbooks"
        self._lock = threading.Lock()

    gate = staticmethod(lambda: True)

    # ---- git ---------------------------------------------------------------------
    def _env(self):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        if self.g.get("ssh_key"):
            env["GIT_SSH_COMMAND"] = (f"ssh -i {self.g['ssh_key']} -o IdentitiesOnly=yes "
                                      "-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
        return env

    def _url(self):
        url = self.g["url"]
        tf = self.g.get("token_file")
        if tf and url.startswith("https://"):
            with open(tf) as fh:
                tok = fh.read().strip()
            url = "https://oauth2:" + tok + "@" + url[len("https://"):]
        return url

    def _git(self, *args, cwd=None, timeout=120):
        r = subprocess.run(["git", *args], cwd=cwd, env=self._env(), capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().replace(self._url(), self.g["url"])
            raise GitError(f"git {args[0]} failed: {msg[-500:]}")
        return r.stdout.strip()

    def fetch(self):
        """Bring the work tree to the tip of the branch. Returns the commit id."""
        branch = self.g.get("branch") or "main"
        if not os.path.isdir(os.path.join(self.workdir, ".git")):
            shutil.rmtree(self.workdir, ignore_errors=True)
            os.makedirs(os.path.dirname(self.workdir), exist_ok=True)
            self._git("clone", "--depth", "1", "--branch", branch, "--single-branch", self._url(), self.workdir)
        else:
            self._git("remote", "set-url", "origin", self._url(), cwd=self.workdir)
            self._git("fetch", "--depth", "1", "origin", branch, cwd=self.workdir)
            self._git("reset", "--hard", "FETCH_HEAD", cwd=self.workdir)
            self._git("clean", "-fdx", cwd=self.workdir)
        return self._git("rev-parse", "HEAD", cwd=self.workdir)

    # ---- sync ------------------------------------------------------------------------
    def status(self):
        st = self.db.kv_get(STATUS_KEY, {}) or {}
        managed = self.db.all(select(runbook_origins).where(runbook_origins.c.origin == "git"))
        return {"enabled": self.enabled, "url": self.g.get("url"), "branch": self.g.get("branch"),
                "path": self.g.get("path"), "interval_s": self.g.get("interval_s"),
                "webhook": bool(self.g.get("webhook_secret")), "managed": len(managed), **st}

    def managed(self, name):
        r = self.db.one(select(runbook_origins).where(runbook_origins.c.name == name))
        return r if r and r["origin"] == "git" else None

    def sync(self, progress=lambda m: None, actor="gitops"):
        if not self.enabled:
            raise GitError("runbooks.git.url isn't set in config.yaml")
        with self._lock:
            started = time.time()
            try:
                progress(f"Fetching {self.g['url']} ({self.g.get('branch') or 'main'})")
                commit = self.fetch()
                root = os.path.normpath(os.path.join(self.workdir, self.g.get("path") or "."))
                if os.path.commonpath([root, os.path.normpath(self.workdir)]) != os.path.normpath(self.workdir):
                    raise GitError("runbooks.git.path points outside the repository")
                if not os.path.isdir(root):
                    raise GitError(f"{self.g.get('path')} isn't a folder in the repository")
                found = load_dir(root)
                progress(f"Commit {commit[:10]}: {len(found)} valid runbook(s)")
                result = self._apply(found, commit)
            except (GitError, rbmod.RunbookError, subprocess.TimeoutExpired, OSError) as e:
                st = {"last_attempt": started, "ok": False, "error": str(e)}
                self.db.kv_set(STATUS_KEY, {**(self.db.kv_get(STATUS_KEY, {}) or {}), **st})
                self.db.audit(actor, "runbook.sync", self.g.get("url") or "git", "failed", str(e))
                raise
            st = {"last_attempt": started, "last_sync": time.time(), "ok": True, "error": None,
                  "commit": commit, **result}
            self.db.kv_set(STATUS_KEY, st)
            self.db.audit(actor, "runbook.sync", self.g["url"], "ok", {"commit": commit, **result})
            progress(f"Added {len(result['added'])}, updated {len(result['updated'])}, "
                     f"removed {len(result['removed'])}, unchanged {result['unchanged']}")
            return st

    def _apply(self, found, commit):
        now = time.time()
        added, updated, removed, unchanged = [], [], [], 0
        existing = {r["name"]: r for r in self.db.all(select(runbooks))}
        origins = {r["name"]: r for r in self.db.all(select(runbook_origins))}
        with self.db.engine.begin() as c:
            for name, (rel, src, rb) in found.items():
                row = {"description": (rb.get("description") or "")[:255], "source": src, "updated": now}
                if name in existing:
                    if existing[name]["source"] != src:
                        c.execute(update(runbooks).where(runbooks.c.name == name).values(**row))
                        updated.append(name)
                    else:
                        unchanged += 1
                else:
                    c.execute(insert(runbooks).values(name=name, **row))
                    added.append(name)
                orow = {"origin": "git", "commit": commit, "path": rel, "synced": now}
                if name in origins:
                    c.execute(update(runbook_origins).where(runbook_origins.c.name == name).values(**orow))
                else:
                    c.execute(insert(runbook_origins).values(name=name, **orow))
            if self.g.get("prune", True):
                for name, o in origins.items():
                    if o["origin"] == "git" and name not in found:
                        c.execute(delete(runbooks).where(runbooks.c.name == name))
                        c.execute(delete(runbook_origins).where(runbook_origins.c.name == name))
                        removed.append(name)
        return {"added": added, "updated": updated, "removed": removed, "unchanged": unchanged}

    # ---- timer ------------------------------------------------------------------------
    def start(self):
        if not self.enabled:
            return

        def loop():
            time.sleep(10)
            while True:
                if self.gate():
                    try:
                        self.sync(actor="gitops-timer")
                    except Exception as e:  # noqa: BLE001
                        log.warning("runbook sync failed: %s", e)
                time.sleep(max(30, float(self.g.get("interval_s") or 300)))
        threading.Thread(target=loop, name="katam-gitops", daemon=True).start()


def _json(body):
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}
