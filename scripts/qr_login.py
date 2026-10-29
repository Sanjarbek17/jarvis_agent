"""Log the Jarvis Telegram account in again with a QR code, without breaking the session that works.

    .venv/bin/python scripts/qr_login.py [minutes]   # default 10

1. A new client logs in on a separate file (var/jarvis_qr.session).
2. Each QR code (it expires every ~30 s) is sent to Sanjar through the current session, the
   previous one is deleted. He scans it on a device that is logged in as the Jarvis account:
   Telegram -> Settings -> Devices -> Link Desktop Device.
3. When it's scanned, the new session must be the same account as the current one (not Sanjar's
   own). Then var/jarvis.session is backed up and replaced; restart Jarvis to use it.
Exit codes: 0 done, 2 not scanned in time, 3 wrong account, 4 needs the 2FA password.
"""
import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import segno
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

HERE = Path(__file__).parent.parent
VAR = HERE / "var"
load_dotenv(HERE / ".env")
API_ID, API_HASH, OWNER = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], int(os.environ["OWNER_ID"])
LIVE = VAR / "jarvis.session"
NEW = VAR / "jarvis_qr"                     # Telethon adds .session
HOW = ("Scan with Telegram on a device logged in as the Jarvis account (@Jarvis_Sanjar):\n"
       "Settings → Devices → Link Desktop Device. Not with your own account, sir.")


async def main(minutes: float) -> int:
    # a copy of the live session to talk to Sanjar (the running Jarvis keeps the original locked)
    talk_copy = VAR / f"qr_talk_{uuid.uuid4().hex[:6]}"
    shutil.copy(LIVE, talk_copy.with_suffix(".session"))
    NEW.with_suffix(".session").unlink(missing_ok=True)
    talk = TelegramClient(str(talk_copy), API_ID, API_HASH)
    new = TelegramClient(str(NEW), API_ID, API_HASH)
    try:
        await talk.connect()
        me_old = await talk.get_me()
        await new.connect()
        deadline, last_msg = time.monotonic() + minutes * 60, None
        await talk.send_message(OWNER, f"🔑 New Jarvis login by QR code. {HOW}\nA fresh code comes every ~30 s "
                                       f"for {minutes:.0f} minutes.")
        while time.monotonic() < deadline:
            qr = await new.qr_login()
            png = VAR / "qr_login.png"
            segno.make(qr.url, error="m").save(str(png), scale=10, border=3)
            if last_msg:
                await last_msg.delete()
            last_msg = await talk.send_file(OWNER, str(png), caption="Scan me (Link Desktop Device)")
            print(f"{time.strftime('%H:%M:%S')} QR sent, valid until {qr.expires:%H:%M:%S} UTC", flush=True)
            try:
                await qr.wait()                 # until this code expires
                print("scanned", flush=True)
                break
            except asyncio.TimeoutError:
                continue                    # expired: make a new one
            except SessionPasswordNeededError:
                await talk.send_message(OWNER, "🔑 Scanned. The account has a 2FA password; I need it to finish.")
                return 4
        else:
            if last_msg:
                await last_msg.delete()
            await talk.send_message(OWNER, "🔑 The QR codes expired without a scan. Nothing changed; the old "
                                           "login still works.")
            return 2
        if last_msg:
            await last_msg.delete()
        me_new = await new.get_me()
        if me_new.id != me_old.id:
            await new.log_out()             # that was someone else's account: remove that login again
            await talk.send_message(OWNER, f"🔑 That scan logged in @{me_new.username or me_new.id}, not the Jarvis "
                                           f"account (@{me_old.username}). I logged it out again; nothing changed.")
            return 3
        await new.disconnect()
        backup = VAR / f"jarvis.session.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(LIVE, backup)
        os.replace(NEW.with_suffix(".session"), LIVE)
        await talk.send_message(OWNER, f"✅ Logged in again as @{me_new.username} by QR code. Jarvis switches to "
                                       f"the new login at its next restart (old one backed up as {backup.name}).")
        print(f"OK: new session for @{me_new.username} in place, backup {backup.name}")
        return 0
    finally:
        for c in (talk, new):
            if c.is_connected():
                await c.disconnect()
        talk_copy.with_suffix(".session").unlink(missing_ok=True)
        (VAR / "qr_login.png").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 10)))
