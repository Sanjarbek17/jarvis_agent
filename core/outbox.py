"""A local outbox: scripts send Sanjar messages and files through the running Jarvis.

Why: a script that opens a *copy* of Jarvis's Telegram session uses the same auth key, and
Telegram then delivers new messages to that copy instead of to Jarvis. While the script runs,
Jarvis is deaf (seen during the QR re-login: no replies until the script was stopped).
So scripts drop a JSON note in var/outbox/; Jarvis sends it within a second with its own
connection and writes a `.done` note back. Only when Jarvis isn't running may a script connect
directly (nothing to collide with then).

This module has no Jarvis imports, so scripts can use it cheaply.
"""
import asyncio
import fcntl
import json
import logging
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent.parent
VAR = HERE / "var"
OUTBOX = VAR / "outbox"
LOCK = VAR / "jarvis.lock"            # held by the running Jarvis (single-instance lock)
SECRET_2FA = VAR / "state" / "tg_2fa_pending"   # /2fa <password> lands here for a waiting login
WANT_2FA = VAR / "state" / "tg_2fa_wanted"      # a login is waiting; /2fa is refused without it

log = logging.getLogger("jarvis.outbox")


def jarvis_running() -> bool:
    """True if a Jarvis process holds the single-instance lock."""
    if not LOCK.exists():
        return False
    with open(LOCK) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def send(text: str = "", file: str | Path = "", caption: str = "", key: str = "",
         delete_key: str = "", document: bool = False, whoami: bool = False, wait: float = 120) -> dict:
    """Queue a message for Jarvis to send to Sanjar and wait for the result.
    key: name this message; a later message with the same key replaces it (old one deleted).
    delete_key: just delete the message saved under that key.
    whoami: also return the Jarvis account as "me": {"id", "username"}. Returns {"ok": bool, ...}."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    name = f"{time.time():.6f}-{uuid.uuid4().hex[:6]}"
    note = {"text": text, "file": str(Path(file).resolve()) if file else "", "caption": caption,
            "key": key, "delete_key": delete_key, "document": document, "whoami": whoami}
    tmp = OUTBOX / f"{name}.tmp"
    tmp.write_text(json.dumps(note, ensure_ascii=False))
    tmp.rename(OUTBOX / f"{name}.json")            # appears complete, never half-written
    done = OUTBOX / f"{name}.done"
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if done.exists():
            result = json.loads(done.read_text())
            done.unlink(missing_ok=True)
            return result
        time.sleep(0.3)
    return {"ok": False, "error": "Jarvis didn't pick it up in time"}


async def run(tg, owner_id: int) -> None:
    """Inside Jarvis: send whatever scripts queued, to Sanjar only."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    keyed: dict[str, object] = {}                    # key -> sent message
    while True:
        for f in sorted(OUTBOX.glob("*.json")):
            result: dict = {"ok": True}
            try:
                note = json.loads(f.read_text())
                if note.get("whoami"):
                    me = await tg.get_me()
                    result["me"] = {"id": me.id, "username": me.username}
                for k in (note.get("delete_key"), note.get("key")):
                    if k and k in keyed:             # replacing or deleting a keyed message
                        old = keyed.pop(k)
                        try:
                            await old.delete()
                        except Exception:
                            pass
                if note.get("file"):
                    msg = await tg.send_file(owner_id, note["file"], caption=(note.get("caption") or "")[:1000],
                                             force_document=bool(note.get("document")))
                elif note.get("text"):
                    msg = await tg.send_message(owner_id, note["text"][:4000], link_preview=False)
                else:
                    msg = None
                if msg is not None and note.get("key"):
                    keyed[note["key"]] = msg
                if msg is not None:
                    result["message_id"] = msg.id
            except Exception as e:
                log.warning("outbox note %s failed: %r", f.name, e)
                result = {"ok": False, "error": repr(e)[:300]}
            f.with_suffix(".done").write_text(json.dumps(result))
            f.unlink(missing_ok=True)
        await asyncio.sleep(0.5)
