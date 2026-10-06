# LinkFinder build script - written by Stas Mitin.
# Builds both front ends into dist\ with PyInstaller.
# NOTE: PyInstaller logs to stderr, which PowerShell 5.1 turns into a terminating
# error when ErrorActionPreference is Stop, so native calls run under Continue and
# are checked through $LASTEXITCODE instead.
Set-Location $PSScriptRoot
$ErrorActionPreference = "Continue"

function Invoke-Step($label, [scriptblock]$body) {
    Write-Host "== $label"
    & $body
    if ($LASTEXITCODE -ne 0) { throw "$label failed with exit code $LASTEXITCODE" }
}

Invoke-Step "tests: decode"  { python tests\test_decode.py  | Select-Object -Last 1 }
Invoke-Step "tests: snmp"    { python tests\test_snmp.py    | Select-Object -Last 1 }
Invoke-Step "tests: parsers" { python tests\test_parsers.py | Select-Object -Last 1 }
Invoke-Step "tests: store"   { python tests\test_store.py   | Select-Object -Last 1 }
Invoke-Step "tests: switch"  { python tests\test_switchcli.py | Select-Object -Last 1 }
Invoke-Step "tests: topology" { python tests\test_topology.py | Select-Object -Last 1 }
Invoke-Step "tests: crawl"   { python tests\test_crawl.py     | Select-Object -Last 1 }
Invoke-Step "tests: render"  { python tests\test_render.py    | Select-Object -Last 1 }
Invoke-Step "tests: health"  { python tests\test_port_health.py | Select-Object -Last 1 }
Invoke-Step "tests: ptp"     { python tests\test_ptp.py       | Select-Object -Last 1 }
Invoke-Step "tests: netaddr" { python tests\test_netaddr.py   | Select-Object -Last 1 }
Invoke-Step "tests: links"   { python tests\test_linkcheck.py  | Select-Object -Last 1 }
Invoke-Step "tests: diff"    { python tests\test_diff.py       | Select-Object -Last 1 }

$assets = "linkfinder/assets;linkfinder/assets"
$common = @("--noconfirm", "--onefile", "--clean", "--log-level", "WARN",
            "--hidden-import", "paramiko", "--hidden-import", "requests",
            "--add-data", $assets)

Invoke-Step "build linkfinder.exe (CLI)" {
    python -m PyInstaller @common --console --name linkfinder cli_entry.py
}
Invoke-Step "build LinkFinder-GUI.exe (GUI)" {
    python -m PyInstaller @common --windowed --name LinkFinder-GUI gui_entry.py
}

Write-Host ""
Get-ChildItem dist\*.exe | Format-Table Name, @{N="MB";E={[math]::Round($_.Length/1MB,1)}}, LastWriteTime
