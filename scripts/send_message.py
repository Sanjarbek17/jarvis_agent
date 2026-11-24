"""Send a message to a Telegram user, chat, or Sanjar from the Jarvis account.

    python scripts/send_message.py <to> <message>
    python scripts/send_message.py <message>   # sends to Sanjar (OWNER_ID)

While Jarvis is running, the message goes through its outbox (core/outbox.py): Jarvis sends it with
its own connection. Only when Jarvis is stopped does this connect by itself.
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402

sys.path.insert(0, str(HERE))
from core import outbox  # noqa: E402

load_dotenv(HERE / ".env")
SESSION = VAR / "jarvis.session"


async def direct(to: int | str, text: str):
    tg = TelegramClient(str(SESSION.with_suffix("")), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            sys.exit("The Jarvis session isn't logged in; run scripts/login.py first.")
        msg = await tg.send_message(to, text[:4000], link_preview=False)
        return getattr(msg, "id", None)
    finally:
        await tg.disconnect()


def send_message(to: int | str, text: str):
    if not text.strip():
        sys.exit("Empty message text.")
    if outbox.jarvis_running():
        r = outbox.send(text=text, to=to, wait=60)
        if not r.get("ok"):
            sys.exit(f"Jarvis couldn't send it: {r.get('error')}")
        mid = r.get("message_id")
        print(f"Sent message to {to}" + (f" (message_id: {mid})" if mid else ""))
        return mid
    else:
        mid = asyncio.run(direct(to, text))
        print(f"Sent message directly to {to}" + (f" (message_id: {mid})" if mid else ""))
        return mid


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    owner_id = int(os.environ.get("OWNER_ID", "0"))
    first = sys.argv[1]

    # Check if first arg is an explicit recipient (numeric ID or username starting with @)
    if len(sys.argv) >= 3 and (first.lstrip("-").isdigit() or first.startswith("@")):
        to = int(first) if first.lstrip("-").isdigit() else first
        text = " ".join(sys.argv[2:])
    elif first == "--to" and len(sys.argv) >= 4:
        raw_to = sys.argv[2]
        to = int(raw_to) if raw_to.lstrip("-").isdigit() else raw_to
        text = " ".join(sys.argv[3:])
    else:
        to = owner_id
        text = " ".join(sys.argv[1:])

    send_message(to, text)


if __name__ == "__main__":
    main()
