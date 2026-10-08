"""Prometheus /metrics and Grafana annotations."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import free_port


def parse(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            k, v = line.rsplit(" ", 1)
            out[k] = float(v)
    return out


def test_metrics_access_and_content(make_app):
    app, c = make_app({"metrics": {"token": "scrape-me", "allow": ["127.0.0.1/32"]}})
    app.katam["metering"].sample()
    assert c.get("/metrics").status_code == 200                       # allowed address
    far = {"REMOTE_ADDR": "198.51.100.7"}
    assert c.get("/metrics", environ_base=far).status_code == 403
    assert c.get("/metrics", environ_base=far, headers={"Authorization": "Bearer nope"}).status_code == 403
    r = c.get("/metrics", environ_base=far, headers={"Authorization": "Bearer scrape-me"})
    assert r.status_code == 200 and r.mimetype == "text/plain"
    m = parse(r.data.decode())
    assert m['katam_controller_is_leader{member="%s"}' % app.katam["leader"].member] == 1
    assert m['katam_node_up{node="hv1",site="primary"}'] == 1
    assert m['katam_node_state{state="ok",node="hv1",site="primary"}'] == 1
    assert m['katam_vm_running{vm="test",project="default",node="hv1"}'] == 1
    assert m['katam_project_cost_per_hour{project="default",currency="USD"}'] > 0
    assert m["katam_leader_term"] == 1
    assert m["katam_metering_last_sample_timestamp_seconds"] > 1.7e9     # full precision
    # every series has HELP and TYPE once
    text = r.data.decode()
    names = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line and not line.startswith("#")}
    for n in names:
        assert text.count(f"# TYPE {n} ") == 1


def test_metrics_can_be_disabled(make_app):
    app, c = make_app({"metrics": {"enabled": False}})
    assert c.get("/metrics").status_code == 403


def test_grafana_annotations(make_app):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.path, self.headers["Authorization"], body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app, c = make_app({"grafana": {"url": f"http://127.0.0.1:{port}", "token": "glsa_x"}})
    db = app.katam["db"]
    db.audit("ha", "node.fenced", "hv2", "ok", "Powered off through Redfish")
    db.audit("admin", "token.create", "ci")                              # not an annotation event
    db.audit("controller:c2", "leader.elected", "c2", "ok", {"term": 9})
    import time
    for _ in range(50):
        if len(got) >= 2:
            break
        time.sleep(0.1)
    srv.shutdown()
    assert [g[0] for g in got] == ["/api/annotations", "/api/annotations"]
    assert got[0][1] == "Bearer glsa_x"
    assert got[0][2]["tags"] == ["katam", "node", "ok"] and "hv2" in got[0][2]["text"]
    assert got[0][2]["dashboardUID"] == "katam-overview"
    assert "leader.elected c2" in got[1][2]["text"]


def test_integrations_endpoint(make_app):
    app, c = make_app({"grafana": {"url": "https://grafana.example.com/", "token": "t"}})
    j = c.get("/api/v1/integrations").json
    assert j["grafana"]["dashboard_url"] == "https://grafana.example.com/d/katam-overview"
    assert j["metrics"]["path"] == "/metrics"
