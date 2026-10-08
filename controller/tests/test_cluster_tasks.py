"""Tasks shared by several controllers on one database."""
import threading

import pytest

from katam.db import DB
from katam.tasks import Busy, Tasks


def test_busy_across_controllers_and_orphans(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/t.db")
    a, b = Tasks(db, member="c1"), Tasks(db, member="c2")
    release = threading.Event()
    tid = a.submit("vm.migrate", "vm:web", lambda p: release.wait(10) and "moved")
    with pytest.raises(Busy):
        b.submit("vm.power", "vm:web", lambda p: "x")
    # c1 dies; the leader (c2) sees it's no longer a member and fails its task
    assert b.reap_orphans({"c2"}) == 1
    assert a.get(tid)["status"] == "failed" and "c1" in a.get(tid)["message"]
    release.set()


def test_restart_only_fails_own_tasks(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/t.db")
    a = Tasks(db, member="c1")
    hold = threading.Event()
    tid = a.submit("x", "t1", lambda p: hold.wait(10))
    Tasks(db, member="c2")                    # another controller starting up
    assert a.get(tid)["status"] == "running"
    Tasks(db, member="c1")                    # c1 itself restarting
    assert a.get(tid)["status"] == "failed"
    hold.set()
