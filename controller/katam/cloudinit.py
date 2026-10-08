"""Build a NoCloud cloud-init seed ISO in memory (no genisoimage needed)."""
import io

import pycdlib
import yaml


def _documents(hostname, instance_id, user, ssh_keys, password, nics, static):
    meta = {"instance-id": instance_id, "local-hostname": hostname}

    account = {
        "name": user,
        "groups": ["sudo"],
        "shell": "/bin/bash",
        "sudo": "ALL=(ALL) NOPASSWD:ALL",
        "ssh_authorized_keys": list(ssh_keys),
        "lock_passwd": not password,
    }
    if password:
        account["plain_text_passwd"] = password

    user_data = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "users": [account],
        "ssh_pwauth": bool(password),
        "growpart": {"mode": "auto", "devices": ["/"]},
        "package_update": True,
        "packages": ["qemu-guest-agent"],
        "runcmd": [["systemctl", "enable", "--now", "qemu-guest-agent"]],
    }

    ethernets = {}
    for i, nic in enumerate(nics):
        eth = {"match": {"macaddress": nic["mac"]}, "set-name": f"eth{i}"}
        if i == 0 and static:
            eth["addresses"] = [static["address"]]
            if static.get("gateway"):
                eth["routes"] = [{"to": "default", "via": static["gateway"]}]
            if static.get("dns"):
                eth["nameservers"] = {"addresses": list(static["dns"])}
        elif i == 0 or nic.get("dhcp"):
            eth["dhcp4"] = True
        else:
            eth["dhcp4"] = False
            eth["optional"] = True
        ethernets[f"eth{i}"] = eth

    return {
        "meta-data": yaml.safe_dump(meta, sort_keys=False),
        "user-data": "#cloud-config\n" + yaml.safe_dump(user_data, sort_keys=False),
        "network-config": yaml.safe_dump({"version": 2, "ethernets": ethernets}, sort_keys=False),
    }


def build_seed(hostname, instance_id, user, ssh_keys, nics, static=None, password=None):
    """Return the bytes of an ISO labelled 'cidata'."""
    docs = _documents(hostname, instance_id, user, ssh_keys, password, nics, static)
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
    for name, text in docs.items():
        data = text.encode()
        iso9660 = "/" + name.upper().replace("-", "_") + ".;1"
        iso.add_fp(io.BytesIO(data), len(data), iso9660,
                   rr_name=name, joliet_path="/" + name)
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    return out.getvalue()
