"""Install Blender for the carving styles, from the app (its Install Blender button).

The official portable build from blender.org, checked against blender.org's own
checksums, unpacked where no administrator is needed, then connected:

    Windows  the .zip, into %LOCALAPPDATA%\\NoobPainterSimulation\\blender
    macOS    the .dmg opened, Blender.app copied into ~/Applications
    Linux    the .tar.xz, into ~/.local/share/noobpainter/blender

About 250-400 MB to download and about 1 GB unpacked. Blender is free (GPL); this only
fetches it, as its own download page would.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

VERSION = "4.5.14"                     # an LTS release, built for every platform below
BASE = f"https://download.blender.org/release/Blender{VERSION.rsplit('.', 1)[0]}"
NEED_GB = 2.0                          # the download, and Blender unpacked


def asset() -> str:
    """This computer's Blender download (its file name on blender.org)."""
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if os.name == "nt":
        return f"blender-{VERSION}-windows-{'arm64' if arm else 'x64'}.zip"
    if sys.platform == "darwin":
        return f"blender-{VERSION}-macos-{'arm64' if arm else 'x64'}.dmg"
    if arm:
        raise RuntimeError("blender.org has no Linux build for ARM: install Blender from your "
                           "package manager, then press Look again")
    return f"blender-{VERSION}-linux-x64.tar.xz"


def home() -> Path:
    """Where this installs Blender (the user's own folders: no administrator needed)."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NoobPainterSimulation" / "blender"
    if sys.platform == "darwin":
        return Path.home() / "Applications"
    return Path.home() / ".local" / "share" / "noobpainter" / "blender"


def _checksum(name: str) -> str:
    """blender.org's sha256 for `name`, from the release's checksum list."""
    import downloads
    url = f"{BASE}/blender-{VERSION}.sha256"
    req = urllib.request.Request(url, headers={"User-Agent": "NoobPainterSimulation"})
    with urllib.request.urlopen(req, timeout=60, context=downloads._CTX) as r:
        for line in r.read().decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == name:
                return parts[0].lower()
    raise RuntimeError(f"blender.org lists no checksum for {name}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(say: Callable[[str], None] = print) -> Path:
    """Download, check, unpack and connect Blender. Returns the Blender program."""
    import carving
    import downloads
    name = asset()
    dest = home()
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest).free / 1e9
    if free < NEED_GB:
        raise RuntimeError(f"only {free:.1f} GB free; Blender needs about {NEED_GB:.0f} GB")
    archive = Path(tempfile.gettempdir()) / "noobpainter-downloads" / name
    say(f"Blender {VERSION} for this computer: {name}")
    want = _checksum(name)
    if archive.exists() and _sha256(archive) != want:      # a stale or broken earlier copy
        archive.unlink()
    downloads.fetch(f"{BASE}/{name}", archive, f"Blender {VERSION}", say=say)
    say("Checking the download against blender.org's checksum")
    if _sha256(archive) != want:
        archive.unlink(missing_ok=True)
        raise RuntimeError("the download didn't match blender.org's checksum (deleted; press Install again)")
    say("Unpacking Blender (about 1 GB; a minute or two)")
    if name.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        program = dest / name.removesuffix(".zip") / "blender.exe"
    elif name.endswith(".tar.xz"):
        import tarfile
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(dest)
        program = dest / name.removesuffix(".tar.xz") / "blender"
    else:                                                  # macOS: open the disk image, copy the app
        mount = Path(tempfile.mkdtemp(prefix="blender-dmg-"))
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-quiet", "-mountpoint",
                        str(mount), str(archive)], check=True)
        try:
            app = dest / "Blender.app"
            if app.exists():                               # an older one we put there: replace it
                shutil.rmtree(app)
            subprocess.run(["ditto", str(mount / "Blender.app"), str(app)], check=True)
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], check=False)
        program = dest / "Blender.app" / "Contents" / "MacOS" / "Blender"
    say("Checking that Blender runs")
    version = carving.blender_version(program)
    if not version:
        raise RuntimeError(f"Blender was unpacked but doesn't run ({program})")
    carving.save_blender(str(program))
    archive.unlink(missing_ok=True)                        # the download is no longer needed
    say(f"✓ {version} installed and connected")
    return program
