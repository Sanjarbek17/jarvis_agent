"""Publish a Jarvis App APK so phones update themselves (ticket 50b087e6), like the Taxi App's driver updates.

    /Users/sanjarbek/dev/side_project/projects/jarvis/.venv/bin/python \\
        /Users/sanjarbek/dev/side_project/projects/jarvis/scripts/publish_app.py <app-release.apk> ["what's new"]

Reads versionCode / versionName from the APK itself (aapt), copies it to var/state/app_release/ as
jarvis-app-<name>+<code>.apk and writes latest.json (atomically, the APK first) with its size and sha256.
The app checks GET /api/app/latest on open and every 15 minutes and offers the update. A versionCode not above
the published one is refused: phones only take newer ones anyway. The 3 newest APKs are kept.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402  (tests: a temporary folder, never the real one)

RELEASES = VAR / "state" / "app_release"
SDK = Path.home() / "Library" / "Android" / "sdk" / "build-tools"
KEEP = 3


def apk_version(apk: Path) -> tuple[int, str, str]:
    """(versionCode, versionName, package) from the APK's manifest."""
    tools = sorted(SDK.glob("*/aapt"))
    if not tools:
        sys.exit("aapt not found in the Android SDK build-tools")
    out = subprocess.run([str(tools[-1]), "dump", "badging", str(apk)], capture_output=True, text=True, check=True).stdout
    found = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", out)
    if not found:
        sys.exit("Couldn't read the APK's version")
    return int(found[2]), found[3], found[1]


def publish(apk: Path, notes: str = "") -> dict:
    code, name, package = apk_version(apk)
    if package != "com.sanjar.jarvis":
        sys.exit(f"That's not the Jarvis app ({package})")
    if not re.fullmatch(r"[0-9][0-9A-Za-z.]*", name):
        sys.exit(f"Odd versionName {name!r}")
    RELEASES.mkdir(parents=True, exist_ok=True)
    manifest = RELEASES / "latest.json"
    if manifest.exists():
        old = json.loads(manifest.read_text()).get("version_code", 0)
        if code <= old:
            sys.exit(f"versionCode {code} isn't above the published {old}: bump versionCode in app/build.gradle.kts")
    file = f"jarvis-app-{name}+{code}.apk"
    shutil.copyfile(apk, RELEASES / (file + ".tmp"))
    os.replace(RELEASES / (file + ".tmp"), RELEASES / file)
    data = (RELEASES / file).read_bytes()
    release = {"version_code": code, "version_name": name, "file": file, "sha256": hashlib.sha256(data).hexdigest(),
               "size_bytes": len(data), "notes": notes.strip()[:500]}
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(release, indent=1))
    os.replace(tmp, manifest)
    for old_apk in sorted(RELEASES.glob("jarvis-app-*.apk"), key=lambda p: p.stat().st_mtime)[:-KEEP]:
        old_apk.unlink(missing_ok=True)
    return release


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    r = publish(Path(sys.argv[1]).expanduser(), " ".join(sys.argv[2:]))
    print(f"Published Jarvis App {r['version_name']} ({r['version_code']}), {r['size_bytes'] // 1024} KB: "
          "phones offer the update on their next check.")
