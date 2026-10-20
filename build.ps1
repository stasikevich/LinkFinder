# LinkFinder build script - written by Stas Mitin.
# Builds both front ends into dist\ with PyInstaller.
# NOTE: PyInstaller logs to stderr, which PowerShell 5.1 turns into a terminating
# error when ErrorActionPreference is Stop, so native calls run under Continue and
# are checked through $LASTEXITCODE instead.
param([ValidateSet("open", "pw")] [string]$Brand = "open")
Set-Location $PSScriptRoot
$ErrorActionPreference = "Continue"

# PyInstaller cannot overwrite a binary that is still running, and the failure
# it reports is a bare exit code 1 with nothing to say why. Stop them by name:
# matching on a window title misses a process whose window has not appeared.
foreach ($name in @("LinkFinder-GUI", "linkfinder",
                    "LinkFinder-PW-GUI", "linkfinder-pw")) {
    Get-Process -Name $name -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 400

function Invoke-Step($label, [scriptblock]$body) {
    Write-Host "== $label"
    & $body
    if ($LASTEXITCODE -ne 0) { throw "$label failed with exit code $LASTEXITCODE" }
}

# The two builds are named apart on purpose: Windows file names are
# case-insensitive, so two builds sharing one name leave a single file and no
# warning. The branded one needs branding\ - absent, that is a failure, not a
# silent fall back to the open build under a company name.
$cliName = "linkfinder"
$guiName = "LinkFinder-GUI"
$brandArgs = @()
if ($Brand -eq "pw") {
    $icon = Join-Path $PSScriptRoot "branding\icon.ico"
    $meta = Join-Path $PSScriptRoot "branding\brand.json"
    if (-not (Test-Path $icon)) {
        throw "branding\icon.ico is missing - see branding\README.txt"
    }
    if (-not (Test-Path $meta)) {
        throw "branding\brand.json is missing - see branding\README.txt"
    }
    $cliName = "linkfinder-pw"
    $guiName = "LinkFinder-PW-GUI"
    $brandArgs = @("--icon", $icon, "--add-data", "branding/brand.json;.",
                   "--add-data", "branding/icon.ico;.")
    # Default lab logins travel with the internal build only, exactly as the
    # brand does. The open build never bundles this, and a gate proves it.
    $logins = Join-Path $PSScriptRoot "branding\logins.json"
    if (Test-Path $logins) {
        $brandArgs += @("--add-data", "branding/logins.json;.")
    }
    # Radio NETCONF logins and the (site-specific) log-file rules travel with
    # the internal build only, exactly like the host logins. Absent is fine -
    # the radio serial just goes unread.
    foreach ($extra in @("radio-logins.json", "radio-sources.json")) {
        if (Test-Path (Join-Path $PSScriptRoot "branding\$extra")) {
            $brandArgs += @("--add-data", "branding/$extra;.")
        }
    }
    Write-Host "== branded build: $cliName / $guiName"
} else {
    Write-Host "== open build: $cliName / $guiName"
}

# The cheapest check there is, so it goes first: a name nothing defines is
# a crash in whichever branch holds it, and the tests cannot reach a branch
# behind an option nobody turned on. One such name had been sitting in the
# switch-port lookup, swallowed by a bare except, for as long as it existed.
Invoke-Step "no dead names" { python tools\no_dead_names.py | Select-Object -Last 1 }

Invoke-Step "no site refs" { python tools\no_site_refs.py | Select-Object -Last 1 }

# Before anything is bundled. PyInstaller only WARNS about a hidden import it
# cannot find, so a build host without paramiko produced binaries that started,
# drew every menu, and could not open one SSH session.
Invoke-Step "the libraries it needs are here" {
    python tools\libraries_present.py | Select-Object -Last 1
}

# Settings, the NetBox token, remembered switches and setups belong beside
# the person, never inside the product. One stray file next to the sources
# is all it takes, so it is checked before anything is bundled.
Invoke-Step "no settings" { python tools\no_settings.py | Select-Object -Last 1 }
# The guide is built before the binary, so the page inside it describes
# this build and not the last one somebody remembered to regenerate.
Invoke-Step "help page" { python tools\make_help.py | Select-Object -Last 1 }
# Every suite in tests/, found rather than listed. A hand-kept list means a
# new suite is silently skipped on the day it is written, which is the day it
# matters most.
Get-ChildItem tests\test_*.py | Sort-Object Name | ForEach-Object {
    $suite = $_.FullName
    $short = $_.BaseName -replace '^test_', ''
    Invoke-Step "tests: $short" { python $suite | Select-Object -Last 1 }
}

$assets = "linkfinder/assets;linkfinder/assets"
# The libraries are imported lazily, so PyInstaller cannot see them. The list
# is read out of the source rather than typed here: the typed one named
# paramiko and requests, and had never heard of urllib3.
$common = @("--noconfirm", "--onefile", "--clean", "--log-level", "WARN",
            "--add-data", $assets)
foreach ($library in (python tools\libraries_present.py --names)) {
    $common += @("--hidden-import", $library)
}

Invoke-Step "build $cliName.exe (CLI)" {
    python -m PyInstaller @common @brandArgs --console --name $cliName cli_entry.py
}
Invoke-Step "build $guiName.exe (GUI)" {
    python -m PyInstaller @common @brandArgs --windowed --name $guiName gui_entry.py
}

# The two front ends are built a minute apart by the same command, so a
# mismatched icon ships unnoticed and costs an afternoon to chase. Checked
# against branding\icon.ico as well, so this also proves the icon on screen
# is the one on disk rather than whatever the shell cached.
if ($Brand -eq "pw") {
    Invoke-Step "same icon" {
        python tools\same_icon.py "dist\$cliName.exe" "dist\$guiName.exe" $icon
    }
}

# The Linux binary is built elsewhere and copied into dist\ by hand; the
# packager takes whatever is there and says plainly what was missing.
Invoke-Step "package" { python tools\package.py $Brand }

# Again on the finished article: the first run proved the sources were
# clean, this one proves what is about to leave the building is.
Invoke-Step "nothing personal shipped" {
    python tools\no_settings.py | Select-Object -Last 1
}

# The tests all run from source, where every import works. The bundler
# reads the source for import statements, so a module reached by a name
# built at run time is left out and the feature silently does nothing in
# the product. Three link builders shipped missing that way.
# Named rather than "everything in dist": the other brand's binaries are
# from an earlier run and have never heard of a module added since, which
# is worth knowing before shipping and is no reason to stop this build.
# Run it bare - python tools\all_modules_shipped.py - to judge the lot.
Invoke-Step "the whole program is in the binary" {
    python tools\all_modules_shipped.py "dist\$cliName.exe" "dist\$guiName.exe" |
        Select-Object -Last 1
}

# Knowing what is inside it is not the same as knowing it works. Nothing
# had ever started the built file until now - it was inspected, packaged
# and shipped. This makes it merge two maps and compare them, which needs
# no network and no lab, so there is no excuse for not doing it.
Invoke-Step "the binary starts and does its work" {
    python tools\binary_runs.py | Select-Object -Last 1
}

Write-Host ""
# Plain lines, not a formatted table: piping this script's output anywhere
# collides with PowerShell's formatter and fails the whole run with a message
# about FormatEntryData that says nothing about the build.
Get-ChildItem dist\*.exe | ForEach-Object {
    "{0,-24} {1,6:N1} MB  {2}" -f $_.Name, ($_.Length / 1MB), $_.LastWriteTime
}
