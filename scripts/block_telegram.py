#!/usr/bin/env python3
"""Manage blocked Telegram users on the Jarvis account (ticket fb74bc39).

Usage:
  scripts/block_telegram.py list
  scripts/block_telegram.py block <user_id> [--name <name>] [--reason <reason>]
  scripts/block_telegram.py unblock <user_id>
"""
import argparse
import asyncio
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core import blocked
from core.config import SESSION, TG_API_ID, TG_API_HASH
from telethon import TelegramClient


def list_users():
    items = blocked.load()
    if not items:
        print("No users currently blocked.")
        return
    print(f"Blocked users ({len(items)}):")
    for uid, info in sorted(items.items()):
        name = info.get("name") or "(no name)"
        uname = f" (@{info['username']})" if info.get("username") else ""
        reason = f" [{info['reason']}]" if info.get("reason") else ""
        print(f"- {uid}: {name}{uname}{reason}")


async def block_user(user_id_str: str, name: str = "", reason: str = "spam"):
    try:
        user_id = int(user_id_str)
    except ValueError:
        print(f"Error: user_id must be an integer, got {user_id_str!r}")
        sys.exit(1)

    # First add to local store
    entry = blocked.add_blocked(user_id, name=name, reason=reason)
    print(f"Saved {user_id} to blocked store ({entry.get('name') or 'no name'}).")

    # Try to execute Telegram block directly if possible
    try:
        client = TelegramClient(str(SESSION), TG_API_ID, TG_API_HASH)
        await client.connect()
        try:
            ok, msg = await blocked.block(client, user_id, name=name, reason=reason)
            print(msg)
        finally:
            await client.disconnect()
    except Exception as e:
        print(f"Note: Could not connect to Telegram directly ({e}); will sync on Jarvis startup.")


async def unblock_user(user_id_str: str):
    try:
        user_id = int(user_id_str)
    except ValueError:
        print(f"Error: user_id must be an integer, got {user_id_str!r}")
        sys.exit(1)

    try:
        client = TelegramClient(str(SESSION), TG_API_ID, TG_API_HASH)
        await client.connect()
        try:
            ok, msg = await blocked.unblock(client, user_id)
            print(msg)
        finally:
            await client.disconnect()
    except Exception as e:
        blocked.remove_blocked(user_id)
        print(f"Removed {user_id} from local blocked store (Telegram sync: {e}).")


def main():
    parser = argparse.ArgumentParser(description="Manage blocked Telegram users.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="List blocked users")

    p_block = sub.add_parser("block", help="Block a user")
    p_block.add_argument("user_id", help="Telegram user ID")
    p_block.add_argument("--name", default="", help="User's display name")
    p_block.add_argument("--reason", default="spam", help="Reason for blocking")

    p_unblock = sub.add_parser("unblock", help="Unblock a user")
    p_unblock.add_argument("user_id", help="Telegram user ID")

    args = parser.parse_args()
    if args.cmd == "list" or not args.cmd:
        list_users()
    elif args.cmd == "block":
        asyncio.run(block_user(args.user_id, name=args.name, reason=args.reason))
    elif args.cmd == "unblock":
        asyncio.run(unblock_user(args.user_id))


if __name__ == "__main__":
    main()
