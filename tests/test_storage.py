"""Disks and RAID, from lsblk to a controller's own CLI.

The hardware-RAID parsers matter most here: no bench has a controller CLI
installed, so without these fixtures the storcli/ssacli/megacli/arcconf paths
would ship untested and could silently return nothing the day a real array
appears. Each one is fed a captured sample and checked for the array it must
find, and the off-PATH lookup is checked too.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import storage

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


# --- disks: HP logical volume, NVMe, an iSCSI target, a loop, an empty reader
LSBLK = """{"blockdevices":[
  {"name":"loop0","type":"loop","size":91267072,"rota":"0","tran":null,"model":null,"serial":null,"vendor":null},
  {"name":"nvme0n1","type":"disk","size":480103981056,"rota":"0","tran":"nvme","model":"Micron_7450","serial":"X1","vendor":null},
  {"name":"sda","type":"disk","size":399431958528,"rota":"1","tran":"sas","model":"LOGICAL VOLUME","serial":"PW1","vendor":"HPE"},
  {"name":"sdc","type":"disk","size":0,"rota":"1","tran":"usb","model":"SD/MMC CRW","serial":null,"vendor":null},
  {"name":"sdd","type":"disk","size":1073741824,"rota":"1","tran":"iscsi","model":"VIRTUAL-DISK","serial":"beaf","vendor":"IET"}
]}"""


def test_disks():
    disks = storage.parse_lsblk_json(LSBLK)
    names = [d["name"] for d in disks]
    check("loop and empty dropped", names, ["nvme0n1", "sda", "sdd"])
    by = {d["name"]: d for d in disks}
    check("nvme media", by["nvme0n1"]["media"], "nvme")
    check("logical media", by["sda"]["media"], "logical")
    check("logical behind_raid", by["sda"]["behind_raid"], True)
    check("iscsi classed network", by["sdd"]["class"], "network")
    check("sas classed local", by["sda"]["class"], "local")
    check("size human", by["sda"]["size"], "372.0 GiB")


def test_controllers():
    lspci = ("00:1f.2 SATA controller: Intel\n"
             "5c:00.0 Serial Attached SCSI controller: Adaptec Smart Storage PQI SAS (rev 01)\n")
    ctrls = storage.detect_controllers(lspci, "ahci smartpqi iscsi_tcp usb-storage")
    check("one controller", len(ctrls), 1)
    check("controller model", "Smart Storage" in ctrls[0]["model"], True)
    check("driver folded in", ctrls[0]["driver"], "smartpqi")
    only = storage.detect_controllers("", "megaraid_sas mpt3sas")
    check("driver-only controller", only[0]["driver"], "megaraid_sas")
    check("plain HBA is not RAID", storage.detect_controllers("", "ahci nvme"), [])


def test_mdstat():
    md = ("Personalities : [raid1]\n"
          "md0 : active raid1 sdb1[1] sda1[0](F)\n"
          "      976630464 blocks super 1.2 [2/1] [U_]\n"
          "      [===>.................]  recovery = 18.5% (1/5) finish=10min\n"
          "unused devices: <none>\n")
    arr = storage.parse_mdstat(md)
    check("one array", len(arr), 1)
    check("level", arr[0]["level"], "raid1")
    check("degraded", "degraded" in arr[0]["state"], True)
    check("faulty member flagged",
          [m["state"] for m in arr[0]["members"] if m["name"] == "sda1"], ["faulty"])
    check("rebuild progress", arr[0]["progress"], "recovery 18.5%")
    check("no array -> empty", storage.parse_mdstat("unused devices: <none>"), [])


def test_storcli():
    sample = ('{"Controllers":[{"Response Data":{"Controller":0,'
              '"VD LIST":[{"DG/VD":"0/0","TYPE":"RAID5","State":"Optl","Size":"3.000 TB","Name":"data"}]}}]}')
    vds = storage.parse_storcli(sample)
    check("one vd", len(vds), 1)
    check("vd level", vds[0]["level"], "raid5")
    check("vd state", vds[0]["state"], "Optl")
    check("vd controller", vds[0]["controller"], "controller 0")


SSACLI = """
Smart Array P440ar in Slot 0 (Embedded)

   Array A (SAS, Unused Space: 0 MB)

      logicaldrive 1 (279.4 GB, RAID 1, OK)

      physicaldrive 1I:1:1 (port 1I:box 1:bay 1, SAS HDD, 300 GB, OK)
      physicaldrive 1I:1:2 (port 1I:box 1:bay 2, SAS HDD, 300 GB, OK)

      logicaldrive 2 (1.09 TB, RAID 5, Recovering)
