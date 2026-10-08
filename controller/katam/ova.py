"""OVF descriptors: read them from imported OVAs, write VMware-compatible ones on export."""
import re
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

NS = {
    "ovf": "http://schemas.dmtf.org/ovf/envelope/1",
    "rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData",
    "vssd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData",
    "vmw": "http://www.vmware.com/schema/ovf",
}
O = "{%s}" % NS["ovf"]


def _units(unit_str):
    """'byte * 2^20' -> 1048576. Defaults to bytes."""
    if not unit_str:
        return 1
    u = unit_str.replace(" ", "").lower()
    m = re.match(r"byte\*2\^(\d+)", u)
    if m:
        return 2 ** int(m.group(1))
    return {"kilobytes": 2**10, "megabytes": 2**20, "gigabytes": 2**30, "byte": 1}.get(u, 1)


def parse_ovf(text):
    root = ET.fromstring(text)
    files = {f.get(O + "id"): f.get(O + "href") for f in root.iterfind("ovf:References/ovf:File", NS)}
    disks = {}
    for d in root.iterfind(".//ovf:DiskSection/ovf:Disk", NS):
        cap = int(d.get(O + "capacity", "0")) * _units(d.get(O + "capacityAllocationUnits"))
        disks[d.get(O + "diskId")] = {"file": files.get(d.get(O + "fileRef")), "capacity": cap}

    vs = root.find(".//ovf:VirtualSystem", NS)
    if vs is None:
        raise ValueError("No VirtualSystem in OVF")
    name = vs.get(O + "id") or "imported"
    nm = vs.find("ovf:Name", NS)
    if nm is not None and nm.text:
        name = nm.text

    os_el = vs.find("ovf:OperatingSystemSection", NS)
    os_hint = ""
    if os_el is not None:
        os_hint = (os_el.get("{%s}osType" % NS["vmw"]) or "") + " " + (os_el.findtext("ovf:Description", "", NS) or "")
    os_type = "windows" if "win" in os_hint.lower() else "linux"

    vcpus, memory_mb, nics, ordered = 1, 1024, 0, []
    firmware = "bios"
    for item in vs.iterfind(".//ovf:VirtualHardwareSection/ovf:Item", NS):
        rt = item.findtext("rasd:ResourceType", "", NS)
        if rt == "3":
            vcpus = int(item.findtext("rasd:VirtualQuantity", "1", NS))
        elif rt == "4":
            qty = int(item.findtext("rasd:VirtualQuantity", "1024", NS))
            memory_mb = qty * _units(item.findtext("rasd:AllocationUnits", "byte * 2^20", NS)) // 2**20
        elif rt == "10":
            nics += 1
        elif rt == "17":
            ref = item.findtext("rasd:HostResource", "", NS)
            disk_id = ref.rsplit("/", 1)[-1]
            if disk_id in disks:
                ordered.append(disks[disk_id])
    for cfg in vs.iterfind(".//{%s}Config" % NS["vmw"]):
        if cfg.get("{%s}key" % NS["vmw"]) == "firmware" and cfg.get("{%s}value" % NS["vmw"]) == "efi":
            firmware = "uefi"
    if not ordered:
        ordered = list(disks.values())
    return {"name": name, "vcpus": vcpus, "memory_mb": memory_mb, "nics": max(nics, 1),
            "disks": [d for d in ordered if d["file"]], "os_type": os_type, "firmware": firmware}


def build_ovf(name, vcpus, memory_mb, disks, os_type="linux", firmware="bios", nics=1):
    """disks: [{'file': 'x-disk0.vmdk', 'size': bytes_on_disk, 'capacity': virtual_bytes}]"""
    windows = os_type == "windows"
    os_id, os_vmw = ("1", "windows9Server64Guest") if windows else ("101", "other4xLinux64Guest")
    scsi = "lsilogicsas" if windows else "lsilogic"
    nic_type = "E1000e" if windows else "VmxNet3"
    refs, disk_items, hw = [], [], []
    for i, d in enumerate(disks):
        refs.append(f'<File ovf:id="file{i}" ovf:href="{escape(d["file"])}" ovf:size="{d["size"]}"/>')
        disk_items.append(
            f'<Disk ovf:capacity="{d["capacity"]}" ovf:capacityAllocationUnits="byte" ovf:diskId="vmdisk{i}" '
            f'ovf:fileRef="file{i}" ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>')
    iid = 1

    def item(body):
        nonlocal iid
        s = f"<Item><rasd:InstanceID>{iid}</rasd:InstanceID>{body}</Item>"
        iid += 1
        return s

    hw.append(item(f"<rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits><rasd:ElementName>{vcpus} vCPU</rasd:ElementName>"
                   f"<rasd:ResourceType>3</rasd:ResourceType><rasd:VirtualQuantity>{vcpus}</rasd:VirtualQuantity>"))
    hw.append(item(f"<rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits><rasd:ElementName>{memory_mb} MB memory</rasd:ElementName>"
                   f"<rasd:ResourceType>4</rasd:ResourceType><rasd:VirtualQuantity>{memory_mb}</rasd:VirtualQuantity>"))
    scsi_id = iid
    hw.append(item(f"<rasd:Address>0</rasd:Address><rasd:ElementName>SCSI controller 0</rasd:ElementName>"
                   f"<rasd:ResourceSubType>{scsi}</rasd:ResourceSubType><rasd:ResourceType>6</rasd:ResourceType>"))
    for i, _ in enumerate(disks):
        hw.append(item(f"<rasd:AddressOnParent>{i}</rasd:AddressOnParent><rasd:ElementName>Hard disk {i + 1}</rasd:ElementName>"
                       f"<rasd:HostResource>ovf:/disk/vmdisk{i}</rasd:HostResource><rasd:Parent>{scsi_id}</rasd:Parent>"
                       f"<rasd:ResourceType>17</rasd:ResourceType>"))
    for n in range(nics):
        hw.append(item(f"<rasd:AddressOnParent>{n + 7}</rasd:AddressOnParent><rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>"
                       f"<rasd:Connection>VM Network</rasd:Connection><rasd:ElementName>Network adapter {n + 1}</rasd:ElementName>"
                       f"<rasd:ResourceSubType>{nic_type}</rasd:ResourceSubType><rasd:ResourceType>10</rasd:ResourceType>"))
    fw = '<vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="efi"/>' if firmware == "uefi" else ""
    n = escape(name)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:rasd="{NS['rasd']}" xmlns:vssd="{NS['vssd']}" xmlns:vmw="{NS['vmw']}"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <References>{"".join(refs)}</References>
  <DiskSection><Info>Virtual disks</Info>{"".join(disk_items)}</DiskSection>
  <NetworkSection><Info>Networks</Info><Network ovf:name="VM Network"><Description>VM network</Description></Network></NetworkSection>
  <VirtualSystem ovf:id="{n}">
    <Info>Exported from Katam Virtualization</Info>
    <Name>{n}</Name>
    <OperatingSystemSection ovf:id="{os_id}" vmw:osType="{os_vmw}"><Info>Guest OS</Info></OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware</Info>
      <System><vssd:ElementName>Virtual Hardware Family</vssd:ElementName><vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>{n}</vssd:VirtualSystemIdentifier><vssd:VirtualSystemType>vmx-14</vssd:VirtualSystemType></System>
      {"".join(hw)}
      {fw}
    </VirtualHardwareSection>
    <AnnotationSection><Info>Notes</Info><Annotation>Katam export {uuid.uuid4()}</Annotation></AnnotationSection>
  </VirtualSystem>
</Envelope>
'''
