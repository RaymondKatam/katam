"""One-click upgrade: node order, controller restarts, self-upgrade request, feed discovery."""
import json

from katam import updates as up


class FakeHA:
    def __init__(self):
        self.maint = []

    def state(self, node):
        return "ok"

    def maintenance(self, node, on, progress):
        self.maint.append((node, on))


class FakeExec:
    def __init__(self, controllers=()):
        self.calls, self.controllers = [], set(controllers)

    def run(self, node, argv, sudo=False, timeout=None, check=True, **kw):
        self.calls.append((node, argv[:3] if argv[0] != "apt-get" else argv[-1:] if "update" in argv else ["apt", argv[-4]]))
        if argv[:2] == ["systemctl", "is-enabled"]:
            return "enabled" if node in self.controllers else "disabled"
        return ""


def cfg(*hosts):
    return {"nodes": [{"name": f"hv{i + 1}", "host": h, "site": "primary"} for i, h in enumerate(hosts)],
            "updates": {}}


def test_rollout_order_restarts_and_self_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "local_addresses", lambda: {"192.0.2.12"})      # we run on hv2
    ha, x = FakeHA(), FakeExec(controllers={"hv1", "hv2", "hv3"})
    u = up.Updates(cfg("192.0.2.11", "192.0.2.12", "192.0.2.13"), ha, x, upgrade_dir=str(tmp_path))
    log = []
    msg = u.rollout(log.append)
    # this controller's own node goes last, every node goes through maintenance
    assert [n for n, on in ha.maint if on] == ["hv1", "hv3", "hv2"]
    # other nodes' controllers restart right after their packages; ours via the root path unit
    restarts = [n for n, a in x.calls if a[:2] == ["systemctl", "restart"]]
    assert restarts == ["hv1", "hv1", "hv3", "hv3"]
    req = json.loads((tmp_path / "request").read_text())
    assert req["include_os"] is False
    assert "restart" in msg


def test_rollout_installs_all_katam_packages_only_if_present(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "local_addresses", lambda: set())
    seen = []

    class X(FakeExec):
        def run(self, node, argv, **kw):
            seen.append(argv)
            return ""
    u = up.Updates(cfg("192.0.2.11"), FakeHA(), X(), upgrade_dir=str(tmp_path))
    u.rollout(lambda m: None, controller=False)
    install = next(a for a in seen if "install" in a)
    assert "--only-upgrade" in install and {"katam-node", "katam-controller", "katam-witness"} <= set(install)
    assert not (tmp_path / "request").exists()


def test_feed_from_package_file(tmp_path):
    f = tmp_path / "update-feed"
    f.write_text("https://example.org/katam/releases.json\n")
    u = up.Updates({"updates": {}}, None, None, feed_file=str(f))
    assert u.u["feed_url"] == "https://example.org/katam/releases.json"
    u2 = up.Updates({"updates": {"feed_url": "https://mine/feed.json"}}, None, None, feed_file=str(f))
    assert u2.u["feed_url"] == "https://mine/feed.json"          # config wins


def test_self_status(tmp_path):
    u = up.Updates({"updates": {}}, None, None, upgrade_dir=str(tmp_path))
    assert u.self_status() is None
    (tmp_path / "result.json").write_text('{"ok": true, "before": "0.5.1", "after": "0.5.2"}')
    assert u.self_status()["after"] == "0.5.2"
