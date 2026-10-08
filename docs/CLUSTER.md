# Running the control plane on the nodes (leader election)

From 0.5, the Katam controller can run on every node instead of on one separate VM. The
controllers elect a leader; when the leader's node dies, the others elect a new one and carry on.

```
            hv1                     hv2                     hv3              witness (optional,
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐     required for 2 nodes)
   │ Katam controller │   │ Katam controller │   │ Katam controller │     ┌──────────────┐
   │   (leader, VIP)  │   │    (follower)    │   │    (follower)    │     │ etcd (vote)  │
   │ etcd member      │◄─►│ etcd member      │◄─►│ etcd member      │◄───►│ witness probe│
   │ PostgreSQL (pri) │──►│ PostgreSQL (rep) │──►│ PostgreSQL (rep) │     └──────────────┘
   └──────────────────┘   └──────────────────┘   └──────────────────┘
```

## How it works

- **etcd** (Raft) holds one key, `/katam/leader`, bound to a 10-second lease. The controller that
  creates the key is the leader and renews the lease every few seconds. If it stops renewing
  (crash, power loss, network cut), the lease expires and another controller creates the key.
  Every new leader gets a higher **term**.
- **Only the leader** runs HA (watching and fencing nodes), snapshot schedules, metering, Git
  syncs and the PR/DR watch. Every controller serves the UI and API.
- **Fencing token.** Right before anything that can't be undone (powering off a node through its
  BMC, restarting its VMs elsewhere, a DR failover), the leader re-reads the leader key from etcd
  and checks its term. A leader that was cut off and comes back can't act on stale decisions.
- **Stepping down early.** A leader that can't renew its lease steps down after 60% of the TTL,
  before the lease can expire, so two leaders never act at the same time. A watchdog enforces
  this even while an etcd call is stuck.
- **PostgreSQL** is replicated by **Patroni**, which also uses etcd. Commits wait for one replica
  (`synchronous_mode`), so a database failover loses no committed data. Controllers list every
  member in their database URL and connect to whichever one is primary.
- **Virtual IP.** The leader holds a floating address (`cluster.vip`) and announces it with
  gratuitous ARP; users and Prometheus use that one address.
- **Majority rule.** etcd needs a majority of its voters: 3 voters survive 1 failure, 5 survive 2.
  Without a majority, no controller is leader: running VMs keep running, but HA and schedules
  pause until the majority returns. That is deliberate; it's how split-brain is prevented.

## Two nodes need a witness

With two nodes, neither can tell "the other node died" from "the link between us broke". The
witness is a third etcd voter on separate power and network (a small VM at another site, or a
cloud VM). It runs no VMs and no database.

| Event | Result |
|---|---|
| hv2 dies | hv1 + witness = 2 of 3 votes. hv1 leads, fences hv2, restarts its VMs |
| Link hv1–hv2 breaks | Only the side that still reaches the witness has a majority and acts |
| Witness dies | hv1 + hv2 = 2 of 3. Everything keeps working; fix the witness soon |

`katam-cluster init` refuses two members without `--witness`.

## Setting it up

All members: Ubuntu 24.04 with `katam-node` and `katam-controller` installed (the Katam ISO's
**All-in-one** or **Node** role, plus `apt install /opt/katam-debs/katam-controller_*.deb`).
Witness: the ISO's **Witness** role or `katam-witness` on any Ubuntu 24.04 machine.

1. On the first node, create the cluster bundle (certificates and passwords):

   ```bash
   sudo katam-cluster init --members "hv1=172.20.10.11,hv2=172.20.10.12,hv3=172.20.10.13" \
        --vip 172.20.10.5/24 --iface br-mgmt
   # two nodes:  --members "hv1=...,hv2=..." --witness "witness=172.20.50.5"
   ```

2. Copy `/root/katam-cluster-bundle.tar` to every member and the witness (`scp`, then delete it
   from anywhere it doesn't need to be: it contains the cluster's private keys).

3. On every member, within a few minutes of each other:

   ```bash
   sudo katam-cluster join --bundle katam-cluster-bundle.tar --name hv1   # hv2, hv3 ...
   ```

   On the witness:

   ```bash
   sudo /usr/lib/katam-witness/katam-cluster join --bundle katam-cluster-bundle.tar --name witness
   ```

4. Check, then restart Katam on every member:

   ```bash
   sudo katam-cluster status          # etcd members, Patroni primary and replicas
   sudo systemctl restart katam
   katamctl cluster                   # controllers, leader, term
   ```

5. Browse to the virtual IP: `http://172.20.10.5:8080`.

The join step points each controller at the replicated database. To keep the data from a
single-controller install, dump it first (`sqlite3 /var/lib/katam/katam.db .dump`), or start
fresh and re-create users, projects and policies.

## Day to day

- **Planned maintenance on the leader:** `katamctl cluster step-down` (or stop `katam`): another
  controller takes over within a second or two.
- **Watch it:** the header of the UI shows the leader and term; Prometheus has
  `katam_controller_is_leader`, `katam_leader_term` and `katam_etcd_endpoint_healthy`, and the
  `KatamNoControlPlaneLeader` alert fires if no controller leads for 2 minutes.
- **Firewall:** between members, allow 2379-2380 (etcd), 5432 (PostgreSQL), 8008 (Patroni) and
  8080 (Katam). The witness needs 2379-2380 and its probe port 8443.

## Measured behaviour (test cluster)

| Scenario | Result |
|---|---|
| Leader process killed | New leader after 5.6 s (6 s lease) |
| Leader stopped (SIGTERM) | Handover in 0.6 s |
| Leader frozen 10 s, then resumed | Replaced while frozen; stepped down within 1 s of resuming |
| etcd loses its majority | Leader stepped down in 4.4 s; no leader until the majority returned |
| Whole node lost (controller, etcd member, PostgreSQL primary) | Patroni promoted the replica; data written just before the loss was kept; a new controller led after 16 s |

## PR and DR sites

For two sites more than a few milliseconds apart, run one Katam cluster per site and set up DR
(RBD mirroring and DR plans). On the DR site, `dr.watch` checks the primary site's controller
address every 15 s:

| Primary answers? | Witness reaches primary? | DR site does |
|---|---|---|
| yes | – | nothing |
| no | yes | nothing: the link between sites is down, not the primary (no split-brain) |
| no | witness unreachable | nothing: no failover without a second opinion |
| no | no, for `after_s` | asks an admin to **Approve failover** (default), or fails over by itself with `approve: false` |

After failing back, click **Watch again** on the Disaster recovery tab (or `katamctl dr-watch reset`).
