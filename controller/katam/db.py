"""Control-plane database. SQLite for a single controller, Postgres for HA controllers."""
import json
import time

from sqlalchemy import (Boolean, Column, Float, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, delete, insert, select, update)

md = MetaData()

users = Table("users", md,
    Column("name", String(64), primary_key=True),
    Column("display", String(128)),
    Column("email", String(128)),
    Column("source", String(16), nullable=False),        # local | ldap | saml
    Column("password_hash", String(255)),
    Column("disabled", Boolean, default=False),
    Column("created", Float))

groups = Table("groups", md,
    Column("name", String(128), primary_key=True),
    Column("description", String(255)))

group_members = Table("group_members", md,
    Column("group_name", String(128), primary_key=True),
    Column("user_name", String(64), primary_key=True))

projects = Table("projects", md,
    Column("name", String(64), primary_key=True),
    Column("description", String(255)),
    Column("quota", Text, default="{}"),                 # JSON: vcpus, memory_gb, disk_gb, gpus, vms
    Column("cost_center", String(64)),
    Column("created", Float))

bindings = Table("role_bindings", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subject", String(200), nullable=False),      # user:alice | group:Katam-Admins
    Column("role", String(32), nullable=False),          # admin | operator | viewer | auditor
    Column("project", String(64), nullable=False),       # project name, or * for all
    UniqueConstraint("subject", "role", "project"))

tokens = Table("api_tokens", md,
    Column("id", String(16), primary_key=True),
    Column("user_name", String(64), nullable=False),
    Column("name", String(64)),
    Column("hash", String(64), nullable=False),
    Column("expires", Float),
    Column("last_used", Float),
    Column("created", Float))

images = Table("images", md,
    Column("name", String(64), primary_key=True),
    Column("label", String(128)),
    Column("project", String(64)),                       # null = shared with every project
    Column("volumes", Text, nullable=False),             # JSON list of base volume names, boot disk first
    Column("os_type", String(16), default="linux"),
    Column("firmware", String(8), default="bios"),
    Column("format", String(16), default="qcow2"),
    Column("size_gb", Float),
    Column("cloud_init", Boolean, default=True),
    Column("source", String(512)),
    Column("status", String(16), default="ready"),       # importing | ready | failed
    Column("created", Float))

templates = Table("templates", md,
    Column("name", String(64), primary_key=True),
    Column("label", String(128)),
    Column("project", String(64)),
    Column("spec", Text, nullable=False),                # JSON VM create body
    Column("created", Float))

security_groups = Table("security_groups", md,
    Column("name", String(64), primary_key=True),
    Column("project", String(64)),
    Column("description", String(255)),
    Column("rules", Text, default="[]"),
    Column("created", Float))

runbooks = Table("runbooks", md,
    Column("name", String(64), primary_key=True),
    Column("description", String(255)),
    Column("source", Text, nullable=False),              # YAML
    Column("updated", Float))

dr_plans = Table("dr_plans", md,
    Column("name", String(64), primary_key=True),
    Column("spec", Text, nullable=False),                # JSON
    Column("last_test", Float),
    Column("last_result", Text))

vm_backups = Table("vm_xml_backups", md,                 # used by HA restart and DR
    Column("vm", String(64), primary_key=True),
    Column("node", String(64)),
    Column("xml", Text),
    Column("project", String(64)),
    Column("ha", Boolean, default=True),
    Column("autostart", Boolean, default=False),
    Column("running", Boolean, default=False),
    Column("gpu", Boolean, default=False),
    Column("updated", Float))

usage = Table("usage_samples", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, index=True),
    Column("vm", String(64)),
    Column("project", String(64)),
    Column("node", String(64)),
    Column("running", Boolean),
    Column("vcpus", Integer),
    Column("memory_mb", Integer),
    Column("disk_gb", Float),
    Column("gpus", Integer),
    Column("cpu_util", Float),
    Column("interval_s", Float))

node_power = Table("node_power", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, index=True),
    Column("node", String(64)),
    Column("watts", Float),
    Column("interval_s", Float))

node_state = Table("node_state", md,
    Column("node", String(64), primary_key=True),
    Column("status", String(16)),                        # ok | suspect | fenced | maintenance
    Column("detail", Text),
    Column("updated", Float))

audit = Table("audit_log", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, index=True),
    Column("actor", String(128)),
    Column("action", String(64)),
    Column("target", String(200)),
    Column("result", String(16)),
    Column("detail", Text))

