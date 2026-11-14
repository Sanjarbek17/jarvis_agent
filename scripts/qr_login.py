"""Log the Jarvis Telegram account in again with a QR code, without breaking the session that works.

    .venv/bin/python scripts/qr_login.py [minutes]   # default 10

1. A new client logs in on a separate file (var/jarvis_qr.session).
2. Each QR code (it expires every ~30 s) is sent to Sanjar, the previous one is deleted. He scans
   it on a device that is logged in as the Jarvis account:
   Telegram -> Settings -> Devices -> Link Desktop Device.
   The messages go through the running Jarvis (core/outbox.py), never through a copy of its
   session: a copy would make Telegram stop delivering Sanjar's messages to Jarvis meanwhile.
3. If the account has a 2FA password, Sanjar sends Jarvis `/2fa <password>` (Jarvis deletes that
   message at once and hands the password over through var/state/, mode 600).
4. The new session must be the same account as the current one (not Sanjar's own). Then
   var/jarvis.session is backed up and replaced; restart Jarvis to use it.
Exit codes: 0 done, 2 not scanned in time, 3 wrong account, 4 2FA password not given / wrong.
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import segno
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402  (tests: a temporary folder, never the real one)

sys.path.insert(0, str(HERE))
from core import outbox  # noqa: E402

load_dotenv(HERE / ".env")
API_ID, API_HASH, OWNER = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], int(os.environ["OWNER_ID"])
LIVE = VAR / "jarvis.session"
NEW = VAR / "jarvis_qr"                     # Telethon adds .session
PNG = VAR / "qr_login.png"
PASSWORD_WAIT = 5 * 60
HOW = ("Scan with Telegram on a device logged in as the Jarvis account (@Jarvis_Sanjar):\n"
       "Settings → Devices → Link Desktop Device. Not with your own account, sir.")


class Talk:
    """Messages to Sanjar: through the running Jarvis, or (Jarvis stopped) its own session directly."""

    def __init__(self):
        self.via_jarvis = outbox.jarvis_running()
        self.tg = None if self.via_jarvis else TelegramClient(str(LIVE.with_suffix("")), API_ID, API_HASH)
        self.keyed = {}

    async def me(self):
        if self.via_jarvis:
            r = await asyncio.to_thread(outbox.send, whoami=True)
            if not r.get("ok"):
                raise RuntimeError(f"Jarvis didn't answer the outbox: {r.get('error')}")
            return r["me"]["id"], r["me"]["username"]
        await self.tg.connect()
        me = await self.tg.get_me()
        return me.id, me.username

    async def say(self, text="", file="", caption="", key=""):
        if self.via_jarvis:
            r = await asyncio.to_thread(outbox.send, text=text, file=file, caption=caption, key=key)
            if not r.get("ok"):
                print(f"outbox: {r.get('error')}", flush=True)
            return
        if key in self.keyed:
            await self.keyed.pop(key).delete()
        msg = (await self.tg.send_file(OWNER, file, caption=caption) if file
               else await self.tg.send_message(OWNER, text))
        if key:
            self.keyed[key] = msg

    async def drop(self, key):
        if self.via_jarvis:
            await asyncio.to_thread(outbox.send, delete_key=key)
        elif key in self.keyed:
            await self.keyed.pop(key).delete()

    async def close(self):
        if self.tg and self.tg.is_connected():
            await self.tg.disconnect()


async def ask_password(new: TelegramClient, talk: Talk) -> bool:
    """2FA: wait for /2fa <password> from Sanjar (via Jarvis), up to 3 tries."""
    if not talk.via_jarvis:
        await talk.say("🔑 Scanned, but the account has a 2FA password and Jarvis isn't running to take it. "
                       "Start Jarvis and run the login again.")
        return False
    outbox.SECRET_2FA.unlink(missing_ok=True)
    outbox.WANT_2FA.touch()
    await talk.say("🔑 Scanned. The account has a 2FA password: send me /2fa <password> "
                   f"(I delete that message right away). Waiting {PASSWORD_WAIT // 60} minutes.")
    for _ in range(3):
        end = time.monotonic() + PASSWORD_WAIT
        while time.monotonic() < end and not outbox.SECRET_2FA.exists():
            await asyncio.sleep(1)
        if not outbox.SECRET_2FA.exists():
            await talk.say("🔑 No password came, so I stopped. Nothing changed; the old login still works.")
            return False
        pw = outbox.SECRET_2FA.read_text().strip()
        outbox.SECRET_2FA.unlink(missing_ok=True)
        try:
            await new.sign_in(password=pw)
            return True
        except PasswordHashInvalidError:
            await talk.say("🔑 That password is wrong. Send /2fa <password> again.")
    await talk.say("🔑 Wrong password three times, so I stopped. Nothing changed.")
    return False


async def main(minutes: float) -> int:
    NEW.with_suffix(".session").unlink(missing_ok=True)
    talk = Talk()
    new = TelegramClient(str(NEW), API_ID, API_HASH)
    try:
        old_id, old_name = await talk.me()
        await new.connect()
        deadline = time.monotonic() + minutes * 60
        await talk.say(f"🔑 New Jarvis login by QR code. {HOW}\nA fresh code comes every ~30 s "
                       f"for {minutes:.0f} minutes.")
        while time.monotonic() < deadline:
            qr = await new.qr_login()
            segno.make(qr.url, error="m").save(str(PNG), scale=10, border=3)
            await talk.say(file=PNG, caption="Scan me (Link Desktop Device)", key="qr")
            print(f"{time.strftime('%H:%M:%S')} QR sent, valid until {qr.expires:%H:%M:%S} UTC", flush=True)
            try:
                await qr.wait()                 # until this code expires
                print("scanned", flush=True)
                break
            except asyncio.TimeoutError:
                continue                        # expired: make a new one
            except SessionPasswordNeededError:
                await talk.drop("qr")
                if not await ask_password(new, talk):
                    return 4
                break
        else:
            await talk.drop("qr")
            await talk.say("🔑 The QR codes expired without a scan. Nothing changed; the old login still works.")
            return 2
        await talk.drop("qr")
        me_new = await new.get_me()
        if me_new.id != old_id:
            await new.log_out()                 # that was someone else's account: remove that login again
            await talk.say(f"🔑 That scan logged in @{me_new.username or me_new.id}, not the Jarvis "
                           f"account (@{old_name}). I logged it out again; nothing changed.")
            return 3
        await new.disconnect()
        backup = VAR / f"jarvis.session.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(LIVE, backup)
        os.replace(NEW.with_suffix(".session"), LIVE)
        await talk.say(f"✅ Logged in again as @{me_new.username} by QR code. Jarvis switches to the new "
                       f"login at its next restart (old one backed up as {backup.name}).")
        print(f"OK: new session for @{me_new.username} in place, backup {backup.name}")
        return 0
    finally:
        if new.is_connected():
            await new.disconnect()
        await talk.close()
        PNG.unlink(missing_ok=True)
        outbox.WANT_2FA.unlink(missing_ok=True)
        outbox.SECRET_2FA.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 10)))
