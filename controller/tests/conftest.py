"""Shared fixtures. The app runs against libvirt's test driver and SQLite; nothing touches a
real host. Tests that need etcd or git skip themselves when the binary isn't installed."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_cfg(tmp, extra=None):
    cfg = {
        "controller": {"secret_key": "0123456789abcdef0123456789abcdef", "novnc_url": "http://localhost:6080",
                       "database_url": f"sqlite:///{tmp}/katam.db", "console_token_dir": f"{tmp}/tok",
                       "staging_dir": f"{tmp}/stage", "exports_dir": f"{tmp}/exp",
                       "bootstrap_admin": {"user": "admin", "password": "admin-pass-123"}},
        "nodes": [{"name": "hv1", "host": "127.0.0.1", "uri": "test:///default"}],
        "storage": {"type": "dir", "pool": "default", "dir_path": f"{tmp}/img"},
        "networks": [{"name": "lan", "bridge": "br0"}],
    }
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    p = os.path.join(tmp, "cfg.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f)
    return p


@pytest.fixture
def make_app(tmp_path):
    def make(extra=None, background=False):
        from katam.app import create_app
        app, cfg = create_app(write_cfg(str(tmp_path), extra), start_background=background)
        c = app.test_client()
        r = c.post("/auth/login", json={"username": "admin", "password": "admin-pass-123"})
        assert r.status_code == 200, r.json
        return app, c
    return make


H = {"X-Katam": "1"}


@pytest.fixture(scope="session")
def etcd():
    """A throwaway single-member etcd. Yields its client URL."""
    if not shutil.which("etcd"):
        pytest.skip("etcd not installed")
    d = tempfile.mkdtemp()
    cport, pport = free_port(), free_port()
    url = f"http://127.0.0.1:{cport}"
    p = subprocess.Popen(["etcd", "--data-dir", d, "--listen-client-urls", url, "--advertise-client-urls", url,
                          "--listen-peer-urls", f"http://127.0.0.1:{pport}",
                          "--initial-advertise-peer-urls", f"http://127.0.0.1:{pport}",
                          "--initial-cluster", f"default=http://127.0.0.1:{pport}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import requests
    for _ in range(50):
        try:
            if requests.get(f"{url}/health", timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.2)
    yield url
    p.terminate()
    p.wait()
    shutil.rmtree(d, ignore_errors=True)
