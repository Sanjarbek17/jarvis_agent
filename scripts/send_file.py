"""Send a file (APK, PDF, zip...) to Sanjar on Telegram from the Jarvis account. For workers.

    /Users/sanjarbek/dev/side_project/projects/jarvis/.venv/bin/python \
        /Users/sanjarbek/dev/side_project/projects/jarvis/scripts/send_file.py <file> [caption]

Works while Jarvis is running: it uses a temporary copy of the session file (the live process
keeps the original locked). The copy is removed afterwards. Telegram's limit is 2 GB per file.
"""
import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

HERE = Path(__file__).parent.parent
load_dotenv(HERE / ".env")
SESSION = HERE / "var" / "jarvis.session"


async def main(path: Path, caption: str):
    if not path.is_file():
        sys.exit(f"No such file: {path}")
    copy = HERE / "var" / f"send_tmp_{uuid.uuid4().hex[:8]}"
    shutil.copy(SESSION, copy.with_suffix(".session"))
    tg = TelegramClient(str(copy), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            sys.exit("The Jarvis session isn't logged in; run scripts/login.py first.")
        await tg.send_file(int(os.environ["OWNER_ID"]), str(path), caption=caption[:1000],
                           force_document=True)
        print(f"Sent {path.name} ({path.stat().st_size // 1024} KB) to Sanjar")
    finally:
        await tg.disconnect()
        copy.with_suffix(".session").unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(Path(sys.argv[1]).expanduser(), " ".join(sys.argv[2:])))
