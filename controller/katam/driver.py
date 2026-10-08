"""Talks to libvirtd on every node. libvirt is the source of truth for VM state."""
import logging
import random
import secrets
import socket
import threading
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import libvirt

from . import cloudinit, xmlgen

log = logging.getLogger("katam.driver")

GPU_CLASSES = ("0x0300", "0x0302")  # VGA controller, 3D controller

STATE = {
    libvirt.VIR_DOMAIN_NOSTATE: "unknown",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "running",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "stopping",
    libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
}

UNDEFINE_FLAGS = (
    libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
    | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
    | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
    | getattr(libvirt, "VIR_DOMAIN_UNDEFINE_TPM", 0)
)


class ClusterError(Exception):
    """A problem the operator can fix; shown verbatim in the UI."""


def _start_event_loop():
    # Needed for keepalives, so dead SSH connections are noticed and reopened.
    libvirt.virEventRegisterDefaultImpl()

    def loop():
        while True:
            libvirt.virEventRunDefaultImpl()

    threading.Thread(target=loop, name="libvirt-events", daemon=True).start()


class Cluster:
    def __init__(self, cfg):
        libvirt.registerErrorHandler(lambda ctx, err: None, None)
        _start_event_loop()
        self.cfg = cfg
        self.nodes = {n["name"]: n for n in cfg["nodes"]}
        self.storage = cfg["storage"]
        self.rbd_mode = self.storage["type"] == "rbd"
        self._conns = {}
        self._lock = threading.RLock()
        self._gpu_lock = threading.Lock()
        self._node_locks = {}
        self._down = {}
        self.sdn = None          # set by the app when SDN is enabled
        self._io = ThreadPoolExecutor(max_workers=len(self.nodes) * 2)

    # ---- connections -------------------------------------------------
    def conn(self, node):
        if node not in self.nodes:
            raise ClusterError(f"Unknown node '{node}'")
        with self._lock:
            c = self._conns.get(node)
            node_lock = self._node_locks.setdefault(node, threading.Lock())
        if c is not None:
            try:
                if c.isAlive():
                    return c
            except libvirt.libvirtError:
                pass
        with node_lock:
            with self._lock:
                c = self._conns.get(node)
            if c is not None:
                try:
                    if c.isAlive():
                        return c
                    c.close()
                except libvirt.libvirtError:
                    pass
            if self._down.get(node, 0) > time.time():
                raise ClusterError(f"Can't reach {node} (retrying shortly)")
            uri = self.nodes[node]["uri"]
            target = _uri_endpoint(uri)
            if target:
                # Fail fast: libvirt over SSH can hang for minutes on a dead host.
                try:
                    socket.create_connection(target, timeout=3).close()
                except OSError as e:
                    self._down[node] = time.time() + 20
                    raise ClusterError(f"Can't reach {node} at {target[0]}:{target[1]}: {e}") from e
            try:
                c = libvirt.open(uri)
            except libvirt.libvirtError as e:
                self._down[node] = time.time() + 20
                raise ClusterError(f"Can't reach {node}: {e.get_error_message()}") from e
            self._down.pop(node, None)
            try:
                c.setKeepAlive(5, 3)
            except libvirt.libvirtError:
                pass  # local/test drivers have no keepalive
            with self._lock:
                self._conns[node] = c
            return c

    def _online(self):
        from concurrent.futures import ThreadPoolExecutor

        def probe(name):
            try:
                self.conn(name)
                return name
            except ClusterError:
                return None
        with ThreadPoolExecutor(max_workers=max(1, len(self.nodes))) as ex:
            return [n for n in ex.map(probe, list(self.nodes)) if n]

    # ---- inventory ---------------------------------------------------
    def node_summary(self, node):
        try:
            c = self.conn(node)
            model, mem_mb, threads, mhz, *_ = c.getInfo()
            committed = vcpus = running = count = 0
            for d in c.listAllDomains():
                st, maxmem, _, nvcpu, _ = d.info()
                committed += maxmem // 1024
                vcpus += nvcpu
                count += 1
                running += st in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_BLOCKED)
            gtotal, gfree = self._gpu_counts(node)
            return {
                "name": node, "online": True, "host": self.nodes[node]["host"],
                "cpu_model": model, "threads": threads, "mhz": mhz,
                "memory_mb": mem_mb, "free_mb": c.getFreeMemory() // 2**20,
                "committed_mb": committed, "vcpus_allocated": vcpus,
                "vm_count": count, "running": running,
                "reserve_mb": self.cfg["scheduler"]["host_reserve_mb"],
                "gpu_total": gtotal, "gpu_free": gfree,
            }
        except (ClusterError, libvirt.libvirtError) as e:
            return {"name": node, "online": False, "host": self.nodes[node]["host"], "error": str(e)}

    def nodes_summary(self):
        return list(self._io.map(self.node_summary, self.nodes))

    def _meta(self, d):
        try:
            raw = d.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, xmlgen.META_NS)
        except libvirt.libvirtError:
            return {}
        return {child.tag.split("}")[-1]: child.text for child in ET.fromstring(raw)}

    @staticmethod
    def _ips(d):
        try:
            ifaces = d.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT, 0)
        except libvirt.libvirtError:
            return []  # no guest agent yet
        ips = []
        for name, val in ifaces.items():
            if name == "lo":
                continue
            for a in val.get("addrs") or []:
                if a["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4 and not a["addr"].startswith("127."):
                    ips.append(a["addr"])
        return ips

    def _vm_dict(self, node, d):
        st, maxmem, _, nvcpu, _ = d.info()
        state = STATE.get(st, "unknown")
        root = ET.fromstring(d.XMLDesc(0))
        gpus = sum(1 for a in root.findall("devices/hostdev[@type='pci']/source/address")
                   if int(a.get("function"), 16) == 0)
        meta = self._meta(d)
        return {
            **meta,
            "project": meta.get("project") or "default",
            "autostart": meta.get("autostart") == "1",
            "ha": meta.get("ha", "1") == "1",
            "secgroups": [x for x in (meta.get("secgroups") or "").split(",") if x],
            "name": d.name(), "uuid": d.UUIDString(), "node": node, "state": state,
            "vcpus": nvcpu, "memory_mb": maxmem // 1024,
            "gpus": gpus,
            "ips": self._ips(d) if state == "running" else [],
        }

    def _node_vms(self, node):
        try:
            c = self.conn(node)
            return [self._vm_dict(node, d) for d in c.listAllDomains()]
        except (ClusterError, libvirt.libvirtError) as e:
            log.warning("listing VMs on %s failed: %s", node, e)
            return []

    def list_vms(self):
        vms = [vm for chunk in self._io.map(self._node_vms, self.nodes) for vm in chunk]
        return sorted(vms, key=lambda v: v["name"])

    def find(self, name):
        for node in self._online():
            try:
                return node, self.conn(node).lookupByName(name)
            except libvirt.libvirtError:
                continue
        return None

    def _must(self, name):
        hit = self.find(name)
        if not hit:
            raise ClusterError(f"No VM named '{name}' on any reachable node")
        return hit

    def list_isos(self):
        for node in self._online():
            try:
                pool = self.conn(node).storagePoolLookupByName(self.storage["iso_pool"])
                pool.refresh(0)
                return sorted(v for v in pool.listVolumes() if v.lower().endswith(".iso"))
            except libvirt.libvirtError:
                continue
        return []

    # ---- GPUs (VFIO passthrough) -------------------------------------
    @staticmethod
    def _pci(a):
        return tuple(int(a.get(k), 16) for k in ("domain", "bus", "slot", "function"))

    @staticmethod
    def _pci_str(t):
        return "%04x:%02x:%02x.%x" % t

    def node_gpus(self, node):
        """Whole GPUs on a node, each with the PCI functions that must pass through with it."""
        c = self.conn(node)
        used = {}
        for d in c.listAllDomains():
            root = ET.fromstring(d.XMLDesc(0))
            for a in root.findall("devices/hostdev[@type='pci']/source/address"):
                used[self._pci(a)] = d.name()
        gpus = []
        for dev in c.listAllDevices(libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_PCI_DEV):
            cap = ET.fromstring(dev.XMLDesc(0)).find("capability[@type='pci']")
            if cap is None or not (cap.findtext("class") or "").startswith(GPU_CLASSES):
                continue
            addr = tuple(int(cap.findtext(k)) for k in ("domain", "bus", "slot", "function"))
            funcs = []
            for a in cap.findall("iommuGroup/address"):
                t = self._pci(a)
                try:
                    peer = c.nodeDeviceLookupByName("pci_%04x_%02x_%02x_%x" % t)
                    pcap = ET.fromstring(peer.XMLDesc(0)).find("capability[@type='pci']")
                    if (pcap.findtext("class") or "").startswith("0x0604"):
                        continue  # PCI bridges stay with the host
                except libvirt.libvirtError:
                    pass
                funcs.append(t)
            gpus.append({
                "address": self._pci_str(addr),
                "vendor": cap.findtext("vendor") or "",
                "model": cap.findtext("product") or "GPU",
                "functions": funcs or [addr],
                "used_by": used.get(addr),
            })
        return gpus

    def _gpu_counts(self, node):
        try:
            g = self.node_gpus(node)
        except libvirt.libvirtError:
            return 0, 0
        return len(g), sum(1 for x in g if not x["used_by"])

    def gpu_models(self):
        models = {}
        for node in self._online():
            try:
                for g in self.node_gpus(node):
                    m = models.setdefault(g["model"], {"model": g["model"], "total": 0, "free": 0})
                    m["total"] += 1
                    m["free"] += 0 if g["used_by"] else 1
            except libvirt.libvirtError:
                continue
        return sorted(models.values(), key=lambda m: m["model"])

    def _free_gpus(self, node, model=None):
        return [g for g in self.node_gpus(node)
                if not g["used_by"] and (not model or g["model"] == model)]

    # ---- scheduling --------------------------------------------------
    def choose_node(self, memory_mb, gpus=0, gpu_model=None):
        sch = self.cfg["scheduler"]
        best, best_room = None, None
        for s in self.nodes_summary():
            if not s["online"]:
                continue
            if gpus and len(self._free_gpus(s["name"], gpu_model)) < gpus:
                continue
            room = s["memory_mb"] * sch["memory_overcommit"] - sch["host_reserve_mb"] - s["committed_mb"]
            if best_room is None or room > best_room:
                best, best_room = s["name"], room
        if best is None:
            if gpus:
                raise ClusterError(f"No reachable node has {gpus} free GPU(s)"
                                   + (f" of model {gpu_model}" if gpu_model else ""))
            raise ClusterError("No nodes are reachable")
        if best_room < memory_mb:
            raise ClusterError(
                f"Not enough memory: the emptiest node ({best}) has {int(best_room)} MB "
                f"free after the host reserve, the VM needs {memory_mb} MB")
        return best

    # ---- helpers -----------------------------------------------------
    @staticmethod
    def _mac():
        return "52:54:00:%02x:%02x:%02x" % tuple(random.randint(0, 255) for _ in range(3))

    def _disk_ref(self, vol, device="disk", bus="virtio", boot=None):
        if self.rbd_mode:
            src = f"{self.storage['rbd']['ceph_pool']}/{vol.name()}"
            return {"kind": "rbd", "device": device, "source": src, "bus": bus, "boot": boot}
        return {"kind": "file", "device": device, "format": "qcow2",
                "source": vol.path(), "bus": bus, "boot": boot}

    @staticmethod
    def _upload(c, vol, data):
        stream = c.newStream(0)
        try:
            vol.upload(stream, 0, len(data), 0)
            off = 0
            while off < len(data):
                off += stream.send(data[off:off + (1 << 20)])
            stream.finish()
        except libvirt.libvirtError:
            stream.abort()
            raise

    def _image(self, name):
        for img in self.cfg["images"]:
            if img["name"] == name:
                return img
        raise ClusterError(f"Unknown image '{name}'")

    def _network(self, name):
        return self.network(name)

    def network(self, name):
        for net in self.cfg["networks"]:
            if net["name"] == name:
                return net
        raise ClusterError(f"Unknown network '{name}'")

    # ---- lifecycle ---------------------------------------------------
    def create_vm(self, req, progress=lambda m: None):
        name = req["name"]
        if self.find(name):
            raise ClusterError(f"A VM named '{name}' already exists")
        want_gpus = req.get("gpus", 0)
        node = req.get("node") or self.choose_node(req["memory_mb"], want_gpus, req.get("gpu_model"))
        c = self.conn(node)
        st = self.storage
        windows = req["os_type"] == "windows"
        created = []
        gpu_locked = False

        try:
            pool = c.storagePoolLookupByName(st["pool"])
            pool.refresh(0)
            want = req["disk_gb"] * 2**30
            vol_name = f"{name}-disk0"
            disks = []

            progress(f"Creating {req['disk_gb']} GB disk on {node}")
            img = req.get("image_rec")
            if img:
                vols = img["volumes"]
                for i, base_name in enumerate(vols):
                    try:
                        base = pool.storageVolLookupByName(base_name)
                    except libvirt.libvirtError:
                        raise ClusterError(
                            f"Base volume '{base_name}' isn't in pool '{st['pool']}' on {node}")
                    base_cap = base.info()[1]
                    this_name = vol_name if i == 0 else f"{name}-disk{i}"
                    this_want = want if i == 0 else base_cap
                    if self.rbd_mode:
                        # libvirt snapshots + clones the base image: instant, copy-on-write.
                        vol = pool.createXMLFrom(xmlgen.volume_xml(this_name, base_cap), base, 0)
                    else:
                        backing = (base.path(), img.get("format") or "qcow2")
                        vol = pool.createXML(xmlgen.volume_xml(
                            this_name, max(this_want, base_cap), "qcow2", backing), 0)
                    created.append(vol)
                    if vol.info()[1] < this_want:
                        vol.resize(this_want, 0)
                    disks.append(self._disk_ref(vol, boot=1 if i == 0 else None))
                for j, extra_gb in enumerate(req.get("data_disks") or [], start=len(vols)):
                    v = pool.createXML(xmlgen.volume_xml(
                        f"{name}-disk{j}", extra_gb * 2**30, None if self.rbd_mode else "qcow2"), 0)
                    created.append(v)
                    disks.append(self._disk_ref(v))
            else:
                vol = pool.createXML(
                    xmlgen.volume_xml(vol_name, want, None if self.rbd_mode else "qcow2"), 0)
                created.append(vol)
                disks.append(self._disk_ref(vol, boot=2))
                iso_pool = c.storagePoolLookupByName(st["iso_pool"])
                iso_pool.refresh(0)
                media = [(req["iso"], 1)]
                if windows and st.get("virtio_win_iso"):
                    media.append((st["virtio_win_iso"], None))
                for iso_name, boot in media:
                    try:
                        iso = iso_pool.storageVolLookupByName(iso_name)
                    except libvirt.libvirtError:
                        raise ClusterError(f"ISO '{iso_name}' isn't in the ISO pool on {node}")
                    disks.append({"kind": "file", "device": "cdrom", "format": "raw",
                                  "source": iso.path(), "bus": "sata", "boot": boot})

            nics = []
            for i, net_name in enumerate(req["networks"]):
                net = self._network(net_name)
                nic = {"mac": self._mac(), "dhcp": net.get("dhcp", False), "network": net_name}
                if net["type"] == "bridge":
                    nic["bridge"] = net["bridge"]
                else:
                    nic["bridge"] = "br-int"
                    # libvirt wants a UUID; it becomes the OVN logical port name.
                    nic["iface_id"] = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"katam:{name}:{i}"))
                    nic["ip"] = (req.get("ip") or "").split("/")[0] if i == 0 else None
                nics.append(nic)
            if self.sdn and any(n.get("iface_id") for n in nics):
                progress("Creating SDN ports")
                self.sdn.attach_ports(name, req.get("project", "default"), nics,
                                      req.get("secgroups") or [])

            if img and img.get("cloud_init", True) and not windows:
                progress("Writing cloud-init seed")
                first = self._network(req["networks"][0])
                static = None
                if req.get("ip"):
                    static = {"address": req["ip"],
                              "gateway": req.get("gateway") or first.get("gateway"),
                              "dns": first.get("dns", [])}
                ci = self.cfg["cloud_init"]
                data = cloudinit.build_seed(
                    hostname=name, instance_id=str(uuidlib.uuid4()),
                    user=ci["default_user"], ssh_keys=req.get("ssh_keys") or ci["ssh_keys"],
                    nics=nics, static=static, password=req.get("password"))
                seed_pool = c.storagePoolLookupByName(st["seed_pool"])
                seed = seed_pool.createXML(xmlgen.volume_xml(f"{name}-seed.iso", len(data), "raw"), 0)
                created.append(seed)
                self._upload(c, seed, data)
                disks.append({"kind": "file", "device": "cdrom", "format": "raw",
                              "source": seed.path(), "bus": "sata"})

            hostdevs, gpu_model = [], None
            if want_gpus:
                # Held until the VM is defined, so two creates can't grab the same GPU.
                self._gpu_lock.acquire()
                gpu_locked = True
                free = self._free_gpus(node, req.get("gpu_model"))
                if len(free) < want_gpus:
                    raise ClusterError(f"{node} has only {len(free)} free GPU(s) of that model")
                gpu_model = free[0]["model"]
                for g in free[:want_gpus]:
                    progress(f"Passing through GPU {g['address']} ({g['model']})")
                    hostdevs.extend(g["functions"])

            spec = {
                "hostdevs": hostdevs,
                "name": name, "uuid": str(uuidlib.uuid4()), "vcpus": req["vcpus"],
                "memory_mb": req["memory_mb"], "os_type": req["os_type"],
                "firmware": req.get("firmware", "bios"), "disks": disks, "nics": nics,
                "vnc_password": secrets.token_hex(4), "description": req.get("description"),
            }
            progress("Defining VM")
            dom = c.defineXML(xmlgen.domain_xml(spec, st.get("rbd")))
        except Exception:
            if gpu_locked:
                self._gpu_lock.release()
            for v in created:
                try:
                    v.delete(0)
                except libvirt.libvirtError:
                    pass
            raise
        if gpu_locked:
            self._gpu_lock.release()

        dom.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                        xmlgen.metadata_xml({
                            "os_type": req["os_type"],
                            "source": (img or {}).get("name") or req.get("iso"),
                            "networks": ",".join(req["networks"]),
                            "gpu_model": gpu_model,
                            "project": req.get("project", "default"),
                            "owner": req.get("owner"),
                            "template": req.get("template"),
                            "ha": "1" if req.get("ha", True) else "0",
                            "autostart": "1" if req.get("autostart") else "0",
                            "secgroups": ",".join(req.get("secgroups") or []),
                            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        }),
                        "katam", xmlgen.META_NS, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
        if req.get("start", True):
            progress("Starting")
            dom.create()
        return f"Created {name} on {node}"

    def power(self, name, action):
        _, d = self._must(name)
        ops = {
            "start": d.create,
            "shutdown": d.shutdown,
            "poweroff": d.destroy,
            "reboot": lambda: d.reboot(0),
            "pause": d.suspend,
            "resume": d.resume,
        }
        if action not in ops:
            raise ClusterError(f"Unknown action '{action}'")
        ops[action]()

    def set_meta(self, name, **fields):
        _, d = self._must(name)
        meta = self._meta(d)
        meta.update({k: v for k, v in fields.items()})
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if d.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        d.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, xmlgen.metadata_xml(meta),
                      "katam", xmlgen.META_NS, flags)

    def set_autostart(self, name, on):
        # Katam starts these VMs itself; libvirt autostart stays off so a fenced node
        # that powers back on can't start a second copy of a VM that HA restarted elsewhere.
        self.set_meta(name, autostart="1" if on else "0")

    def _owned_volumes(self, c, root, name):
        """Volumes this VM created (name-*), never shared bases or ISOs."""
        vols = []
        for disk in root.findall("devices/disk"):
            src = disk.find("source")
            if src is None:
                continue
            try:
                if src.get("file"):
                    vol = c.storageVolLookupByPath(src.get("file"))
                elif src.get("protocol") == "rbd":
                    image = src.get("name").split("/", 1)[1]
                    vol = c.storagePoolLookupByName(self.storage["pool"]).storageVolLookupByName(image)
                else:
                    continue
            except libvirt.libvirtError:
                continue
            if vol.name().startswith(name + "-"):
                vols.append(vol)
        return vols

    @staticmethod
    def _undefine(d):
        try:
            d.undefineFlags(UNDEFINE_FLAGS)
        except libvirt.libvirtError as e:
            if e.get_error_code() not in (libvirt.VIR_ERR_INVALID_ARG, libvirt.VIR_ERR_NO_SUPPORT):
                raise
            d.undefine()  # older libvirt without TPM/NVRAM flags

    def delete_vm(self, name, progress=lambda m: None):
        node, d = self._must(name)
        c = self.conn(node)
        if d.isActive():
            progress("Powering off")
            d.destroy()
        root = ET.fromstring(d.XMLDesc(0))
        vols = self._owned_volumes(c, root, name)
        self._undefine(d)
        for v in vols:
            progress(f"Deleting {v.name()}")
            v.delete(0)
        if self.sdn:
            self.sdn.detach_vm(name)
        return f"Deleted {name} and {len(vols)} volume(s)"

    def _eject_local_media(self, c, d):
        """Local CD-ROM files don't exist on other nodes; eject them before a move."""
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if d.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        root = ET.fromstring(d.XMLDesc(0))
        for disk in root.findall("devices/disk[@device='cdrom']"):
            src = disk.find("source")
            if src is None or not src.get("file"):
                continue
            path = src.get("file")
            disk.remove(src)
            d.updateDeviceFlags(ET.tostring(disk, encoding="unicode"), flags)
            if path.endswith(f"{d.name()}-seed.iso"):
                try:
                    c.storageVolLookupByPath(path).delete(0)
                except libvirt.libvirtError:
                    pass

    def migrate_vm(self, name, dest, progress=lambda m: None):
        node, d = self._must(name)
        if dest == node:
            raise ClusterError(f"{name} is already on {dest}")
        if ET.fromstring(d.XMLDesc(0)).find("devices/hostdev") is not None:
            raise ClusterError(f"{name} has a passthrough GPU, so it's pinned to {node}. "
                               "Shut it down and recreate it on the target node instead.")
        c, dc = self.conn(node), self.conn(dest)
        progress("Ejecting local media")
        self._eject_local_media(c, d)

        if d.isActive():
            flags = (libvirt.VIR_MIGRATE_LIVE | libvirt.VIR_MIGRATE_PERSIST_DEST
                     | libvirt.VIR_MIGRATE_UNDEFINE_SOURCE | libvirt.VIR_MIGRATE_AUTO_CONVERGE)
            if not self.rbd_mode:
                flags |= libvirt.VIR_MIGRATE_NON_SHARED_DISK
            target = self.nodes[dest].get("migration_address", self.nodes[dest]["host"])
            progress(f"Live-migrating {node} to {dest}")
            d.migrate3(dc, {libvirt.VIR_MIGRATE_PARAM_URI: f"tcp://{target}"}, flags)
        else:
            if not self.rbd_mode:
                raise ClusterError("Moving a stopped VM needs shared (Ceph) storage; start it and live-migrate instead")
            progress(f"Moving definition {node} to {dest}")
            xml = d.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE | libvirt.VIR_DOMAIN_XML_MIGRATABLE)
            dc.defineXML(xml)
            self._undefine(d)
        return f"Moved {name} to {dest}"

    def console(self, name):
        node, d = self._must(name)
        if not d.isActive():
            raise ClusterError("Start the VM to open its console")
        root = ET.fromstring(d.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE))
        g = root.find("devices/graphics[@type='vnc']")
        if g is None or g.get("port") in (None, "-1"):
            raise ClusterError("This VM has no VNC display")
        return self.nodes[node]["host"], int(g.get("port")), g.get("passwd")

    # ---- data for HA, DR and FinOps ----------------------------------
    def vm_disk_gb(self, node, d):
        c = self.conn(node)
        root = ET.fromstring(d.XMLDesc(0))
        total = 0
        for v in self._owned_volumes(c, root, d.name()):
            if not v.name().endswith(".iso"):
                total += v.info()[1]
        return round(total / 2**30, 1)

    def snapshot_inventory(self):
        """Per-VM facts for metering and HA backups, one pass over every node."""
        out = []
        for node in self._online():
            c = self.conn(node)
            for d in c.listAllDomains():
                try:
                    vm = self._vm_dict(node, d)
                    vm["disk_gb"] = self.vm_disk_gb(node, d)
                    vm["cpu_time_ns"] = d.info()[4]
                    vm["xml"] = d.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE | libvirt.VIR_DOMAIN_XML_MIGRATABLE)
                    out.append(vm)
                except libvirt.libvirtError as e:
                    log.warning("inventory of %s on %s failed: %s", d.name(), node, e)
        return out

    def define_from_xml(self, node, xml, start=True, strip_local_media=True):
        """Recreate a VM on another node from a saved definition (HA restart, DR)."""
        root = ET.fromstring(xml)
        if strip_local_media:
            for disk in root.findall("devices/disk[@device='cdrom']"):
                src = disk.find("source")
                if src is not None and src.get("file"):
                    disk.remove(src)
        c = self.conn(node)
        d = c.defineXML(ET.tostring(root, encoding="unicode"))
        if start:
            d.create()
        return d

    def undefine_stale(self, node, names):
        """On a recovered node, remove definitions of VMs that now live elsewhere."""
        c = self.conn(node)
        removed = []
        for n in names:
            try:
                d = c.lookupByName(n)
            except libvirt.libvirtError:
                continue
            if d.isActive():
                d.destroy()
            self._undefine(d)
            removed.append(n)
        return removed

    def disk_sources(self, name):
        """[(kind, source)] for the VM's own disks: ('rbd', 'vms/x-disk0') or ('file', path)."""
        node, d = self._must(name)
        root = ET.fromstring(d.XMLDesc(0))
        out = []
        for disk in root.findall("devices/disk[@device='disk']"):
            src = disk.find("source")
            if src is None:
                continue
            if src.get("protocol") == "rbd":
                out.append(("rbd", src.get("name")))
            elif src.get("file"):
                out.append(("file", src.get("file")))
        return node, out


def _uri_endpoint(uri):
    """(host, port) to probe for a remote libvirt URI, or None for local/test URIs."""
    from urllib.parse import urlparse
    u = urlparse(uri)
    if not u.hostname:
        return None
    if "+ssh" in u.scheme or u.scheme == "ssh":
        return (u.hostname, u.port or 22)
    if "+tcp" in u.scheme:
        return (u.hostname, u.port or 16509)
    if "+tls" in u.scheme:
        return (u.hostname, u.port or 16514)
    return None
