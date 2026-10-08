"""SDN with OVN: logical networks, routers with SNAT, and security groups (micro-segmentation).

Every VM port joins port group pg_katam_all, which drops all IP traffic by default (with
deny logging). Security groups are port groups whose ACLs allow specific flows; OVN keeps an
address set <group>_ip4 per port group, so rules can reference other groups.
"""
import json
import re
import time
import zlib

from .db import delete, insert, security_groups, select, update
from .driver import ClusterError

ALL = "pg_katam_all"
DEFAULT_RULES = [
    {"direction": "egress", "protocol": "any", "remote": "0.0.0.0/0"},
    {"direction": "ingress", "protocol": "any", "remote_group": "default"},
]


def pg_name(sg):
    return "sg_" + re.sub(r"[^A-Za-z0-9_]", "_", sg)


def ls_name(net):
    return "katam-" + net


class SDN:
    def __init__(self, cfg, db, nexec):
        self.cfg, self.db, self.x = cfg, db, nexec
        self.central = cfg["sdn"]["central_node"]

    def nb(self, *args, check=True):
        return self.x.run(self.central, ["ovn-nbctl", "--no-leader-only", *args], sudo=True, check=check).strip()

    def _uuid(self, table, name):
        out = self.nb("--bare", "--columns=_uuid", "find", table, f"name={name}")
        return out.split()[0] if out else None

    # ---- bootstrap / networks ------------------------------------------
    def sync(self):
        """Create everything config.yaml describes. Idempotent."""
        self.nb("--may-exist", "pg-add", ALL)
        self.nb("--type=port-group", "acl-del", ALL, check=False)
        self.nb("--type=port-group", "--log", "--severity=warning", "--name=katam-deny-in",
                "acl-add", ALL, "to-lport", "1001", f"outport == @{ALL} && ip", "drop")
        self.nb("--type=port-group", "--log", "--severity=warning", "--name=katam-deny-out",
                "acl-add", ALL, "from-lport", "1001", f"inport == @{ALL} && ip", "drop")
        self.nb("--type=port-group", "acl-add", ALL, "from-lport", "1002",
                f"inport == @{ALL} && udp && udp.dst == 67", "allow")
        self.nb("--type=port-group", "acl-add", ALL, "to-lport", "1002",
                f"outport == @{ALL} && udp && udp.src == 67", "allow")

        for net in self.cfg["networks"]:
            if net["type"] not in ("ovn-vlan", "ovn-overlay"):
                continue
            ls = ls_name(net["name"])
            self.nb("--may-exist", "ls-add", ls)
            if net["type"] == "ovn-vlan":
                ln = f"ln-{net['name']}"
                self.nb("--may-exist", "lsp-add", ls, ln)
                self.nb("lsp-set-type", ln, "localnet")
                self.nb("lsp-set-addresses", ln, "unknown")
                self.nb("lsp-set-options", ln, f"network_name={self.cfg['sdn']['provider_network']}")
                self.nb("set", "logical_switch_port", ln, f"tag={int(net['vlan'])}")
            if net.get("cidr") and net.get("dhcp") and net["type"] == "ovn-overlay":
                self.nb("set", "logical_switch", ls, f"other_config:subnet={net['cidr']}")
                if net.get("gateway"):
                    self.nb("set", "logical_switch", ls, f"other_config:exclude_ips={net['gateway']}")

        for r in self.cfg["sdn"].get("routers", []):
            self._sync_router(r)
        if not self.db.one(select(security_groups).where(security_groups.c.name == "default")):
            self.save_group("default", None, "Allow all egress; allow ingress from members of default",
                            DEFAULT_RULES)

    def _sync_router(self, r):
        lr = f"lr-{r['name']}"
        self.nb("--may-exist", "lr-add", lr)
        for i, net_name in enumerate(r.get("networks", [])):
            net = next(n for n in self.cfg["networks"] if n["name"] == net_name)
            prefix = net["cidr"].split("/")[1]
            lrp, mac = f"{lr}-{net_name}", "02:ac:%02x:00:%02x:01" % (zlib.crc32(r["name"].encode()) & 0xff, i)
            self.nb("--may-exist", "lrp-add", lr, lrp, mac, f"{net['gateway']}/{prefix}")
            rp = f"rp-{net_name}-{r['name']}"
            self.nb("--may-exist", "lsp-add", ls_name(net_name), rp)
            self.nb("lsp-set-type", rp, "router")
            self.nb("lsp-set-addresses", rp, "router")
            self.nb("lsp-set-options", rp, f"router-port={lrp}")
            if net.get("dhcp"):
                self._dhcp(net)
        ext = r.get("external")
        if ext:
            lrp = f"{lr}-ext"
            self.nb("--may-exist", "lrp-add", lr, lrp, "02:ac:ff:00:00:01", ext["ip_cidr"])
            rp = f"rp-ext-{r['name']}"
            self.nb("--may-exist", "lsp-add", ls_name(ext["network"]), rp)
            self.nb("lsp-set-type", rp, "router")
            self.nb("lsp-set-addresses", rp, "router")
            self.nb("lsp-set-options", rp, f"router-port={lrp}")
            for prio, chassis in enumerate(ext.get("gateway_chassis", []), start=1):
                self.nb("lrp-set-gateway-chassis", lrp, chassis, str(100 - prio))
            self.nb("--may-exist", "lr-route-add", lr, "0.0.0.0/0", ext["next_hop"])
            ext_ip = ext["ip_cidr"].split("/")[0]
            for net_name in r.get("networks", []):
                net = next(n for n in self.cfg["networks"] if n["name"] == net_name)
                self.nb("--may-exist", "lr-nat-add", lr, "snat", ext_ip, net["cidr"])

    def _dhcp(self, net):
        find = ("--bare", "--columns=_uuid", "find", "DHCP_Options", f'cidr="{net["cidr"]}"')
        existing = self.nb(*find)
        if not existing:
            self.nb("dhcp-options-create", net["cidr"])
            existing = self.nb(*find)
        uid = existing.split()[0]
        dns = ",".join(net.get("dns") or [net["gateway"]])
        self.nb("dhcp-options-set-options", uid, "lease_time=3600", f"router={net['gateway']}",
                f"server_id={net['gateway']}", "server_mac=02:ac:00:00:00:01", f"dns_server={{{dns}}}")
        return uid

    # ---- VM ports -------------------------------------------------------
    def attach_ports(self, vm, project, nics, secgroups):
        sgs = secgroups or ["default"]
        for sg in sgs:
            if not self.db.one(select(security_groups).where(security_groups.c.name == sg)):
                raise ClusterError(f"No security group named '{sg}'")
        for nic in nics:
            if not nic.get("iface_id"):
                continue
            net = next(n for n in self.cfg["networks"] if n["name"] == nic["network"])
            ls, port = ls_name(net["name"]), nic["iface_id"]
            self.nb("--may-exist", "lsp-add", ls, port)
            if nic.get("ip"):
                addr = f"{nic['mac']} {nic['ip']}"
                self.nb("lsp-set-addresses", port, addr)
                self.nb("lsp-set-port-security", port, addr)
            elif net["type"] == "ovn-overlay" and net.get("dhcp"):
                self.nb("lsp-set-addresses", port, f"{nic['mac']} dynamic")
                self.nb("lsp-set-port-security", port, nic["mac"])
                self.nb("lsp-set-dhcpv4-options", port, self._dhcp(net))
            else:
                self.nb("lsp-set-addresses", port, nic["mac"])
                self.nb("lsp-set-port-security", port, nic["mac"])
            self.nb("set", "logical_switch_port", port,
                    f"external_ids:katam-vm={vm}", f"external_ids:katam-project={project}")
            uid = self._uuid("Logical_Switch_Port", port)
            self.nb("add", "port_group", ALL, "ports", uid)
            for sg in sgs:
                self.nb("add", "port_group", pg_name(sg), "ports", uid)

    def vm_ports(self, vm):
        out = self.nb("--bare", "--columns=_uuid,name", "find", "Logical_Switch_Port", f"external_ids:katam-vm={vm}")
        parts = out.split()
        return list(zip(parts[0::2], parts[1::2]))

    def detach_vm(self, vm):
        for _, name in self.vm_ports(vm):
            self.nb("--if-exists", "lsp-del", name)

    def set_vm_groups(self, vm, sgs):
        sgs = sgs or ["default"]
        for sg in sgs:
            if not self.db.one(select(security_groups).where(security_groups.c.name == sg)):
                raise ClusterError(f"No security group named '{sg}'")
        all_groups = [g["name"] for g in self.db.all(select(security_groups))]
        for uid, _ in self.vm_ports(vm):
            for g in all_groups:
                self.nb("remove", "port_group", pg_name(g), "ports", uid, check=False)
            for g in sgs:
                self.nb("add", "port_group", pg_name(g), "ports", uid)

    # ---- security groups ------------------------------------------------
    @staticmethod
    def validate_rules(rules):
        clean = []
        for r in rules:
            d = r.get("direction")
            if d not in ("ingress", "egress"):
                raise ClusterError("Each rule needs direction ingress or egress")
            proto = r.get("protocol", "any")
            if proto not in ("any", "tcp", "udp", "icmp"):
                raise ClusterError("protocol must be any, tcp, udp or icmp")
            ports = str(r.get("ports", "")).strip()
            if ports and (proto not in ("tcp", "udp") or not re.fullmatch(r"\d{1,5}(-\d{1,5})?", ports)):
                raise ClusterError("ports needs tcp/udp and looks like 443 or 8000-8100")
            if bool(r.get("remote")) == bool(r.get("remote_group")):
                raise ClusterError("Each rule needs exactly one of remote (CIDR) or remote_group")
            if r.get("remote") and not re.fullmatch(r"[0-9./]+", r["remote"]):
                raise ClusterError(f"remote must be an IPv4 CIDR, got {r['remote']}")
            clean.append({k: v for k, v in {"direction": d, "protocol": proto, "ports": ports or None,
                                            "remote": r.get("remote"), "remote_group": r.get("remote_group"),
                                            "description": r.get("description")}.items() if v})
        return clean

    def _match(self, sg, r):
        pg = pg_name(sg)
        if r["direction"] == "ingress":
            m = [f"outport == @{pg}", "ip4"]
            side = "ip4.src"
        else:
            m = [f"inport == @{pg}", "ip4"]
            side = "ip4.dst"
        if r.get("remote_group"):
            m.append(f"{side} == ${pg_name(r['remote_group'])}_ip4")
        elif r.get("remote") not in (None, "0.0.0.0/0"):
            m.append(f"{side} == {r['remote']}")
        if r["protocol"] != "any":
            m.append(r["protocol"])
            if r.get("ports"):
                lo, _, hi = r["ports"].partition("-")
                fld = f"{r['protocol']}.dst"
                m.append(f"{fld} == {lo}" if not hi else f"{fld} >= {lo} && {fld} <= {hi}")
        return " && ".join(m)

    def save_group(self, name, project, description, rules):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
            raise ClusterError("Group names: lowercase letters, digits, hyphens")
        rules = self.validate_rules(rules)
        for r in rules:
            if r.get("remote_group") and r["remote_group"] != name and not self.db.one(
                    select(security_groups).where(security_groups.c.name == r["remote_group"])):
                raise ClusterError(f"Rule references unknown group {r['remote_group']}")
        pg = pg_name(name)
        self.nb("--may-exist", "pg-add", pg)
        self.nb("--type=port-group", "acl-del", pg, check=False)
        for i, r in enumerate(rules):
            d = "to-lport" if r["direction"] == "ingress" else "from-lport"
            self.nb("--type=port-group", "--log", "--severity=info", f"--name={pg}-r{i}",
                    "acl-add", pg, d, "1002", self._match(name, r), "allow-related")
        row = dict(project=project, description=description, rules=json.dumps(rules))
        if self.db.one(select(security_groups).where(security_groups.c.name == name)):
            self.db.run(update(security_groups).where(security_groups.c.name == name).values(**row))
        else:
            self.db.run(insert(security_groups).values(name=name, created=time.time(), **row))
        return rules

    def delete_group(self, name):
        if name == "default":
            raise ClusterError("The default group can't be deleted")
        uid = self._uuid("Port_Group", pg_name(name))
        if uid and self.nb("--bare", "--columns=ports", "list", "Port_Group", uid):
            raise ClusterError(f"VMs still use {name}; move them to other groups first")
        self.nb("--if-exists", "pg-del", pg_name(name))
        self.db.run(delete(security_groups).where(security_groups.c.name == name))