"""


def test_ssacli():
    arr = storage.parse_ssacli(SSACLI)
    check("two logical drives", len(arr), 2)
    check("ld1 level", arr[0]["level"], "raid1")
    check("ld1 state", arr[0]["state"], "OK")
    check("ld1 controller", arr[0]["controller"], "Smart Array P440ar slot 0")
    check("ld1 two members", len(arr[0]["members"]), 2)
    check("ld2 recovering", arr[1]["state"], "Recovering")


MEGACLI = """Adapter 0 -- Virtual Drive Information:
Virtual Drive: 0 (Target Id: 0)
Name                :
RAID Level          : Primary-5, Secondary-0, RAID Level Qualifier-3
Size                : 2.728 TB
State               : Optimal
Number Of Drives    : 4
"""


def test_megacli():
    arr = storage.parse_megacli(MEGACLI)
    check("megacli one vd", len(arr), 1)
    check("megacli level", arr[0]["level"], "raid5")
    check("megacli state", arr[0]["state"], "Optimal")
    check("megacli size", arr[0]["size"], "2.728 TB")


ARCCONF = """Logical device number 0
   Logical device name                      : Array0
   RAID level                               : 5
   Status of logical device                 : Optimal
   Size                                     : 3815280 MB
"""


def test_arcconf():
    arr = storage.parse_arcconf(ARCCONF)
    check("arcconf one ld", len(arr), 1)
    check("arcconf level", arr[0]["level"], "raid5")
    check("arcconf state", arr[0]["state"], "Optimal")


def test_find_cli_off_path():
    # command -v finds nothing, but the binary is at a known absolute path
    def run(cmd):
        if cmd.startswith("command -v"):
            return ""
        if "test -x /opt/smartstorageadmin/ssacli/bin/ssacli" in cmd:
            return "yes\n"
        return ""
    cli = storage._find_cli(run, ("ssacli", "hpssacli"),
                            ("/usr/sbin/ssacli", "/opt/smartstorageadmin/ssacli/bin/ssacli"))
    check("found off-PATH cli", cli, "/opt/smartstorageadmin/ssacli/bin/ssacli")


NVME_SMART = """SMART overall-health self-assessment test result: PASSED
Critical Warning:                   0x00
Temperature:                        37 Celsius
Available Spare:                    100%
Available Spare Threshold:          10%
Percentage Used:                    45%
Power On Hours:                     962
Media and Data Integrity Errors:    1"""

SATA_SMART = """SMART overall-health self-assessment test result: PASSED
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       8
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       0
177 Wear_Leveling_Count     0x0013   095   095   000    Pre-fail  Always       -       120
194 Temperature_Celsius     0x0022   035   045   000    Old_age   Always       -       35
  9 Power_On_Hours          0x0032   100   100   000    Old_age   Always       -       14523"""

FAILING = """SMART overall-health self-assessment test result: FAILED!
Critical Warning:                   0x04
Percentage Used:                    96%
Available Spare:                    4%
Available Spare Threshold:          10%"""


def test_smart_nvme():
    h = storage.parse_smart(NVME_SMART)
    check("nvme passed", h["passed"], True)
    check("nvme wear", h["percentage_used"], 45)
    check("nvme media errors", h["media_errors"], 1)
    check("nvme spare", h["available_spare"], 100)


def test_smart_sata():
    h = storage.parse_smart(SATA_SMART)
    check("sata reallocated", h["reallocated"], 8)
    check("sata wear from leveling", h["percentage_used"], 5)
    check("sata temp", h["temperature_c"], 35)


def test_smart_problems():
    pr = storage.health_problems([
        {"name": "nvme0n1", "health": storage.parse_smart(NVME_SMART)},
        {"name": "sda", "health": storage.parse_smart(SATA_SMART)},
        {"name": "bad", "health": storage.parse_smart(FAILING)}])
    worst = {}
    for p in pr:
        if p["disk"] not in worst or p["severity"] == "mismatch":
            worst[p["disk"]] = p["severity"]
    details = " | ".join(p["detail"] for p in pr)
    check("failing drive has a mismatch", worst.get("bad"), "mismatch")
    check("nvme media error flagged", "media error" in details, True)
    check("sata reallocated flagged", "reallocated" in details, True)
    check("healthy-but-aging nvme is only a warn", worst.get("nvme0n1"), "warn")


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("storage: all checks passed")
