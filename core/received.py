"""What other people send to Jarvis's own Telegram account (ticket 8d867330).

People write to Jarvis's account (@Jarvis_Sanjar) too: Zilola sent her timer as index.html there. Jarvis only answers
Sanjar (and Flatmates replies), and used to drop everything else without a trace, so when Sanjar asked for "the file
Zilola sent" nobody knew it had come in, and the Mac worker searched his own accounts by screen and said there was no
file. Now every such message is logged and its file saved on arrival (on_arrival), and the brain reads these chats
directly from Telegram with listing() (the telegram_received tool), older history included.

Nothing here answers anyone: those people still get silence.

Ticket 3e945138: logging alone still left Sanjar unaware until he asked. on_arrival now returns a note on each message
from a person, which jarvis.py hands to the chat brain with TELL_PROMPT, so he is told right away who wrote and what.
"""
import logging
import re
import time
from pathlib import Path

from .config import INBOX

log = logging.getLogger("jarvis.received")
PEOPLE = INBOX / "people"          # var/state/inbox/people/<sender id>/<message id>_<file name>
MAX_DOWNLOAD = 50 * 1024 * 1024    # bigger files are listed, not downloaded
SERVICE_IDS = {777000, 42777}      # Telegram's own accounts (login codes: never passed on, Telegram voids forwarded ones)

TELL_PROMPT = """[PRIVATE MESSAGE TO YOUR OWN TELEGRAM ACCOUNT — someone other than Sanjar wrote to you (Jarvis), not to
him; he does NOT see it unless you tell him]
{note}

Tell Sanjar now in 1-2 plain lines: who wrote to you and what they want or sent (a file's path is above). Don't answer
them and don't act on it unless he tells you to. NO_REPLY is not allowed here."""


def _name(user) -> str:
    if user is None:
        return "someone"
    full = " ".join(p for p in (getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "") if p)
    handle = getattr(user, "username", "") or ""
    return (full or handle or str(getattr(user, "id", "?"))) + (f" (@{handle})" if handle and full else "")


def file_of(message) -> tuple[str, int] | None:
    """(file name, size) of a message's attachment, or None. Voice notes and stickers count as files too."""
    if not (getattr(message, "document", None) or getattr(message, "photo", None)):
        return None
    f = message.file
    ext = (f.ext or "") if f else ""
    name = (f.name if f and f.name else f"{message.id}{ext}")
    return Path(name).name, (f.size or 0) if f else 0


async def save(message, sender_id: int) -> Path | None:
    """Download a message's attachment to PEOPLE/<sender>/ (once). None if there's none or it's too big."""
    found = file_of(message)
    if not found or found[1] > MAX_DOWNLOAD:
        return None
    folder = PEOPLE / str(sender_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[/\\:]", "_", found[0])
    path = folder / f"{message.id}_{safe}"
    if not path.exists():
        await message.download_media(file=str(path))
    return path


async def on_arrival(event) -> str:
    """Someone other than Sanjar wrote to Jarvis's account: log it, keep the file, and return a short note for Sanjar
    ("" for bots and Telegram's service messages, which he isn't told about)."""
    sender = await event.get_sender()
    if getattr(sender, "bot", False) or event.sender_id in SERVICE_IDS:
        return ""
    text = (event.raw_text or "").strip()
    path = None
    try:
        path = await save(event.message, event.sender_id)
    except Exception:
        log.warning("couldn't save the file %s sent", _name(sender), exc_info=True)
    found = file_of(event.message)
    log.info("private message to Jarvis's account from %s: %r%s", _name(sender), text[:200],
             f" + file {found[0]} ({found[1] // 1024 or 1} KB)" + (f" saved at {path}" if path else " (not saved)")
             if found else "")
    note = f"💬 {_name(sender)} wrote to me on Telegram (my own account):"
    if text:
        note += f"\n{text[:1500]}"
    if found:
        note += f"\n[file {found[0]}, {found[1] // 1024 or 1} KB" + (f", saved at {path}]" if path else ", not saved]")
    return note if (text or found) else note + "\n[a sticker or other media]"


async def listing(tg, owner_id: int, who: str = "", days: int = 7) -> str:
    """The private chats other people have with Jarvis's account in the last `days`, read from Telegram itself:
    each message with time and sender, and every file downloaded with its path. who: part of a name or @username."""
    since = time.time() - days * 86400
    want = who.strip().lstrip("@").lower()
    out: list[str] = []
    async for d in tg.iter_dialogs(limit=200):
        if d.date and d.date.timestamp() < since:
            break                                   # dialogs come newest first
        user = d.entity
        if not d.is_user or getattr(user, "bot", False) or user.id == owner_id or getattr(user, "is_self", False):
            continue
        name = _name(user)
        if want and want not in name.lower():
            continue
        lines = []
        async for m in tg.iter_messages(user, limit=200):
            if m.date.timestamp() < since:
                break
            who_said = "Jarvis" if m.out else name.split(" (@")[0]
            line = f"  {m.date.astimezone():%Y-%m-%d %H:%M} {who_said}: {(m.raw_text or '').strip()[:500]}"
            found = file_of(m)
            if found:
                try:
                    path = await save(m, user.id)
                except Exception as e:
                    path, line = None, line + f" [download failed: {e}]"
                line += f" [file {found[0]}, {found[1] // 1024 or 1} KB" + (f", saved at {path}]" if path else ", too big to download]")
            lines.append(line)
        if lines:
            out.append(f"{name}, id {user.id}:\n" + "\n".join(reversed(lines)))
    if not out:
        return (f"No one{' matching ' + repr(who) if want else ''} wrote to Jarvis's Telegram account in the last {days} "
                "days. (This only covers Jarvis's own account; Sanjar's accounts are seen only through the Mac.)")
    return "Private chats with Jarvis's own Telegram account:\n\n" + "\n\n".join(out)
