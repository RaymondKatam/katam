"""Run commands on nodes over SSH (or locally, for single-box labs and tests)."""
import shlex
import subprocess

from .config import node_cfg


class ExecError(Exception):
    pass


class NodeExec:
    def __init__(self, cfg):
        self.cfg = cfg
        c = cfg["controller"]
        self.key = c["ssh_key"]
        self.user = c["ssh_user"]

    def _ssh_base(self, n):
        return ["ssh", "-i", self.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30", f"{n.get('ssh_user', self.user)}@{n['host']}"]

    def run(self, node, argv, sudo=False, timeout=None, input_bytes=None, check=True):
        """argv is a list; it is quoted for the remote shell. Returns stdout as text."""
        n = node_cfg(self.cfg, node)
        cmd = (["sudo", "-n"] if sudo else []) + list(argv)
        if n["exec"] == "local":
            full = cmd if not sudo or n.get("local_sudo", False) else list(argv)
        else:
            full = self._ssh_base(n) + [" ".join(shlex.quote(a) for a in cmd)]
        p = subprocess.run(full, capture_output=True, timeout=timeout, input=input_bytes)
        if check and p.returncode != 0:
            err = (p.stderr or p.stdout).decode(errors="replace").strip().splitlines()
            raise ExecError(f"{node}: {' '.join(argv[:3])}... failed: {err[-1] if err else p.returncode}")
        return p.stdout.decode(errors="replace")

    def push(self, node, local_path, remote_path):
        n = node_cfg(self.cfg, node)
        if n["exec"] == "local":
            if local_path != remote_path:
                subprocess.run(["cp", local_path, remote_path], check=True)
            return
        dest = f"{n.get('ssh_user', self.user)}@{n['host']}:{remote_path}"
        p = subprocess.run(["scp", "-q", "-i", self.key, "-o", "BatchMode=yes", local_path, dest],
                           capture_output=True)
        if p.returncode:
            raise ExecError(f"Copy to {node} failed: {p.stderr.decode().strip()}")

    def pull(self, node, remote_path, local_path):
        n = node_cfg(self.cfg, node)
        if n["exec"] == "local":
            if local_path != remote_path:
                subprocess.run(["cp", remote_path, local_path], check=True)
            return
        src = f"{n.get('ssh_user', self.user)}@{n['host']}:{remote_path}"
        p = subprocess.run(["scp", "-q", "-i", self.key, "-o", "BatchMode=yes", src, local_path],
                           capture_output=True)
        if p.returncode:
            raise ExecError(f"Copy from {node} failed: {p.stderr.decode().strip()}")

    def write(self, node, remote_path, data: bytes, sudo=False):
        self.run(node, ["tee", remote_path], sudo=sudo, input_bytes=data)
