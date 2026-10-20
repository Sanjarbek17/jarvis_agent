"""Set the Jarvis Telegram account's profile photo (default: assets/jarvis_avatar.png).

    uv run scripts/set_avatar.py [image.png]

Works while Jarvis is running: it uses a temporary copy of the session file, so it doesn't
fight the live process over the locked original. The copy is removed afterwards.
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.photos import GetUserPhotosRequest, UploadProfilePhotoRequest

HERE = Path(__file__).parent.parent
load_dotenv(HERE / ".env")
SESSION = HERE / "var" / "jarvis.session"
COPY = HERE / "var" / "set_avatar_tmp"


async def main(image: Path):
    shutil.copy(SESSION, COPY.with_suffix(".session"))
    tg = TelegramClient(str(COPY), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            sys.exit("The Jarvis session isn't logged in; run scripts/login.py first.")
        me = await tg.get_me()
        await tg(UploadProfilePhotoRequest(file=await tg.upload_file(str(image))))
        photos = await tg(GetUserPhotosRequest(me.id, offset=0, max_id=0, limit=1))
        print(f"Profile photo set for @{me.username} ({len(photos.photos)} newest photo found)")
    finally:
        await tg.disconnect()
        COPY.with_suffix(".session").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "assets" / "jarvis_avatar.png"))
