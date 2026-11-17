"""One-time posts in other people's Telegram groups from the Jarvis account, through the outbox (ticket ee89e121).

Sanjar approved one announcement of @gasfinder_uz_bot in 8 drivers' groups, sent from this account. Guarded so it
can't turn into spam:
- only chats listed in var/state/announce_allow.json, each at most once (marked done with the message link);
- joins the group first, then posts only if members may send text (a photo only where photos are allowed);
- any flood / spam / ban answer from Telegram stops everything for good (the "stopped" reason is saved);
- spambot_check() asks @SpamBot whether the account is restricted.
Runs inside Jarvis (its own connection), so Jarvis keeps receiving Sanjar's messages.
"""
import asyncio
import json
import time
from pathlib import Path

from .paths import VAR

ALLOW = VAR / "state" / "announce_allow.json"
STOP_ERRORS = ("FloodWait", "PeerFlood", "UserBannedInChannel", "UserRestricted", "SlowModeWait", "ChatRestricted",
               "AuthKey", "UserDeactivated", "PhoneNumberBanned", "Spam")


def _load() -> dict:
    try:
        return json.loads(ALLOW.read_text())
    except (FileNotFoundError, ValueError):
        return {"chats": {}, "stopped": None}


def _save(d: dict) -> None:
    tmp = ALLOW.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.rename(ALLOW)


async def post(tg, chat: str, text: str, file: str = "") -> dict:
    """Join `chat` and post `text` (with `file` as a photo when allowed). Returns {"ok", "link"|"error", ...}."""
    from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
    d = _load()
    if d.get("stopped"):
        return {"ok": False, "error": f"stopped earlier: {d['stopped']}"}
    entry = d["chats"].get(chat)
    if entry is None:
        return {"ok": False, "error": "not an approved chat"}
    if entry.get("done"):
        return {"ok": False, "error": "already posted there", "link": entry.get("link")}
    try:
        await tg(JoinChannelRequest(chat))
        await asyncio.sleep(3)
        full = await tg(GetFullChannelRequest(chat))
        ch = full.chats[0]
        rights = getattr(ch, "default_banned_rights", None)
        if rights and rights.send_messages:
            entry.update(done=True, skipped="members can't post"); _save(d)
            return {"ok": False, "error": "members can't post here"}
        photo_ok = bool(file) and Path(file).is_file() and not (rights and (rights.send_media or rights.send_photos))
        if photo_ok:
            msg = await tg.send_file(chat, file, caption=text[:1024])
        else:
            msg = await tg.send_message(chat, text, link_preview=False)
        if msg is None:
            # Telegram took the post but answered with an empty message: the group (its anti-spam or a bot)
            # deleted it at once. It still counts as our one post there, so mark it done - never resend.
            entry.update(done=True, removed=True, at=time.time(), photo=photo_ok); _save(d)
            return {"ok": False, "removed": True, "error": "sent, but the group deleted it at once", "stop": False}
        link = f"https://t.me/{getattr(ch, 'username', None) or chat}/{msg.id}"
        entry.update(done=True, link=link, at=time.time(), photo=photo_ok); _save(d)
        return {"ok": True, "link": link, "photo": photo_ok}
    except Exception as e:
        name = type(e).__name__
        if any(s in name for s in STOP_ERRORS):
            d["stopped"] = f"{name} in {chat}: {str(e)[:200]}"
            _save(d)
        return {"ok": False, "error": f"{name}: {str(e)[:200]}", "stop": bool(d.get("stopped"))}


async def spambot_check(tg) -> dict:
    """Ask @SpamBot about this account; its answer says whether the account is limited."""
    try:
        await tg.send_message("SpamBot", "/start")
        await asyncio.sleep(6)
        msgs = await tg.get_messages("SpamBot", limit=1)
        text = msgs[0].message if msgs else ""
        free = any(w in text.lower() for w in ("no limits", "good news", "не ограничен", "свободен", "free"))
        return {"ok": True, "text": text[:500], "free": free}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
