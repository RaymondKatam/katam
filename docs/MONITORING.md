# Grafana and Prometheus

Katam exposes Prometheus metrics at `/metrics` on every controller, ships a Grafana dashboard,
and can post its events to Grafana as annotations.

## 1. Let Prometheus scrape Katam

In `/etc/katam/config.yaml` on each controller:

```yaml
metrics:
  token: <openssl rand -hex 24>     # Prometheus sends it as a bearer token
  allow: [127.0.0.1/32]             # addresses that may scrape without the token
```

`sudo systemctl restart katam`, then add the job from
`/usr/share/katam/integrations/prometheus/katam-scrape.yml` to `prometheus.yml`, with the token
in `/etc/prometheus/katam-metrics.token`. Scrape every controller (or the virtual IP). Optional
alert rules: `/usr/share/katam/integrations/prometheus/katam-alerts.yml`.

## 2. Add the dashboard

Either import `/usr/share/katam/integrations/grafana/katam-overview.json` in Grafana
(Dashboards > New > Import, pick your Prometheus data source), or provision it: copy
`integrations/grafana/provisioning/dashboards/katam.yaml` to `/etc/grafana/provisioning/dashboards/`
and the JSON to `/var/lib/grafana/dashboards/katam/`.

Panels: nodes up and their HA state, VMs running, control-plane leader and term, cost per hour,
memory and CPU commitment per node, BMC power draw, free GPUs, busiest VMs, cost per project,
VMs without protection, tasks, failovers and Git syncs, etcd health.

## 3. Events as annotations (optional)

Create a Grafana service account with the **Annotations: write** permission, then:

```yaml
grafana:
  url: https://grafana.example.com
  token: glsa_...
```

Node fencing, HA restarts, leader changes, DR watch changes and failovers, upgrades, runbook
runs and Git syncs then appear as markers (tag `katam`) on the dashboard. The UI header shows a
**Grafana** link to the dashboard.

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `katam_info` | version, member, role, mode | 1 per controller |
| `katam_controller_is_leader` | member | 1 on the leader |
| `katam_leader_term` | | rises on every control-plane failover |
| `katam_etcd_endpoint_healthy` | endpoint | etcd health as seen by this controller |
| `katam_node_up` | node, site | libvirt answers |
| `katam_node_state` | node, site, state | ok, suspect, fenced, maintenance (one series each) |
| `katam_node_memory_bytes`, `_free_bytes`, `_committed_bytes` | node, site | memory |
| `katam_node_cpu_threads`, `katam_node_vcpus_allocated` | node, site | CPU commitment |
| `katam_node_vms`, `katam_node_vms_running` | node, site | VMs per node |
| `katam_node_gpus`, `katam_node_gpus_free` | node, site | passthrough GPUs |
| `katam_node_power_watts` | node | from the BMC (Redfish) |
| `katam_vm_running`, `_vcpus`, `_memory_bytes`, `_disk_bytes`, `_gpus`, `_cpu_utilization` | vm, project, node | latest metering sample |
| `katam_project_vms`, `katam_project_vms_running` | project | |
| `katam_project_cost_per_hour` | project, currency | from the rate card |
| `katam_vms_without_snapshots`, `katam_vms_without_policy` | | protection gaps |
| `katam_tasks_24h` | kind, status | tasks started in the last day |
| `katam_tasks_running` | | |
| `katam_events_total` | event | fencing, leader elections, DR failovers, Git syncs |
| `katam_update_available` | current, latest | a newer release is in the feed |

The output is cached for `metrics.cache_s` (15 s), so frequent scrapes don't load the nodes.
