"""PR/DR watch: the witness breaks the tie between 'primary down' and 'link down'."""
import pytest

from conftest import H


@pytest.fixture
def watch(make_app, monkeypatch):
    def build(approve=True):
        app, c = make_app({"dr": {"watch": {"enabled": True, "plan": "erp", "primary_host": "192.0.2.1",
                                            "witness_url": "http://witness:8443", "after_s": 60,
                                            "approve": approve}}})
        w = app.katam["drwatch"]
        state = {"primary": True, "witness": True}
        monkeypatch.setattr("katam.drwatch.tcp_reachable", lambda h, p, timeout=3: state["primary"])
        monkeypatch.setattr(w, "witness_sees_primary", lambda: state["witness"])
        ran = []
        monkeypatch.setattr(app.katam["dr"], "run", lambda plan, mode, progress: ran.append((plan, mode)) or "ok")
        return app, c, w, state, ran
    return build


def test_primary_up(watch):
    app, c, w, state, ran = watch()
    assert w.tick(1000) == "ok"


def test_link_down_is_not_a_failover(watch):
    app, c, w, state, ran = watch()
    state.update(primary=False, witness=True)
    for t in range(0, 600, 15):
        assert w.tick(1000 + t) == "partitioned"
    assert not ran


def test_no_witness_no_action(watch):
    app, c, w, state, ran = watch()
    state.update(primary=False, witness=None)
    assert w.tick(1000) == "no_witness" and w.tick(2000) == "no_witness"
    assert not ran


def test_primary_down_waits_for_approval(watch):
    app, c, w, state, ran = watch()
    state.update(primary=False, witness=False)
    assert w.tick(1000) == "primary_unreachable"
    assert w.tick(1030) == "primary_unreachable"
    assert w.tick(1061) == "awaiting_approval"
    assert not ran
    assert c.get("/api/v1/dr/watch").json["state"] == "awaiting_approval"
    assert c.post("/api/v1/dr/watch/approve", headers=H, json={"confirm": "nope"}).status_code == 400
    r = c.post("/api/v1/dr/watch/approve", headers=H, json={"confirm": "erp"})
    assert r.status_code == 202
    app.katam["tasks"].wait(r.json["task"], 30)
    assert ran == [("erp", "disaster")]
    assert c.get("/api/v1/dr/watch").json["state"] == "failed_over"
    assert w.tick(5000) == "failed_over"       # stays put until reset after failback
    c.post("/api/v1/dr/watch/reset", headers=H)
    state.update(primary=True)
    assert w.tick(6000) == "ok"


def test_primary_back_resets_timer(watch):
    app, c, w, state, ran = watch()
    state.update(primary=False, witness=False)
    w.tick(1000)
    state.update(primary=True)
    assert w.tick(1050) == "ok"
    state.update(primary=False)
    assert w.tick(1070) == "primary_unreachable"   # counting starts again
    assert w.tick(1100) == "primary_unreachable"


def test_automatic_failover(watch):
    app, c, w, state, ran = watch(approve=False)
    state.update(primary=False, witness=False)
    w.tick(1000)
    assert w.tick(1061) == "failing_over"
    for _ in range(100):
        if ran:
            break
        import time
        time.sleep(0.05)
    assert ran == [("erp", "disaster")]


def test_follower_never_fails_over(watch):
    from katam.leader import NotLeader
    app, c, w, state, ran = watch(approve=False)

    def not_leader(action):
        raise NotLeader(action)
    w.guard = not_leader
    state.update(primary=False, witness=False)
    w.tick(1000)
    with pytest.raises(NotLeader):
        w.tick(1061)
    assert not ran