snapshots = Table("snapshots", md,
    Column("id", String(16), primary_key=True),
    Column("vm", String(64), index=True),
    Column("project", String(64)),
    Column("name", String(128)),
    Column("kind", String(16)),                          # manual | scheduled
    Column("policy", String(64)),
    Column("disks", Text),                               # JSON [[kind, source]]
    Column("consistent", Boolean, default=False),        # guest filesystems were frozen
    Column("locked_until", Float),                       # no deletion before this time
    Column("created", Float, index=True),
    Column("created_by", String(128)),
    Column("status", String(16), default="ok"))

protection_policies = Table("protection_policies", md,
    Column("name", String(64), primary_key=True),
    Column("project", String(64)),                       # whole project, or
    Column("vms", Text, default="[]"),                   # explicit VM list
    Column("interval_h", Float, nullable=False),
    Column("keep", Integer, nullable=False),
    Column("lock_days", Float, default=0),
    Column("vault", Boolean, default=False),             # also copy to the immutable vault
    Column("vault_days", Float, default=30),
    Column("enabled", Boolean, default=True),
    Column("last_run", Float))

vault_copies = Table("vault_copies", md,
    Column("id", String(16), primary_key=True),
    Column("snapshot_id", String(16), index=True),
    Column("vm", String(64), index=True),
    Column("project", String(64)),
    Column("objects", Text),                             # JSON [{key, version, size}]
    Column("retain_until", Float),
    Column("created", Float),
    Column("status", String(16)))                        # copying | ok | failed

tasks = Table("tasks", md,
    Column("id", String(16), primary_key=True),
    Column("kind", String(32)),
    Column("target", String(200)),
    Column("project", String(64)),
    Column("actor", String(128)),
    Column("status", String(16)),
    Column("message", Text),
    Column("log", Text, default=""),
    Column("created", Float, index=True),
    Column("finished", Float))


kv = Table("kv", md,                                    # small shared state (git sync status, DR watch)
    Column("key", String(128), primary_key=True),
    Column("value", Text),
    Column("updated", Float))

runbook_origins = Table("runbook_origins", md,          # runbooks managed in Git
    Column("name", String(64), primary_key=True),
    Column("origin", String(16)),                        # git
    Column("commit", String(64)),
    Column("path", String(255)),
    Column("synced", Float))

task_owners = Table("task_owners", md,                  # which controller runs each task
    Column("task_id", String(16), primary_key=True),
    Column("member", String(128), index=True))


class DB:
    def __init__(self, url):
        kw = {"future": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 15}
        else:
            # Replicated PostgreSQL (Patroni) moves the primary on failover: test connections
            # before use and list every member in the URL with target_session_attrs=read-write.
            kw.update(pool_pre_ping=True, pool_recycle=300)
        self.engine = create_engine(url, **kw)
        md.create_all(self.engine)
        self.listeners = []                              # called after every audit entry

    def kv_get(self, key, default=None):
        r = self.one(select(kv).where(kv.c.key == key))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key, value):
        row = {"value": json.dumps(value), "updated": time.time()}
        if self.one(select(kv.c.key).where(kv.c.key == key)):
            self.run(update(kv).where(kv.c.key == key).values(**row))
        else:
            self.run(insert(kv).values(key=key, **row))

    def all(self, stmt):
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt)]

    def one(self, stmt):
        rows = self.all(stmt)
        return rows[0] if rows else None

    def run(self, stmt):
        with self.engine.begin() as c:
            return c.execute(stmt)

    # Convenience for JSON columns
    @staticmethod
    def loads(v, default=None):
        if v in (None, ""):
            return default
        return json.loads(v)

    @staticmethod
    def dumps(v):
        return json.dumps(v, sort_keys=True)

    def audit(self, actor, action, target, result="ok", detail=None):
        detail = detail if isinstance(detail, str) or detail is None else json.dumps(detail)
        self.run(insert(audit).values(ts=time.time(), actor=actor, action=action,
                                      target=target, result=result, detail=detail))
        for fn in self.listeners:
            try:
                fn({"actor": actor, "action": action, "target": target, "result": result, "detail": detail})
            except Exception:  # noqa: BLE001
                pass


__all__ = ["DB", "select", "insert", "update", "delete",
           "users", "groups", "group_members", "projects", "bindings", "tokens", "images",
           "templates", "security_groups", "runbooks", "dr_plans", "vm_backups", "usage",
           "node_power", "node_state", "audit", "tasks", "snapshots", "protection_policies",
           "vault_copies", "kv", "runbook_origins", "task_owners"]
