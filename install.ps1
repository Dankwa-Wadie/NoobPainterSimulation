# NoobPainterSimulation: install everything on Windows with one line in PowerShell.
#
#   irm https://raw.githubusercontent.com/Dankwa-Wadie/NoobPainterSimulation/main/install.ps1 | iex
#
# It installs what's missing with winget (Git, Python 3.12, ffmpeg, Rust and the C++
# build tools Rust needs), downloads this project into %USERPROFILE%\NoobPainterSimulation,
# sets up its Python environment, builds the paint engine and fetches the music.
# Windows may ask for permission (a UAC prompt) while the build tools install.
#
# Options (set before running):  $env:NOOBPAINTER_DIR = "D:\somewhere"
& {
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Dir  = if ($env:NOOBPAINTER_DIR) { $env:NOOBPAINTER_DIR } else { Join-Path $env:USERPROFILE "NoobPainterSimulation" }
$Repo = "https://github.com/Dankwa-Wadie/NoobPainterSimulation"
$Commit = "059b3a211cd5a916ac27128cec640f1905b02d80"     # claude-paint, as in setup.sh
$Arm = $env:PROCESSOR_ARCHITECTURE -eq "ARM64"

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Run {      # a program and its arguments; stop if it fails
    $exe, $rest = $args
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $($args -join ' ')" }
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                (Join-Path $env:USERPROFILE ".cargo\bin")
}
function Winget-Install($id, [string[]] $extra = @()) {
    Write-Host "  installing $id"
    winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements --silent @extra
    # -1978335189: already installed; anything else non-zero is a real failure
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) { throw "winget could not install $id ($LASTEXITCODE)" }
}

if (-not (Have winget)) {
    throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again."
}

Say "Checking the tools it needs"
Refresh-Path
if (-not (Have git)) { Winget-Install Git.Git }
if (-not (Have ffmpeg)) { Winget-Install Gyan.FFmpeg }

# Python 3.12, x64 even on ARM: not every package the painter uses has ARM wheels for
# Windows yet, and Windows 11 runs x64 programs on ARM by itself.
$Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) {
    Winget-Install Python.Python.3.12 @("--architecture", "x64", "--scope", "user")
}
if (-not (Test-Path $Python)) { throw "Python 3.12 did not install where expected: $Python" }

# Rust builds with Microsoft's C++ linker: the Visual Studio Build Tools.
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$hasVC = (Test-Path $vswhere) -and (& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
if ($Arm) { $hasVC = (Test-Path $vswhere) -and (& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.ARM64 -property installationPath) }
if (-not $hasVC) {
    Say "Installing the C++ build tools (a few GB; Windows may ask for permission)"
    $add = "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
    if ($Arm) { $add += " --add Microsoft.VisualStudio.Component.VC.Tools.ARM64" }
    Winget-Install Microsoft.VisualStudio.2022.BuildTools @("--override", "--quiet --wait --norestart $add")
}

Refresh-Path
if (-not (Have cargo)) {
    Say "Installing Rust (to build the paint engine)"
    $rustup = Join-Path $env:TEMP "rustup-init.exe"
    $arch = if ($Arm) { "aarch64" } else { "x86_64" }
    Invoke-WebRequest "https://static.rust-lang.org/rustup/dist/$arch-pc-windows-msvc/rustup-init.exe" -OutFile $rustup
    Run $rustup -y --profile minimal
    Refresh-Path
}
Refresh-Path

Say "Downloading NoobPainterSimulation into $Dir"
if (Test-Path (Join-Path $Dir ".git")) { Run git -C $Dir pull --ff-only }
else { Run git clone $Repo $Dir }
Set-Location $Dir

Say "Setting up Python (a private environment in $Dir\.venv)"
$Py = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Run $Python -m venv .venv }
Run $Py -m pip install -q --upgrade pip
Run $Py -m pip install -q -r requirements.txt

Say "Building the paint engine (several minutes, the first time)"
$Vendor = Join-Path $Dir "vendor\claude-paint"
if (-not (Test-Path $Vendor)) {
    New-Item -ItemType Directory -Force (Join-Path $Dir "vendor") | Out-Null
    # only the code: the full repo carries its gallery of paintings (hundreds of MB)
    Run git clone --filter=blob:none --no-checkout https://github.com/aliceisjustplaying/claude-paint $Vendor
    Run git -C $Vendor sparse-checkout init --no-cone
    Set-Content -Encoding ascii (Join-Path $Vendor ".git\info\sparse-checkout") "/*`n!/*/`n/crates/easel/`n/crates/paint/`n/.cargo/`n"
    Run git -C $Vendor checkout -q $Commit
}
if (-not (Select-String -Quiet -Pattern hand_secs (Join-Path $Vendor "crates\easel\src\api.rs"))) {
    Run git -C $Vendor apply (Join-Path $Dir "patches\claude-paint-hand-secs.patch")
}
# Windows has no Unix sockets: build without the live session server (`easel run` is all this uses)
if (-not (Test-Path (Join-Path $Vendor "crates\easel\src\uds.rs"))) {
    Run git -C $Vendor apply (Join-Path $Dir "patches\claude-paint-windows.patch")
}
Push-Location $Vendor
try { Run cargo build --release -p easel --features all-boxes } finally { Pop-Location }

Say "Fetching the music (free public-domain recordings)"
& $Py tools\fetch_classical.py
if ($LASTEXITCODE -ne 0) { Write-Host "  (music skipped; videos will be silent until you run tools\fetch_classical.py)" }

Write-Host "`nNot on Windows yet: the one-click local image model (paint from a typed idea)."
Write-Host "Painting from your own pictures works. Optional: Blender for the carving styles: https://www.blender.org/download/"

Say "All set. To open the app:"
Write-Host "    cd `"$Dir`"; .venv\Scripts\python -m noobpainter ui`n"
$go = Read-Host "Open it now? [Y/n]"
if ($go -notmatch '^[nN]') { & $Py -m noobpainter ui }
}
