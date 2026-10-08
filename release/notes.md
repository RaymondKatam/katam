## Katam Virtualization 0.5.0

**Download** `katam-virtualization-0.5.0-amd64.iso` below (BIOS and UEFI, installs offline) and check it:

```bash
shasum -a 256 -c katam-virtualization-0.5.0-amd64.iso.sha256    # macOS
sha256sum -c katam-virtualization-0.5.0-amd64.iso.sha256        # Linux
```

### New
- **Control plane on the nodes, with leader election.** Run the controller on every node; they elect a
  leader through etcd (Raft) with a 10-second lease. If the leader's node dies, another controller takes
  over (about 10-20 s); stopping the leader hands over in about a second. Every election raises a term
  that is re-checked before fencing, HA restarts and DR failover, so a stale leader can never act.
  PostgreSQL is replicated by Patroni with synchronous commits; a virtual IP follows the leader.
  `katam-cluster init/join` sets it all up with TLS between members. See docs/CLUSTER.md.
- **Witness as tie-breaker.** For two-node clusters (third etcd vote) and between PR and DR sites: the
  DR site fails over, or asks an admin to approve, only when the witness confirms the primary is down.
  A broken link between sites never triggers a failover.
- **Grafana.** Prometheus metrics at `/metrics`, a Grafana dashboard, alert rules, and Katam events
  (fencing, failovers, leader changes, upgrades, runbook runs) as Grafana annotations. See docs/MONITORING.md.
- **Runbooks in Git (GitOps) for CI/CD.** Katam syncs runbooks from a Git branch on a timer and on push
  webhooks (GitHub, GitLab, Gitea, Forgejo). One bad file fails the whole sync; nothing half-applies.
  Git-managed runbooks are read-only in the UI; every run records its commit. `katamctl runbook lint`
  (offline, for CI), `katamctl runbook validate` (against the cluster) and example GitHub Actions and
  GitLab CI pipelines in examples/gitops.
- `katamctl cluster`, `cluster step-down`, `gitops status|sync`, `dr-watch status|approve|reset`.

### Upgrading from 0.4
Single-controller installs keep working unchanged (`cluster.mode: single`). To move to leader election,
follow docs/CLUSTER.md. Admins see an update banner in the UI; **Upgrade nodes** upgrades them one at a time.

### Status
Early software (0.x). Leader election, failover, Patroni and Prometheus were tested on real etcd,
PostgreSQL and Prometheus; test on your own hardware before production, and keep backups.
