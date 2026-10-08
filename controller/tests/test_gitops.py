"""Runbooks from Git: sync, update, prune, atomic failure, webhooks, read-only managed runbooks."""
import hashlib
import hmac
import json
import shutil
import subprocess

import pytest

from conftest import H

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")

RB = """name: {name}
description: {desc}
params:
  vm: {{required: true}}
steps:
  - name: reboot
    action: vm.power
    args: {{vm: "${{params.vm}}", action: reboot}}
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    bare = tmp_path / "runbooks.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(bare))
    (work / "runbooks").mkdir()
    (work / "runbooks" / "reboot.yaml").write_text(RB.format(name="reboot-vm", desc="first"))
    (work / "runbooks" / "nested").mkdir()
    (work / "runbooks" / "nested" / "patch.yml").write_text(RB.format(name="patch-vm", desc="patch"))
    (work / "README.md").write_text("not a runbook")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "push", "-q", "origin", "main")

    def commit(msg):
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", msg)
        git(work, "push", "-q", "origin", "main")
    return {"url": f"file://{bare}", "work": work, "commit": commit}


def sync(app, c):
    r = c.post("/api/v1/gitops/sync", headers=H)
    assert r.status_code == 202, r.json
    return app.katam["tasks"].wait(r.json["task"], timeout=60)


def gcfg(repo, tmp_path, **kw):
    return {"runbooks": {"git": {"url": repo["url"], "branch": "main", "path": "runbooks",
                                 "workdir": str(tmp_path / "gitwork"), "webhook_secret": "s3cret", **kw}}}


def test_sync_update_prune(make_app, repo, tmp_path):
    app, c = make_app(gcfg(repo, tmp_path))
    t = sync(app, c)
    assert t["status"] == "done", t["log"]
    rbs = {r["name"]: r for r in c.get("/api/v1/runbooks").json}
    assert set(rbs) == {"reboot-vm", "patch-vm"}
    assert rbs["patch-vm"]["managed_by"] == "git" and rbs["patch-vm"]["path"] == "nested/patch.yml"
    st = c.get("/api/v1/gitops").json
    assert st["ok"] and st["managed"] == 2 and len(st["commit"]) == 40

    # managed runbooks are read-only in Katam
    r = c.put("/api/v1/runbooks/reboot-vm", headers=H, json={"source": RB.format(name="reboot-vm", desc="x")})
    assert r.status_code == 409 and "managed in Git" in r.json["error"]
    assert c.delete("/api/v1/runbooks/reboot-vm", headers=H).status_code == 409

    # change one, delete one, add one
    (repo["work"] / "runbooks" / "reboot.yaml").write_text(RB.format(name="reboot-vm", desc="second"))
    (repo["work"] / "runbooks" / "nested" / "patch.yml").unlink()
    (repo["work"] / "runbooks" / "drain.yaml").write_text(RB.format(name="drain-node", desc="new"))
    repo["commit"]("change")
    t = sync(app, c)
    assert t["status"] == "done", t["log"]
    st = c.get("/api/v1/gitops").json
    assert st["added"] == ["drain-node"] and st["updated"] == ["reboot-vm"] and st["removed"] == ["patch-vm"]
    rbs = {r["name"]: r for r in c.get("/api/v1/runbooks").json}
    assert set(rbs) == {"reboot-vm", "drain-node"} and rbs["reboot-vm"]["description"] == "second"

    # a runbook created in the UI stays when Git prunes
    assert c.put("/api/v1/runbooks/local-one", headers=H,
                 json={"source": RB.format(name="local-one", desc="ui")}).status_code == 200
    t = sync(app, c)
    assert "local-one" in {r["name"] for r in c.get("/api/v1/runbooks").json}


def test_one_bad_file_changes_nothing(make_app, repo, tmp_path):
    app, c = make_app(gcfg(repo, tmp_path))
    assert sync(app, c)["status"] == "done"
    (repo["work"] / "runbooks" / "reboot.yaml").write_text(RB.format(name="reboot-vm", desc="would change"))
    (repo["work"] / "runbooks" / "broken.yaml").write_text("name: broken\nsteps:\n  - action: no.such.action\n")
    repo["commit"]("broken")
    t = sync(app, c)
    assert t["status"] == "failed" and "unknown action" in t["message"]
    st = c.get("/api/v1/gitops").json
    assert st["ok"] is False and "broken.yaml" in st["error"]
    rb = next(r for r in c.get("/api/v1/runbooks").json if r["name"] == "reboot-vm")
    assert rb["description"] == "first"       # nothing half-applied


def test_duplicate_names_rejected(make_app, repo, tmp_path):
    (repo["work"] / "runbooks" / "dup.yaml").write_text(RB.format(name="reboot-vm", desc="dup"))
    repo["commit"]("dup")
    app, c = make_app(gcfg(repo, tmp_path))
    t = sync(app, c)
    assert t["status"] == "failed" and "also used by" in t["message"]


def test_webhooks(make_app, repo, tmp_path):
    app, c = make_app(gcfg(repo, tmp_path))
    c2 = app.test_client()                    # webhooks arrive without a session
    body = json.dumps({"ref": "refs/heads/main", "after": "abc"}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    hdr = {"Content-Type": "application/json", "X-GitHub-Event": "push"}

    assert c2.post("/api/v1/hooks/git", data=body, headers=hdr).status_code == 401
    assert c2.post("/api/v1/hooks/git", data=body,
                   headers={**hdr, "X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401
    r = c2.post("/api/v1/hooks/git", data=body, headers={**hdr, "X-Hub-Signature-256": sig})
    assert r.status_code == 202 and r.json["task"]
    assert app.katam["tasks"].wait(r.json["task"], 60)["status"] == "done"

    other = json.dumps({"ref": "refs/heads/feature"}).encode()
    sig2 = "sha256=" + hmac.new(b"s3cret", other, hashlib.sha256).hexdigest()
    r = c2.post("/api/v1/hooks/git", data=other, headers={**hdr, "X-Hub-Signature-256": sig2})
    assert r.status_code == 202 and "ignored" in r.json

    # GitLab: shared token header
    r = c2.post("/api/v1/hooks/git", data=body, headers={"Content-Type": "application/json",
                                                          "X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "s3cret"})
    assert r.status_code == 202
    app.katam["tasks"].wait(r.json["task"], 60)
    # Gitea / Forgejo: bare hex HMAC
    r = c2.post("/api/v1/hooks/git", data=body, headers={"Content-Type": "application/json",
                                                          "X-Gitea-Signature": sig[7:]})
    assert r.status_code == 202
    app.katam["tasks"].wait(r.json["task"], 60)
    # GitHub's ping when the webhook is created
    ping = b'{"zen":"hi"}'
    r = c2.post("/api/v1/hooks/git", data=ping, headers={"X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", ping, hashlib.sha256).hexdigest()})
    assert r.status_code == 200 and r.json["pong"]


def test_validate_endpoint(make_app):
    app, c = make_app()
    good = RB.format(name="ci-check", desc="from CI")
    r = c.post("/api/v1/runbooks/validate", headers=H, json={"source": good})
    assert r.status_code == 200 and r.json["steps"] == 1
    r = c.post("/api/v1/runbooks/validate", headers=H, json={"source": good, "params": {"vm": "test"}})
    assert r.json["plan"][0]["args"] == {"vm": "test", "action": "reboot"}
    r = c.post("/api/v1/runbooks/validate", headers=H, json={"source": "name: x\nsteps: []\n"})
    assert r.status_code == 400
    r = c.post("/api/v1/runbooks/validate", headers=H, json={"source": RB.format(name="Bad_Name", desc="")})
    assert r.status_code == 400


def test_cli_lint(tmp_path, capsys):
    from katam import cli
    (tmp_path / "a.yaml").write_text(RB.format(name="one", desc=""))
    (tmp_path / "b.yaml").write_text(RB.format(name="two", desc=""))
    cli.lint([str(tmp_path)])
    assert "2 valid, 0 failed" in capsys.readouterr().out
    (tmp_path / "c.yaml").write_text("name: three\nsteps:\n  - name: on\n    action: sleep\n")
    with pytest.raises(SystemExit) as e:
        cli.lint([str(tmp_path)])
    assert e.value.code == 1
