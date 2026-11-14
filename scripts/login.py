"""One-time login for Jarvis's Telegram account, in two non-interactive steps.

    uv run scripts/login.py send +998XXXXXXXXX      # Telegram sends a code to that account
    uv run scripts/login.py code 12345 [2fa-pass]   # finish login, jarvis.session is saved
    uv run scripts/login.py password <2fa-pass>     # if 2FA was asked after the code
    uv run scripts/login.py check                   # show who is logged in
"""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402  (tests: a temporary folder, never the real one)

load_dotenv(HERE / ".env")
SESSION = str(VAR / "jarvis")
PENDING = VAR / ".login_pending.json"


@asynccontextmanager
async def client():
    # connect() instead of start(): start() would prompt for a phone on stdin
    tg = TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await tg.connect()
    try:
        yield tg
    finally:
        await tg.disconnect()


async def send(phone: str) -> None:
    async with client() as tg:
        sent = await tg.send_code_request(phone)
        PENDING.write_text(json.dumps({"phone": phone, "hash": sent.phone_code_hash}))
        print("Code sent. Now run: uv run scripts/login.py code <code>")


async def code(code: str, password: str | None) -> None:
    pending = json.loads(PENDING.read_text())
    async with client() as tg:
        try:
            await tg.sign_in(pending["phone"], code, phone_code_hash=pending["hash"])
        except SessionPasswordNeededError:
            if not password:
                print("This account has 2FA. Run: uv run scripts/login.py code <code> <password>")
                return
            await tg.sign_in(password=password)
        me = await tg.get_me()
        PENDING.unlink()
        print(f"Logged in as {me.first_name} (@{me.username}, id {me.id})")


async def password(pw: str) -> None:
    # code was already accepted; the session is waiting only for the 2FA password
    async with client() as tg:
        await tg.sign_in(password=pw)
        me = await tg.get_me()
        PENDING.unlink(missing_ok=True)
        print(f"Logged in as {me.first_name} (@{me.username}, id {me.id})")


async def check() -> None:
    async with client() as tg:
        if await tg.is_user_authorized():
            me = await tg.get_me()
            print(f"Logged in as {me.first_name} (@{me.username}, id {me.id})")
        else:
            print("Not logged in")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["check"]
    if cmd == "send":
        asyncio.run(send(args[0]))
    elif cmd == "password":
        asyncio.run(password(args[0]))
    elif cmd == "code":
        asyncio.run(code(args[0], args[1] if len(args) > 1 else None))
    else:
        asyncio.run(check())
