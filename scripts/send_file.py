"""Send a file (APK, PDF, zip...) to Sanjar on Telegram from the Jarvis account. For workers.

    /Users/sanjarbek/dev/side_project/projects/jarvis/.venv/bin/python \
        /Users/sanjarbek/dev/side_project/projects/jarvis/scripts/send_file.py <file> [caption]

While Jarvis is running, the file goes through its outbox (core/outbox.py): Jarvis sends it with
its own connection. A second copy of the session would make Telegram stop delivering Sanjar's
messages to Jarvis. Only when Jarvis is stopped does this connect by itself. Limit: 2 GB per file.
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
from core import outbox  # noqa: E402

load_dotenv(HERE / ".env")
SESSION = HERE / "var" / "jarvis.session"


async def direct(path: Path, caption: str):
    tg = TelegramClient(str(SESSION.with_suffix("")), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            sys.exit("The Jarvis session isn't logged in; run scripts/login.py first.")
        await tg.send_file(int(os.environ["OWNER_ID"]), str(path), caption=caption[:1000],
                           force_document=True)
    finally:
        await tg.disconnect()


def main(path: Path, caption: str):
    if not path.is_file():
        sys.exit(f"No such file: {path}")
    if outbox.jarvis_running():
        # big files take a while to upload: allow about a minute per 20 MB, at least 2 minutes
        r = outbox.send(file=path, caption=caption, document=True,
                        wait=max(120, path.stat().st_size / 20e6 * 60))
        if not r.get("ok"):
            sys.exit(f"Jarvis couldn't send it: {r.get('error')}")
    else:
        asyncio.run(direct(path, caption))
    print(f"Sent {path.name} ({path.stat().st_size // 1024} KB) to Sanjar")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).expanduser(), " ".join(sys.argv[2:]))
