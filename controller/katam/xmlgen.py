"""Build libvirt domain and volume XML from plain Python specs."""
import xml.etree.ElementTree as ET

META_NS = "https://katam.io/xmlns/vm/1"


def _sub(parent, tag, text=None, **attrs):
    """SubElement helper; a trailing underscore escapes Python keywords (type_ -> type)."""
    clean = {k.rstrip("_"): str(v) for k, v in attrs.items() if v is not None}
    el = ET.SubElement(parent, tag, clean)
    if text is not None:
        el.text = str(text)
    return el


def volume_xml(name, capacity_bytes, fmt=None, backing=None):
    vol = ET.Element("volume")
    _sub(vol, "name", name)
    _sub(vol, "capacity", int(capacity_bytes), unit="bytes")
    if fmt:
        tgt = _sub(vol, "target")
        _sub(tgt, "format", type_=fmt)
    if backing:
        path, bfmt = backing
        bs = _sub(vol, "backingStore")
        _sub(bs, "path", path)
        _sub(bs, "format", type_=bfmt)
    return ET.tostring(vol, encoding="unicode")


def metadata_xml(fields):
    info = ET.Element("info")
    for k, v in fields.items():
        if v is not None:
            _sub(info, k, v)
    return ET.tostring(info, encoding="unicode")


def disk_element(d, target, rbd=None):
    """d: kind (rbd|file), device (disk|cdrom), format, source, bus, boot."""
    is_disk = d["device"] == "disk"
    if d["kind"] == "rbd":
        el = ET.Element("disk", type="network", device=d["device"])
        _sub(el, "driver", name="qemu", type_="raw",
             cache="writeback" if is_disk else None,
             discard="unmap" if is_disk else None)
        auth = _sub(el, "auth", username=rbd["user"])
        _sub(auth, "secret", type_="ceph", uuid=rbd["secret_uuid"])
        src = _sub(el, "source", protocol="rbd", name=d["source"])
        for mon in rbd["monitors"]:
            _sub(src, "host", name=mon, port=rbd.get("port", 6789))
    else:
        el = ET.Element("disk", type="file", device=d["device"])
        _sub(el, "driver", name="qemu", type_=d.get("format", "qcow2"),
             discard="unmap" if is_disk else None)
        if d.get("source"):
            _sub(el, "source", file=d["source"])
    _sub(el, "target", dev=target, bus=d["bus"])
    if d.get("boot"):
        _sub(el, "boot", order=d["boot"])
    if not is_disk:
        _sub(el, "readonly")
    return el


def domain_xml(spec, rbd=None):
    windows = spec["os_type"] == "windows"
    uefi = spec.get("firmware") == "uefi"

    dom = ET.Element("domain", type="kvm")
    _sub(dom, "name", spec["name"])
    _sub(dom, "uuid", spec["uuid"])
    if spec.get("description"):
        _sub(dom, "description", spec["description"])
    _sub(dom, "memory", spec["memory_mb"], unit="MiB")
    _sub(dom, "currentMemory", spec["memory_mb"], unit="MiB")
    _sub(dom, "vcpu", spec["vcpus"], placement="static")

    os_el = _sub(dom, "os", firmware="efi" if uefi else None)
    _sub(os_el, "type", "hvm", arch="x86_64", machine="q35")
    if uefi and windows:
        fw = _sub(os_el, "firmware")
        _sub(fw, "feature", enabled="yes", name="secure-boot")
        _sub(fw, "feature", enabled="yes", name="enrolled-keys")

    feats = _sub(dom, "features")
    _sub(feats, "acpi")
    _sub(feats, "apic")
    if uefi:
        _sub(feats, "smm", state="on")
    if windows:
        hv = _sub(feats, "hyperv", mode="custom")
        for f in ("relaxed", "vapic", "vpindex", "synic", "stimer", "reset", "frequencies"):
            _sub(hv, f, state="on")
        _sub(hv, "spinlocks", state="on", retries=8191)

    # All four nodes are identical Xeon 4114s, so host-passthrough stays migratable.
    _sub(dom, "cpu", mode="host-passthrough", check="none", migratable="on")

    clock = _sub(dom, "clock", offset="localtime" if windows else "utc")
    _sub(clock, "timer", name="rtc", tickpolicy="catchup")
    _sub(clock, "timer", name="pit", tickpolicy="delay")
    _sub(clock, "timer", name="hpet", present="no")
    if windows:
        _sub(clock, "timer", name="hypervclock", present="yes")

    _sub(dom, "on_poweroff", "destroy")
    _sub(dom, "on_reboot", "restart")
    _sub(dom, "on_crash", "restart")

    devs = _sub(dom, "devices")
    counters = {"virtio": 0, "sata": 0}
    prefix = {"virtio": "vd", "sata": "sd"}
    for d in spec["disks"]:
        bus = d["bus"]
        target = prefix[bus] + "abcdefghijklmnop"[counters[bus]]
        counters[bus] += 1
        devs.append(disk_element(d, target, rbd))

    _sub(devs, "controller", type_="usb", model="qemu-xhci")
    for nic in spec["nics"]:
        iface = _sub(devs, "interface", type_="bridge")
        _sub(iface, "mac", address=nic["mac"])
        _sub(iface, "source", bridge=nic["bridge"])
        if nic.get("iface_id"):
            # OVN: the OVS port's iface-id binds it to the logical switch port.
            vp = _sub(iface, "virtualport", type_="openvswitch")
            _sub(vp, "parameters", interfaceid=nic["iface_id"])
        _sub(iface, "model", type_=nic.get("model", "virtio"))

    serial = _sub(devs, "serial", type_="pty")
    _sub(serial, "target", type_="isa-serial", port=0)
    con = _sub(devs, "console", type_="pty")
    _sub(con, "target", type_="serial", port=0)

    chan = _sub(devs, "channel", type_="unix")
    _sub(chan, "target", type_="virtio", name="org.qemu.guest_agent.0")

    _sub(devs, "input", type_="tablet", bus="usb")
    # Listen on all addresses so the console survives live migration;
    # the node firewall only lets the controller reach 5900-5999.
    gfx = _sub(devs, "graphics", type_="vnc", port=-1, autoport="yes",
               listen="0.0.0.0", passwd=spec.get("vnc_password"))
    _sub(gfx, "listen", type_="address", address="0.0.0.0")
    video = _sub(devs, "video")
    _sub(video, "model", type_="vga" if windows else "virtio")

    rng = _sub(devs, "rng", model="virtio")
    _sub(rng, "backend", "/dev/urandom", model="random")
    _sub(devs, "memballoon", model="virtio")

    for a in spec.get("hostdevs", []):
        hd = _sub(devs, "hostdev", mode="subsystem", type_="pci", managed="yes")
        src = _sub(hd, "source")
        _sub(src, "address", domain="0x%04x" % a[0], bus="0x%02x" % a[1],
             slot="0x%02x" % a[2], function="0x%x" % a[3])

    if windows and uefi:
        tpm = _sub(devs, "tpm", model="tpm-crb")
        _sub(tpm, "backend", type_="emulator", version="2.0")

    return ET.tostring(dom, encoding="unicode")
