"""Leader election against a real etcd: one leader, fencing checks, failover, step-down."""
import time
import uuid

import pytest

from katam.leader import Etcd, Leadership, NotLeader


def cfg(url, member, ttl=3, prefix=None):
    return {"controller": {"port": 8080},
            "cluster": {"mode": "etcd", "member": member, "advertise_url": f"http://{member}:8080",
                        "etcd": {"endpoints": [url], "lease_ttl_s": ttl, "prefix": prefix},
                        "vip": {}}}


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def prefix():
    return f"/test-{uuid.uuid4().hex[:8]}"


def test_single_mode_is_always_leader():
    lead = Leadership({"controller": {"port": 8080}, "cluster": {"mode": "single", "vip": {}}})
    assert lead.is_leader() and lead.still_leader()
    assert lead.ensure() == 1
    with pytest.raises(NotLeader):
        lead.resign()


def test_one_leader_and_failover(etcd, prefix):
    a = Leadership(cfg(etcd, "a", prefix=prefix))
    b = Leadership(cfg(etcd, "b", prefix=prefix))
    a.start()
    assert wait_for(a.is_leader)
    b.start()
    assert wait_for(lambda: b.current and b.current["member"] == "a")
    assert not b.is_leader()
    term_a = a.ensure("fence")
    with pytest.raises(NotLeader):
        b.ensure("fence")                     # a follower can never fence
    assert {m["member"] for m in a.info()["members"]} == {"a", "b"}

    # a crashes without releasing anything: b takes over once the lease expires
    a._stop.set()
    t0 = time.time()
    assert wait_for(b.is_leader, timeout=10)
    assert time.time() - t0 < 3 * 1.6
    assert b.term > term_a                    # every election raises the term
    assert not a.still_leader()               # the old leader's checks fail
    b.stop()


def test_stop_hands_over_immediately(etcd, prefix):
    a = Leadership(cfg(etcd, "a", ttl=10, prefix=prefix))
    b = Leadership(cfg(etcd, "b", ttl=10, prefix=prefix))
    a.start()
    assert wait_for(a.is_leader)
    b.start()
    time.sleep(0.5)
    t0 = time.time()
    a.stop()                                  # revokes the lease
    assert wait_for(b.is_leader, timeout=6)
    assert time.time() - t0 < 4               # well under the 10 s TTL
    b.stop()


def test_resign_lets_another_win(etcd, prefix):
    a = Leadership(cfg(etcd, "a", prefix=prefix))
    b = Leadership(cfg(etcd, "b", prefix=prefix))
    a.start()
    assert wait_for(a.is_leader)
    b.start()
    time.sleep(0.5)
    a.resign()
    assert not a.is_leader()
    assert wait_for(b.is_leader, timeout=6)
    assert wait_for(lambda: a.current and a.current["member"] == "b")
    a.stop()
    b.stop()


def test_unreachable_etcd_steps_down_before_lease_expiry(etcd, prefix):
    a = Leadership(cfg(etcd, "a", ttl=4, prefix=prefix))
    a.start()
    assert wait_for(a.is_leader)
    a.etcd.endpoints = ["http://127.0.0.1:9"]   # partition this controller from etcd
    t0 = time.time()
    assert wait_for(lambda: not a.is_leader(), timeout=6)
    assert time.time() - t0 < 4                # before the 4 s lease can expire
    a._stop.set()


def test_vip_follows_leader(etcd, prefix, tmp_path):
    log = tmp_path / "vip.log"
    helper = tmp_path / "vip.sh"
    helper.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    helper.chmod(0o755)
    c = cfg(etcd, "a", prefix=prefix)
    c["cluster"]["vip"] = {"address": "192.0.2.10/24", "interface": "eth0", "helper": str(helper)}
    a = Leadership(c)
    a.start()
    assert wait_for(a.is_leader)
    assert wait_for(lambda: "add 192.0.2.10/24 eth0" in log.read_text())
    a.stop()
    assert log.read_text().strip().splitlines()[-1] == "del 192.0.2.10/24 eth0"


def test_etcd_client_txn(etcd, prefix):
    e = Etcd({"endpoints": ["http://127.0.0.1:9", etcd]})     # first endpoint dead
    lease = e.grant(5)
    ok, kv = e.create_if_absent(f"{prefix}/k", "one", lease)
    assert ok
    ok2, kv2 = e.create_if_absent(f"{prefix}/k", "two", lease)
    assert not ok2 and int(kv2["create_revision"]) == int(kv["create_revision"])
    assert e.keepalive(lease) > 0
    e.revoke(lease)
    assert e.get(f"{prefix}/k") is None
