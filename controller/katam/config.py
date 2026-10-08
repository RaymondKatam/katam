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
    "dr": {"sites": {}},
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
    return cfg


def node_cfg(cfg, name):
    for n in cfg["nodes"]:
        if n["name"] == name:
            return n
    raise ConfigError(f"Unknown node {name}")
