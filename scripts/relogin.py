"""Log the Jarvis Telegram account in again on a fresh session, without freezing Jarvis or Sanjar.

    nohup .venv/bin/python scripts/relogin.py >> var/logs/relogin.log 2>&1 &

No scan and no code from Sanjar: Jarvis's current login approves the new one itself (the same
thing a phone does when it scans a login QR code), and the 2FA password comes from .env
(TG_2FA_PASSWORD; never printed or logged).
1. A new client asks Telegram for a login token on its own file (var/jarvis_relogin.session).
2. The current login approves it: through the running Jarvis's outbox (core/outbox.py) if it
   supports that, else through a copy of the live session that is connected for a few seconds
   only (a copy held open longer makes Telegram stop delivering messages to Jarvis).
3. The new login must be the same account. It's saved as var/jarvis_new.session; Jarvis swaps it
   in (backing up the old file) at its next start, before it opens the session. Replacing the
   file under the running Jarvis could corrupt it.
Exit codes: 0 done, 1 failed (nothing changed), 3 wrong account.
"""
import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.functions.account import GetPasswordRequest
from telethon.tl.functions.auth import AcceptLoginTokenRequest

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402  (tests: a temporary folder, never the real one)

sys.path.insert(0, str(HERE))
from core import outbox  # noqa: E402

load_dotenv(HERE / ".env")
API_ID, API_HASH, OWNER = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], int(os.environ["OWNER_ID"])
LIVE = VAR / "jarvis.session"
NEW = VAR / "jarvis_relogin"                # Telethon adds .session
READY = VAR / "jarvis_new.session"         # picked up by jarvis.py at its next start


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def outbox_live() -> bool:
    """The running Jarvis answers outbox notes (older builds don't read them)."""
    if not outbox.jarvis_running():
        return False
    return bool(outbox.send(whoami=True, wait=8).get("ok"))


async def approve(token: bytes, via_outbox: bool) -> tuple[int, str]:
    """Approve the token with the current login; returns (id, username) of that account."""
    if via_outbox:
        r = await asyncio.to_thread(outbox.send, accept_login=token, whoami=True, wait=30)
        if not r.get("ok"):
            raise RuntimeError(f"Jarvis couldn't approve the login: {r.get('error')}")
        return r["me"]["id"], r["me"]["username"]
    copy = VAR / f"relogin_tmp_{uuid.uuid4().hex[:6]}"
    shutil.copy(LIVE, copy.with_suffix(".session"))
    old = TelegramClient(str(copy), API_ID, API_HASH)
    try:
        await old.connect()
        if not await old.is_user_authorized():
            raise RuntimeError("the current session isn't logged in, so it can't approve a new one")
        me = await old.get_me()
        await old(AcceptLoginTokenRequest(token))
        return me.id, me.username
    finally:
        await old.disconnect()          # seconds, not minutes
        copy.with_suffix(".session").unlink(missing_ok=True)


async def main() -> int:
    via_outbox = await asyncio.to_thread(outbox_live)
    log(f"start (approve via {'Jarvis outbox' if via_outbox else 'short session copy'})")
    NEW.with_suffix(".session").unlink(missing_ok=True)
    new = TelegramClient(str(NEW), API_ID, API_HASH)
    ok = False
    try:
        await new.connect()
        qr = await new.qr_login()
        waiting = asyncio.create_task(qr.wait(60))     # listen before approving, so the update can't be missed
        await asyncio.sleep(0.5)
        old_id, old_name = await approve(qr.token, via_outbox)
        log(f"approved by the current login: @{old_name} (id {old_id})")
        try:
            await waiting
        except SessionPasswordNeededError:
            pw = os.environ.get("TG_2FA_PASSWORD", "").strip()
            if not pw:
                raise RuntimeError("the account has 2FA and TG_2FA_PASSWORD isn't in .env")
            info = await new(GetPasswordRequest())     # what Telegram says about this account's 2FA
            log(f"2FA: has_password={info.has_password} algo={type(info.current_algo).__name__} "
                f"hint={'yes' if info.hint else 'no'} recovery_email={info.has_recovery} "
                f"pending_reset={bool(info.pending_reset_date)} password_len={len(pw)}")
            try:
                await new.sign_in(password=pw)
            except PasswordHashInvalidError:
                if via_outbox and info.hint:       # to Sanjar's private chat only, never to logs or Notion
                    await asyncio.to_thread(outbox.send, text=f"🔑 Telegram says TG_2FA_PASSWORD isn't the 2FA "
                                            f"password of @{old_name}. That account's password hint is: {info.hint}")
                raise
        me = await new.get_me()
        if me.id != old_id:
            await new.log_out()
            log(f"wrong account {me.id}, logged it out")
            return 3
        await new.disconnect()
        os.replace(NEW.with_suffix(".session"), READY)
        ok = True
        log(f"OK: new login for @{me.username} saved as {READY.name}; Jarvis uses it from its next start")
        return 0
    except Exception as e:
        log(f"failed: {type(e).__name__}: {e}")
        return 1
    finally:
        if new.is_connected():
            await new.disconnect()
        NEW.with_suffix(".session").unlink(missing_ok=True)
        text = ("✅ Telegram re-login done: a fresh login for the Jarvis account is saved. Jarvis switches "
                "to it at its next restart (the old one is backed up then)." if ok else
                "🔑 The Telegram re-login didn't work; nothing changed, the old login still works. "
                "Details in var/logs/relogin.log.")
        try:
            await tell(via_outbox, text)
        except Exception as e:
            log(f"couldn't tell Sanjar: {e!r}")


async def tell(via_outbox: bool, text: str) -> None:
    if via_outbox:
        await asyncio.to_thread(outbox.send, text=text)
    else:
        log(f"(not messaged: Jarvis's outbox isn't live) {text}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
