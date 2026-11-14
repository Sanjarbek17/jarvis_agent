#!/usr/bin/env python
"""Nightly copy of the boards on the Mac to the private repo Sanjarbek17/jarvis-boards, as readable JSON.

    .venv/bin/python scripts/backup_boards.py                          # export, commit, push
    .venv/bin/python scripts/backup_boards.py --restore <folder> <db>  # rebuild a new boards.db from a clone

Runs from launchd (~/Library/LaunchAgents/com.sanjar.jarvis-board-backup.plist). Before anything leaves the
Mac, every password and key value from the .env files under ~/dev, and anything shaped like one, becomes
[hidden], so those values aren't restored. Git history keeps the older nights. If a night fails, the error goes to Sanjar on Telegram.
"""
import argparse
import contextlib
import functools
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402  (tests: a temporary folder, never the real one)

sys.path.insert(0, str(HERE))
from core import localboard  # noqa: E402

REMOTE = "git@github.com:Sanjarbek17/jarvis-boards.git"
CLONE = VAR / "boards-backup"
FAILED = VAR / "logs" / "board-backup-failed.txt"
HIDDEN = "[hidden]"
ENV_ROOT = Path.home() / "dev"
SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "build", "dist", "__pycache__", ".worktrees", ".gradle"}
TEMPLATES = (".example", ".sample", ".template")
SECRET_KEY = re.compile(r"(?i)pass|secret|token|api_?key|private")
PLAIN = {"postgres", "password", "changeme", "localhost"}
SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-ant-[\w-]{10,}|\bsk-[A-Za-z0-9]{20,}"
    r"|\b(?:secret_|ntn_)[A-Za-z0-9]{20,}|\b\d{8,10}:[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16}|AIza[\w-]{30,}"
    r"|eyJ[\w-]{15,}\.eyJ[\w-]{15,}\.[\w-]*"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
NAMED = re.compile(r"(?i)\b(password|passwd|parol|secret|api[_ -]?key|token)(\s*[:=]\s*)(?!\[hidden\])[^\s`*\"'<>]+")
URL_LOGIN = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+@")


@functools.lru_cache(maxsize=1)
def secret_values() -> tuple[str, ...]:
    """The real passwords and keys from every .env under ~/dev: tickets quote them, and shapes can't catch those."""
    found = set()
    for folder, dirs, files in os.walk(ENV_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name == ".env" or (name.startswith(".env.") and not name.endswith(TEMPLATES)):
                with contextlib.suppress(OSError):
                    for key, value in dotenv_values(Path(folder) / name).items():
                        if SECRET_KEY.search(key) and value and len(value) >= 6 and value.lower() not in PLAIN:
                            found.add(value)
    if not found:
        raise RuntimeError(f"found no secrets to hide under {ENV_ROOT}, not sending anything")
    return tuple(sorted(found, key=len, reverse=True))


def mask(text: str) -> str:
    text = text or ""
    for value in secret_values():
        text = text.replace(value, HIDDEN)
    text = SHAPES.sub(HIDDEN, text)
    text = URL_LOGIN.sub(rf"\g<1>{HIDDEN}@", text)
    return NAMED.sub(rf"\g<1>\g<2>{HIDDEN}", text)


def slug(project: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-") or "project"


def snapshot(db: Path) -> dict[str, list[dict]]:
    """Every ticket with its log, by project, from one consistent read; refuses an empty board file."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    with con:
        con.execute("BEGIN")
        logs: dict[str, list[dict]] = {}
        for row in con.execute("SELECT ref, at, line FROM log ORDER BY id"):
            logs.setdefault(row["ref"], []).append({"at": row["at"], "line": mask(row["line"])})
        out: dict[str, list[dict]] = {}
        for row in con.execute("SELECT * FROM tickets ORDER BY project, created, ref"):
            t = dict(row)
            t.update(title=mask(t["title"]), body=mask(t["body"]), note=mask(t["note"]), log=logs.get(t["ref"], []))
            out.setdefault(t["project"], []).append(t)
    con.close()
    if not out:
        raise RuntimeError(f"no tickets in {db}, not touching the backup")
    return out


def export(db: Path, folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    data = snapshot(db)
    wanted = {f"{slug(p)}.json": tickets for p, tickets in data.items()}
    for old in folder.glob("*.json"):
        if old.name not in wanted:
            old.unlink()
    for name, tickets in wanted.items():
        (folder / name).write_text(json.dumps(tickets, indent=1, ensure_ascii=False) + "\n")
    return sum(len(t) for t in data.values())


def restore(folder: Path, db: Path) -> int:
    """A new boards.db from the exported files. Never writes over an existing one."""
    if db.exists():
        raise SystemExit(f"{db} already exists")
    con = sqlite3.connect(db)
    con.executescript(localboard.SCHEMA)
    count = 0
    with con:
        for f in sorted(folder.glob("*.json")):
            for t in json.loads(f.read_text()):
                log = t.pop("log")
                keys = list(t)
                con.execute(f"INSERT INTO tickets ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", list(t.values()))
                con.executemany("INSERT INTO log (ref, at, line) VALUES (?, ?, ?)",
                                [(t["ref"], e["at"], e["line"]) for e in log])
                count += 1
    con.close()
    return count


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(CLONE), *args], check=True, capture_output=True, text=True)


def backup() -> str:
    if not (CLONE / ".git").exists():
        subprocess.run(["git", "clone", "-q", REMOTE, str(CLONE)], check=True, capture_output=True, text=True)
    count = export(localboard.DB, CLONE / "boards")
    git("add", "-A")
    if subprocess.run(["git", "-C", str(CLONE), "diff", "--cached", "--quiet"]).returncode == 0:
        git("push", "-q", "origin", "HEAD")      # a push that failed last night goes out now
        return f"{count} tickets, nothing changed"
    git("commit", "-q", "-m", f"Boards backup: {count} tickets")
    git("push", "-q", "origin", "HEAD")
    return f"{count} tickets, pushed"


def tell_sanjar(error: str) -> None:
    FAILED.parent.mkdir(parents=True, exist_ok=True)
    FAILED.write_text(mask(error)[-3000:] + "\n")
    subprocess.run([sys.executable, str(HERE / "scripts" / "send_file.py"), str(FAILED), "The nightly board backup failed"],
                   timeout=120, check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--restore", nargs=2, metavar=("FOLDER", "DB"))
    a = ap.parse_args()
    if a.restore:
        print(f"{restore(Path(a.restore[0]), Path(a.restore[1]))} tickets restored")
        return 0
    try:
        print(backup(), flush=True)
        return 0
    except Exception as e:
        detail = getattr(e, "stderr", None) or str(e)
        print(f"FAILED: {mask(detail)}", flush=True)
        tell_sanjar(detail)
        return 1


if __name__ == "__main__":
    sys.exit(main())
