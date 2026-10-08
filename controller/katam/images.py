"""Onboard images (qcow2, raw, VMDK, VHD/VHDX, ISO-less OVA) and export VMs (OVA, VMDK, qcow2, VHDX, raw)."""
import json
import os
import posixpath
import re
import shutil
import time

from . import ova
from .db import images, insert, select, templates, update
from .driver import ClusterError

DISK_FORMATS = {"qcow2", "raw", "vmdk", "vpc", "vhdx", "vdi"}
EXPORT_FORMATS = {"ova": "ova", "vmdk": "vmdk", "qcow2": "qcow2", "vhdx": "vhdx", "raw": "img"}


class ImageService:
    def __init__(self, cfg, db, cluster, nexec):
        self.cfg, self.db, self.cluster, self.x = cfg, db, cluster, nexec
        self.st = cfg["storage"]
        self.rbd = self.st["type"] == "rbd"
        os.makedirs(cfg["controller"]["staging_dir"], exist_ok=True)
        os.makedirs(cfg["controller"]["exports_dir"], exist_ok=True)

    # ---- helpers -----------------------------------------------------
    def _primary_nodes(self):
        return [n["name"] for n in self.cfg["nodes"] if n["site"] == "primary"]

    def _info(self, node, path):
        return json.loads(self.x.run(node, ["qemu-img", "info", "--output=json", path], sudo=True))

    def _target(self, vol):
        if self.rbd:
            return f"rbd:{self.st['rbd']['ceph_pool']}/{vol}"
        return posixpath.join(self.st.get("dir_path", "/var/lib/katam/images"), vol)

    def _refresh_pools(self):
        for node in self.cluster._online():
            try:
                self.cluster.conn(node).storagePoolLookupByName(self.st["pool"]).refresh(0)
            except Exception:  # noqa: BLE001 - a node may be down; it refreshes on its next use
                pass

    def _land_disk(self, node, src, vol, progress):
        """Convert a disk file on `node` into a base volume available to every node."""
        if self.rbd:
            progress(f"Converting {posixpath.basename(src)} into Ceph volume {vol}")
            self.x.run(node, ["qemu-img", "convert", "-O", "raw", src, self._target(vol)], sudo=True)
            return
        out = self._target(vol)
        progress(f"Converting {posixpath.basename(src)} to qcow2")
        self.x.run(node, ["qemu-img", "convert", "-O", "qcow2", src, out], sudo=True)
        others = [n for n in self._primary_nodes() if n != node]
        if others:
            local = os.path.join(self.cfg["controller"]["staging_dir"], vol)
            self.x.run(node, ["chmod", "644", out], sudo=True)
            self.x.pull(node, out, local)
            for n in others:
                progress(f"Copying {vol} to {n}")
                tmp = posixpath.join(self.st["node_staging"], vol)
                self.x.push(n, local, tmp)
                self.x.run(n, ["mv", tmp, out], sudo=True)
            os.remove(local)

    # ---- import ------------------------------------------------------
    def begin(self, name, project, label):
        if self.db.one(select(images).where(images.c.name == name)):
            raise ClusterError(f"An image named '{name}' already exists")
        self.db.run(insert(images).values(name=name, label=label or name, project=project,
                                          volumes="[]", status="importing", created=time.time()))

    def import_image(self, name, source, progress, kind="disk", os_type="linux", firmware="bios",
                     cloud_init=True, v2v=False, make_template=True, networks=None):
        """source: {'upload': local_path_on_controller} or {'url': 'https://...'}"""
        node = self.st["import_node"]
        work = posixpath.join(self.st["node_staging"], f"import-{name}-{int(time.time())}")
        self.x.run(node, ["mkdir", "-p", work], sudo=True)
        self.x.run(node, ["chmod", "777", work], sudo=True)
        try:
            if "url" in source:
                fname = re.sub(r"[^A-Za-z0-9._-]", "_", source["url"].rsplit("/", 1)[-1].split("?")[0]) or "download"
                progress(f"Downloading {source['url']} on {node}")
                self.x.run(node, ["curl", "-fsSL", "--retry", "3", "-o", posixpath.join(work, fname),
                                  source["url"]], sudo=True, timeout=6 * 3600)
            else:
                fname = os.path.basename(source["upload"])
                progress(f"Copying upload to {node}")
                self.x.push(node, source["upload"], posixpath.join(work, fname))
            path = posixpath.join(work, fname)
            if fname.lower().endswith(".ova"):
                kind = "ova"

            ovf = None
            if kind == "ova" and v2v:
                progress("Converting with virt-v2v (installs virtio drivers, removes VMware tools)")
                out = posixpath.join(work, "v2v")
                self.x.run(node, ["mkdir", "-p", out], sudo=True)
                self.x.run(node, ["virt-v2v", "-i", "ova", path, "-o", "local", "-os", out, "-of", "qcow2"],
                           sudo=True, timeout=6 * 3600)
                listing = self.x.run(node, ["ls", "-1", out], sudo=True).split()
                disk_files = sorted(posixpath.join(out, f) for f in listing if re.search(r"-sd[a-z]$", f))
                xmlf = [f for f in listing if f.endswith(".xml")]
                if xmlf:
                    import xml.etree.ElementTree as ET
                    dx = ET.fromstring(self.x.run(node, ["cat", posixpath.join(out, xmlf[0])], sudo=True))
                    ovf = {"vcpus": int(dx.findtext("vcpu") or 2),
                           "memory_mb": int(dx.findtext("memory") or 2097152) // 1024,
                           "os_type": "windows" if "windows" in (dx.findtext("description") or "").lower() else os_type,
                           "firmware": "uefi" if dx.find("os/loader") is not None else "bios"}
            elif kind == "ova":
                progress("Unpacking OVA")
                self.x.run(node, ["tar", "-xf", path, "-C", work], sudo=True)
                listing = self.x.run(node, ["ls", "-1", work], sudo=True).split()
                ovf_name = next((f for f in listing if f.lower().endswith(".ovf")), None)
                if not ovf_name:
                    raise ClusterError("The OVA has no .ovf descriptor")
                ovf = ova.parse_ovf(self.x.run(node, ["cat", posixpath.join(work, ovf_name)], sudo=True))
                disk_files = []
                for d in ovf["disks"]:
                    f = posixpath.join(work, d["file"])
                    if d["file"].endswith(".gz"):
                        self.x.run(node, ["gunzip", "-f", f], sudo=True)
                        f = f[:-3]
                    disk_files.append(f)
                os_type, firmware = ovf["os_type"], ovf["firmware"]
                progress(f"OVF: {ovf['vcpus']} vCPU, {ovf['memory_mb']} MB, {len(disk_files)} disk(s), {os_type}")
            else:
                disk_files = [path]

            if not disk_files:
                raise ClusterError("No disks found to import")
            vols, size_gb = [], 0.0
            for i, f in enumerate(disk_files):
                info = self._info(node, f)
                if info.get("format") not in DISK_FORMATS:
                    raise ClusterError(f"{posixpath.basename(f)} is {info.get('format')}, not a disk image")
                if i == 0:
                    size_gb = round(info["virtual-size"] / 2**30, 1)
                vol = f"img-{name}-{i}"
                self._land_disk(node, f, vol, progress)
                vols.append(vol)
            self._refresh_pools()

            if ovf:
                os_type = ovf.get("os_type", os_type)
                firmware = ovf.get("firmware", firmware)
                cloud_init = False  # appliances bring their own configuration
            self.db.run(update(images).where(images.c.name == name).values(
                volumes=json.dumps(vols), os_type=os_type, firmware=firmware, size_gb=size_gb,
                format="raw" if self.rbd else "qcow2", cloud_init=cloud_init,
                source=source.get("url") or os.path.basename(source.get("upload", "")), status="ready"))

            if ovf and make_template:
                img = self.db.one(select(images).where(images.c.name == name))
                spec = {"image": name, "os_type": os_type, "firmware": firmware,
                        "vcpus": ovf["vcpus"], "memory_gb": max(1, round(ovf["memory_mb"] / 1024)),
                        "disk_gb": max(1, int(size_gb + 0.99)),
                        "networks": (networks or [self.cfg["networks"][0]["name"]]) * 1}
                if not self.db.one(select(templates).where(templates.c.name == name)):
                    self.db.run(insert(templates).values(name=name, label=img["label"], project=img["project"],
                                                         spec=json.dumps(spec), created=time.time()))
                progress(f"Created template {name}")
            return f"Imported {name}: {len(vols)} volume(s), {size_gb} GB"
        except Exception:
            self.db.run(update(images).where(images.c.name == name).values(status="failed"))
            raise
        finally:
            self.x.run(node, ["rm", "-rf", work], sudo=True, check=False)
            if "upload" in source and os.path.exists(source["upload"]):
                os.remove(source["upload"])

    # ---- capture a stopped VM as an image + template -----------------
    def capture(self, vm, name, project, progress):
        node, disks = self.cluster.disk_sources(vm)
        _, d = self.cluster._must(vm)
        if d.isActive():
            raise ClusterError("Shut the VM down before capturing it")
        self.begin(name, project, name)
        vols = []
        try:
            for i, (kind, src) in enumerate(disks):
                vol = f"img-{name}-{i}"
                source = f"rbd:{src}" if kind == "rbd" else src
                self._land_disk(node, source, vol, progress)
                vols.append(vol)
            self._refresh_pools()
            meta = self.cluster._meta(d)
            info = d.info()
            self.db.run(update(images).where(images.c.name == name).values(
                volumes=json.dumps(vols), os_type=meta.get("os_type", "linux"), status="ready",
                format="raw" if self.rbd else "qcow2", cloud_init=False, source=f"vm:{vm}"))
            spec = {"image": name, "os_type": meta.get("os_type", "linux"), "vcpus": info[3],
                    "memory_gb": max(1, info[1] // 2**20),
                    "disk_gb": max(1, int(self.cluster.vm_disk_gb(node, d))),
                    "networks": [n for n in (meta.get("networks") or "").split(",") if n] or
                                [self.cfg["networks"][0]["name"]]}
            self.db.run(insert(templates).values(name=name, label=name, project=project,
                                                 spec=json.dumps(spec), created=time.time()))
            return f"Captured {vm} as image and template {name}"
        except Exception:
            self.db.run(update(images).where(images.c.name == name).values(status="failed"))
            raise

    # ---- export ------------------------------------------------------
    def export_vm(self, vm, fmt, progress):
        if fmt not in EXPORT_FORMATS:
            raise ClusterError(f"Export format must be one of {', '.join(EXPORT_FORMATS)}")
        _, d = self.cluster._must(vm)
        if d.isActive():
            raise ClusterError("Shut the VM down before exporting, so the disks are consistent")
        node, disks = self.cluster.disk_sources(vm)
        meta = self.cluster._meta(d)
        info = d.info()
        stamp = time.strftime("%Y%m%d-%H%M")
        work = posixpath.join(self.st["node_staging"], f"export-{vm}-{stamp}")
        self.x.run(node, ["mkdir", "-p", work], sudo=True)
        try:
            produced = []
            for i, (kind, src) in enumerate(disks):
                source = f"rbd:{src}" if kind == "rbd" else src
                ext = "vmdk" if fmt == "ova" else EXPORT_FORMATS[fmt]
                out = posixpath.join(work, f"{vm}-disk{i}.{ext}")
                progress(f"Converting disk {i} to {ext}")
                args = ["qemu-img", "convert", "-O"]
                if ext == "vmdk":
                    args += ["vmdk", "-o", "subformat=streamOptimized"]
                elif ext == "vhdx":
                    args += ["vhdx", "-o", "subformat=dynamic"]
                elif ext == "img":
                    args += ["raw"]
                else:
                    args += [ext]
                self.x.run(node, args + [source, out], sudo=True, timeout=6 * 3600)
                vsize = self._info(node, source)["virtual-size"]
                fsize = int(self.x.run(node, ["stat", "-c", "%s", out], sudo=True).strip())
                produced.append({"path": out, "file": posixpath.basename(out), "size": fsize, "capacity": vsize})

            if fmt == "ova":
                progress("Writing OVF descriptor and manifest")
                ovf_text = ova.build_ovf(vm, info[3], info[1] // 1024, produced,
                                         os_type=meta.get("os_type", "linux"),
                                         firmware="uefi" if "efi" in d.XMLDesc(0) else "bios",
                                         nics=max(1, len([n for n in (meta.get("networks") or "").split(",") if n])))
                self.x.write(node, posixpath.join(work, f"{vm}.ovf"), ovf_text.encode(), sudo=True)
                files = [f"{vm}.ovf"] + [p["file"] for p in produced]
                sums = self.x.run(node, ["sha256sum"] + [posixpath.join(work, f) for f in files], sudo=True)
                mf = "".join(f"SHA256({posixpath.basename(line.split()[1])})= {line.split()[0]}\n"
                             for line in sums.strip().splitlines())
                self.x.write(node, posixpath.join(work, f"{vm}.mf"), mf.encode(), sudo=True)
                final = posixpath.join(work, f"{vm}-{stamp}.ova")
                self.x.run(node, ["tar", "-cf", final, "-C", work, f"{vm}.ovf", f"{vm}.mf"]
                           + [p["file"] for p in produced], sudo=True)
                outputs = [final]
            else:
                outputs = [p["path"] for p in produced]

            names = []
            for f in outputs:
                self.x.run(node, ["chmod", "644", f], sudo=True)
                local = os.path.join(self.cfg["controller"]["exports_dir"], posixpath.basename(f))
                progress(f"Fetching {posixpath.basename(f)} to the controller")
                self.x.pull(node, f, local)
                names.append(posixpath.basename(f))
            return "Exported: " + ", ".join(names)
        finally:
            self.x.run(node, ["rm", "-rf", work], sudo=True, check=False)

    def list_exports(self):
        d = self.cfg["controller"]["exports_dir"]
        out = []
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            out.append({"file": f, "size": os.path.getsize(p), "created": os.path.getmtime(p)})
        return out

    def delete_image(self, name):
        img = self.db.one(select(images).where(images.c.name == name))
        if not img:
            raise ClusterError(f"No image named {name}")
        node = self.st["import_node"]
        for vol in json.loads(img["volumes"] or "[]"):
            if self.rbd:
                pool = self.st["rbd"]["ceph_pool"]
                if self.x.run(node, ["rbd", "children", "--all", f"{pool}/{vol}"], sudo=True, check=False).strip():
                    raise ClusterError(f"VMs were cloned from {name}; delete them (or flatten their disks) first")
                for snap in json.loads(self.x.run(node, ["rbd", "snap", "ls", "--format", "json",
                                                         f"{pool}/{vol}"], sudo=True, check=False) or "[]"):
                    self.x.run(node, ["rbd", "snap", "unprotect", f"{pool}/{vol}@{snap['name']}"], sudo=True, check=False)
                    self.x.run(node, ["rbd", "snap", "rm", f"{pool}/{vol}@{snap['name']}"], sudo=True, check=False)
                self.x.run(node, ["rbd", "rm", f"{pool}/{vol}"], sudo=True, check=False)
            else:
                for n in self._primary_nodes():
                    self.x.run(n, ["rm", "-f", self._target(vol)], sudo=True, check=False)
        self._refresh_pools()
        from .db import delete
        self.db.run(delete(images).where(images.c.name == name))


def staging_path(cfg, filename):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-120:]
    return os.path.join(cfg["controller"]["staging_dir"], f"{int(time.time())}-{safe}")


def remove_tree(p):
    shutil.rmtree(p, ignore_errors=True)
