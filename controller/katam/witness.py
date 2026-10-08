"""Katam witness: a tiny service at a third location that gives HA a second opinion.

Run it on a small VM or box outside the cluster's rack (another site, or another power and
network path). The controller only fences a node when the witness also can't reach it,
which stops a controller-side network fault from triggering fencing.
The same machine can host the Ceph arbiter monitor for a two-site stretch cluster.

  KATAM_WITNESS_TOKEN=... python3 -m katam.witness --port 8443
"""
import argparse
import hmac
import os
import socket

from flask import Flask, abort, jsonify, request


def tcp_reachable(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def create_witness(token):
    app = Flask("katam-witness")

    @app.before_request
    def auth():
        if request.path == "/v1/health":
            return
        given = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            abort(401)

    @app.get("/v1/health")
    def health():
        return jsonify(ok=True)

    @app.get("/v1/probe")
    def probe():
        host = request.args.get("host", "")
        port = int(request.args.get("port", 22))
        if not host or len(host) > 255:
            abort(400)
        return jsonify(host=host, port=port, reachable=tcp_reachable(host, port))

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443)
    args = ap.parse_args()
    token = os.environ.get("KATAM_WITNESS_TOKEN")
    if not token:
        raise SystemExit("Set KATAM_WITNESS_TOKEN")
    from waitress import serve
    serve(create_witness(token), host=args.listen, port=args.port)


if __name__ == "__main__":
    main()
