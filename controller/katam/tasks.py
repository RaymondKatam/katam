"""Background tasks, recorded in the database, one running task per target."""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .db import delete, insert, select, task_owners, tasks, update

log = logging.getLogger("katam.tasks")


class Busy(Exception):
    pass


class Tasks:
    """member: this controller's name. With several controllers on one database, each only
    cleans up its own interrupted tasks; the leader also fails tasks whose controller is gone
    (reap_orphans)."""

    def __init__(self, db, workers=6, member=None):
        self.db = db
        self.member = member
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._busy = set()
        self._lock = threading.Lock()
        mine = None
        if member:
            mine = [r["task_id"] for r in db.all(select(task_owners).where(task_owners.c.member == member))]
        q = update(tasks).where(tasks.c.status == "running")
        if mine is not None:
            q = q.where(tasks.c.id.in_(mine)) if mine else None
        if q is not None:
            db.run(q.values(status="failed", message="Controller restarted before this finished"))

    def reap_orphans(self, live_members):
        """Leader only: fail running tasks owned by controllers that are no longer members."""
        if not live_members:
            return 0
        n = 0
        for r in self.db.all(select(tasks.c.id, task_owners.c.member)
                             .select_from(tasks.join(task_owners, tasks.c.id == task_owners.c.task_id))
                             .where(tasks.c.status == "running")):
            if r["member"] not in live_members:
                self.db.run(update(tasks).where(tasks.c.id == r["id"]).values(
                    status="failed", finished=time.time(),
                    message=f"Controller {r['member']} stopped before this finished"))
                n += 1
        return n

    def submit(self, kind, target, fn, actor="system", project=None):
        with self._lock:
            if target in self._busy:
                raise Busy(f"{target} already has a task running")
            if self.member and self.db.one(select(tasks.c.id).where(tasks.c.target == target)
                                           .where(tasks.c.status == "running")):
                raise Busy(f"{target} already has a task running on another controller")
            self._busy.add(target)
        tid = uuid.uuid4().hex[:12]
        self.db.run(insert(tasks).values(id=tid, kind=kind, target=target, project=project,
                                         actor=actor, status="running", message="Queued",
                                         log="", created=time.time()))
        if self.member:
            self.db.run(insert(task_owners).values(task_id=tid, member=self.member))
        lines = []

        def progress(msg):
            lines.append(time.strftime("%H:%M:%S ") + msg)
            self.db.run(update(tasks).where(tasks.c.id == tid)
                        .values(message=msg, log="\n".join(lines)))

        def run():
            status, msg = "done", "Done"
            try:
                msg = fn(progress) or "Done"
            except Exception as e:  # noqa: BLE001 - every failure is reported to the caller
                log.exception("task %s (%s %s) failed", tid, kind, target)
                status = "failed"
                msg = e.get_error_message() if hasattr(e, "get_error_message") else str(e)
                lines.append(time.strftime("%H:%M:%S ") + "FAILED: " + msg)
            finally:
                with self._lock:
                    self._busy.discard(target)
            self.db.run(update(tasks).where(tasks.c.id == tid)
                        .values(status=status, message=str(msg), log="\n".join(lines),
                                finished=time.time()))
            self.db.audit(actor, kind, target, status, str(msg))
            if self.member:
                self.db.run(delete(task_owners).where(task_owners.c.task_id == tid))

        self.pool.submit(run)
        return tid

    def get(self, tid):
        return self.db.one(select(tasks).where(tasks.c.id == tid))

    def wait(self, tid, timeout=3600):
        end = time.time() + timeout
        while time.time() < end:
            t = self.get(tid)
            if t and t["status"] != "running":
                return t
            time.sleep(0.5)
        raise TimeoutError(f"Task {tid} still running after {timeout}s")

    def recent(self, projects=None, limit=40):
        q = select(tasks).order_by(tasks.c.created.desc()).limit(limit)
        if projects is not None:
            q = q.where(tasks.c.project.in_(list(projects)))
        return self.db.all(q)
