"""Load /etc/katam/config.yaml and fill in defaults."""
import copy

import yaml

DEFAULTS = {
    "controller": {
        "listen": "0.0.0.0",
        "port": 8080,
        "database_url": "sqlite:////var/lib/katam/katam.db",
        "console_token_dir": "/var/lib/katam/console-tokens",
        "staging_dir": "/var/lib/katam/staging",
        "exports_dir": "/var/lib/katam/exports",
        "ssh_key": "/var/lib/katam/.ssh/id_ed25519",
        "ssh_user": "katam",
        "session_hours": 12,
    },
    "auth": {"local": True, "ldap": {"enabled": False}, "saml": {"enabled": False}},
    "storage": {"seed_pool": "seeds", "iso_pool": "isos", "node_staging": "/var/lib/katam/staging"},
    "sdn": {"enabled": False, "provider_network": "provider"},
    "scheduler": {"host_reserve_mb": 24576, "memory_overcommit": 1.0},
    "cloud_init": {"default_user": "lab", "ssh_keys": []},
    "limits": {"max_vcpus": 32, "max_memory_gb": 160, "max_disk_gb": 4096},
    "finops": {
        "currency": "USD",
        "sample_interval_s": 300,
        "rates": {"vcpu_hour": 1.5, "ram_gb_hour": 0.8, "disk_gb_month": 6.0,
                  "gpu_hour": 60.0, "kwh": 28.0},
    },
    "ha": {"enabled": False, "interval_s": 10, "failures_before_fence": 3,
           "restart_delay_s": 5, "witness_url": None, "witness_token": None},
    "dr": {"sites": {},
           # PR/DR watch, run by the DR site's controller: fail over (or ask for approval) only
           # when this site AND the witness both can't reach the primary for after_s seconds.
           "watch": {"enabled": False, "plan": None, "primary_host": None, "primary_port": 8080,
                     "witness_url": None, "witness_token": None, "after_s": 120, "interval_s": 15,
                     "approve": True}},
    # Which controller runs cluster-wide services (see leader.py). single | postgres | etcd
    "cluster": {"mode": "single", "member": None, "advertise_url": None,
                "etcd": {"endpoints": [], "prefix": "/katam", "lease_ttl_s": 10,
                         "ca": None, "cert": None, "key": None},
                "vip": {"address": None, "interface": None}},
    # Prometheus endpoint (/metrics) for Grafana. Scrapers need the token, or a listed address.
    "metrics": {"enabled": True, "token": None, "allow": ["127.0.0.1/32", "::1/128"], "cache_s": 15},
    # Optional: send events (failover, leader change, upgrades, runbook runs) as Grafana annotations.
    "grafana": {"url": None, "token": None, "dashboard_uid": "katam-overview", "verify_tls": True},
    # Runbooks kept in Git (GitOps). Push to the branch, the webhook or the timer syncs them.
    "runbooks": {"git": {"url": None, "branch": "main", "path": ".", "interval_s": 300,
                         "webhook_secret": None, "ssh_key": None, "token_file": None, "prune": True,
                         "workdir": "/var/lib/katam/gitops/runbooks"}},
    "protection": {"scheduler_interval_s": 300, "max_lock_days": 3650, "vault": {"enabled": False}},
    "updates": {"feed_url": None, "channel": "stable", "check_interval_h": 6},
    "images": [],
}


class ConfigError(Exception):
    pass


def _merge(default, given):
    out = copy.deepcopy(default)
    for k, v in (given or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)

    c = cfg["controller"]
    for k in ("secret_key", "novnc_url"):
        if not c.get(k):
            raise ConfigError(f"controller.{k} is required")
    if c["secret_key"] in ("change-me", "replace-with-random"):
        raise ConfigError("Set controller.secret_key to a long random string (openssl rand -hex 32)")

    if not cfg.get("nodes"):
        raise ConfigError("Add at least one entry under nodes")
    for n in cfg["nodes"]:
        for k in ("name", "host", "uri"):
            if not n.get(k):
                raise ConfigError(f"Every node needs '{k}'")
        n.setdefault("site", "primary")
        n.setdefault("exec", "ssh")

    st = cfg["storage"]
    if st.get("type") not in ("rbd", "dir"):
        raise ConfigError("storage.type must be rbd or dir")
    if not st.get("pool"):
        raise ConfigError("storage.pool is required")
    if st["type"] == "rbd":
        for k in ("ceph_pool", "user", "secret_uuid", "monitors"):
            if not (st.get("rbd") or {}).get(k):
                raise ConfigError(f"storage.rbd.{k} is required in rbd mode")
    st.setdefault("import_node", cfg["nodes"][0]["name"])

    if not cfg.get("networks"):
        raise ConfigError("Add at least one entry under networks")
    for net in cfg["networks"]:
        net.setdefault("type", "bridge")
        if net["type"] == "bridge" and not net.get("bridge"):
            raise ConfigError(f"Network {net.get('name')}: bridge networks need 'bridge'")
        if net["type"] in ("ovn-vlan", "ovn-overlay") and not cfg["sdn"]["enabled"]:
            raise ConfigError(f"Network {net['name']} is {net['type']} but sdn.enabled is false")
        if net["type"] == "ovn-vlan" and not net.get("vlan"):
            raise ConfigError(f"Network {net['name']}: ovn-vlan networks need 'vlan'")
    if cfg["sdn"]["enabled"] and not cfg["sdn"].get("central_node"):
        raise ConfigError("sdn.central_node is required when SDN is enabled")

    cl = cfg["cluster"]
    if cl["mode"] not in ("single", "postgres", "etcd"):
        raise ConfigError("cluster.mode must be single, postgres or etcd")
    if cl["mode"] != "single" and not c["database_url"].startswith("postgresql"):
        raise ConfigError(f"cluster.mode {cl['mode']} needs a shared PostgreSQL database_url")
    if cl["mode"] == "etcd":
        if not cl["etcd"].get("endpoints"):
            raise ConfigError("cluster.etcd.endpoints is required in etcd mode")
        if float(cl["etcd"]["lease_ttl_s"]) < 3:
            raise ConfigError("cluster.etcd.lease_ttl_s must be at least 3 seconds")
    if cl["vip"].get("address") and not cl["vip"].get("interface"):
        raise ConfigError("cluster.vip.interface is required with cluster.vip.address")
    w = cfg["dr"]["watch"]
    if w.get("enabled"):
        for k in ("plan", "primary_host", "witness_url"):
            if not w.get(k):
                raise ConfigError(f"dr.watch.{k} is required when dr.watch is enabled")
    g = cfg["runbooks"]["git"]
    if g.get("url") and g.get("path", ".").startswith("/"):
        raise ConfigError("runbooks.git.path is relative to the repository root")
    return cfg


def node_cfg(cfg, name):
    for n in cfg["nodes"]:
        if n["name"] == name:
            return n
    raise ConfigError(f"Unknown node {name}")
