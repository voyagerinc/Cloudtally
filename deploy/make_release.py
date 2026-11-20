"""Builds a Tally Cloud release package for customers' Windows servers, with a new version number.

    python deploy/make_release.py --bump patch      # 1.0.0 -> 1.0.1 (fixes)
    python deploy/make_release.py --bump minor      # 1.0.1 -> 1.1.0 (new features)
    python deploy/make_release.py                   # package the current version again (not yet uploaded)

Every update needs a higher version: --bump writes it into backend/app/__init__.py, commits that and tags
the commit vX.Y.Z, then packs exactly that commit. The portal refuses a version that is not higher than
the last release, and a package whose VERSION file says otherwise.

The package (dist/TallyCloud-X.Y.Z.zip):
    VERSION                       the version, checked by the portal and by the update script
    run.bat                       double-click: installs the first time, later just starts it
    license_public_key.txt        the portal's public key (with --public-key), else fetched at install
    README.txt
    app/                          the code at tag vX.Y.Z
Upload it on the portal's Releases tab, check it, then Publish."""
import argparse
import io
import re
import subprocess
import sys
import zipfile
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "backend" / "app" / "__init__.py"
PATTERN = re.compile(r'__version__ = "(\d+)\.(\d+)\.(\d+)"')

# Same texts as backend/app/licensing/package.py (the portal's own build); raw strings keep the backslashes.
RUN_BAT = r"""@echo off
rem Tally Cloud. Double-click: the first time it installs Tally Cloud (Windows asks to allow it),
rem later it just starts it and opens the browser. After installing, the "Tally Cloud" icon on the
rem desktop (C:\TallyCloud\start.bat) does the same.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\deploy\windows\run.ps1" %*
"""

README = r"""Tally Cloud {version}

Install and run (Windows server or PC, next to Tally):
  1. Unpack this zip, e.g. to C:\TallyCloud-setup
  2. Double-click run.bat and choose "Yes" when Windows asks to allow it.
     The first time it installs everything (a few minutes) and opens Tally Cloud in the browser:
     enter your licence key and create the manager's user ID and password.
  3. Next time: double-click "Tally Cloud" on the desktop (or C:\TallyCloud\start.bat, or run.bat again).
     Tally Cloud also starts by itself with Windows.
  Other users open  http://<this computer's name>:8080  on the office network.

Updates: the manager opens Licence -> Check for updates -> Install update. Nothing to unpack by hand.
"""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def current_version() -> tuple[int, int, int]:
    m = PATTERN.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"No __version__ = \"X.Y.Z\" in {VERSION_FILE}")
    return tuple(int(x) for x in m.groups())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bump", choices=["patch", "minor", "major"], help="raise the version before packaging")
    p.add_argument("--public-key", help="the portal's public key (curl https://<portal>/api/v1/licensing/public-key)")
    p.add_argument("--out", default=str(ROOT / "dist"))
    args = p.parse_args()

    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("Commit or undo your changes first: a release is built from a commit, not from loose files.")
    major, minor, patch = current_version()
    if args.bump:
        major, minor, patch = {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0),
                               "patch": (major, minor, patch + 1)}[args.bump]
    version = f"{major}.{minor}.{patch}"
    tag = f"v{version}"
    tags = git("tag", "--list", tag)
    if args.bump:
        if tags:
            sys.exit(f"{tag} already exists. Every update needs a new version.")
        text = PATTERN.sub(f'__version__ = "{version}"', VERSION_FILE.read_text(encoding="utf-8"))
        VERSION_FILE.write_text(text, encoding="utf-8", newline="\n")
        git("add", str(VERSION_FILE))
        git("commit", "-m", f"Release {version}")
        git("tag", "-a", tag, "-m", f"Tally Cloud {version}")
    elif not tags:
        git("tag", "-a", tag, "-m", f"Tally Cloud {version}")
    elif git("rev-list", "-n", "1", tag) != git("rev-parse", "HEAD"):
        sys.exit(f"{tag} is an older commit. Use --bump to release your newer code with a new version.")

    code = subprocess.run(["git", "archive", "--format=zip", tag], cwd=ROOT, check=True, capture_output=True).stdout
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"TallyCloud-{version}.zip"
    with zipfile.ZipFile(io.BytesIO(code)) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("VERSION", version + "\n")
        z.writestr("run.bat", RUN_BAT.replace("\n", "\r\n"))
        z.writestr("README.txt", README.format(version=version).replace("\n", "\r\n"))
        if args.public_key:
            z.writestr("license_public_key.txt", args.public_key.strip() + "\n")
        for item in src.infolist():
            if item.is_dir() or item.filename.endswith(".docx"):
                continue
            z.writestr(f"app/{item.filename}", src.read(item))
    digest = sha256(target.read_bytes()).hexdigest()
    print(f"Built {target}  ({target.stat().st_size // 1024} KB)")
    print(f"SHA-256 {digest}")
    print(f"Next: git push origin main {tag}   and upload the zip on the portal's Releases tab as version {version}.")


if __name__ == "__main__":
    main()
