"""Blocking spam Telegram senders on Telegram (ticket fb74bc39)."""
import json
import logging
import time
from typing import Any

from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.types import InputPeerUser

from .config import OWNER_ID, STATE

log = logging.getLogger("jarvis.blocked")
BLOCKED_FILE = STATE / "blocked.json"
SERVICE_IDS = {777000, 42777}


def load() -> dict[int, dict[str, Any]]:
    """Return dictionary of blocked users: {user_id: {id, name, username, access_hash, at, reason}}."""
    if not BLOCKED_FILE.exists():
        return {}
    try:
        raw = json.loads(BLOCKED_FILE.read_text())
        return {int(k): v for k, v in raw.items()}
    except Exception:
        log.exception("could not read %s", BLOCKED_FILE)
        return {}


def save(data: dict[int, dict[str, Any]]) -> None:
    """Save blocked users atomically to disk."""
    BLOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BLOCKED_FILE.with_suffix(".tmp")
    payload = {str(k): v for k, v in data.items()}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(BLOCKED_FILE)


def is_blocked(user_id: int | None) -> bool:
    """Whether user_id is blocked."""
    if user_id is None:
        return False
    return int(user_id) in load()


def add_blocked(user_id: int, name: str = "", username: str = "", access_hash: int | None = None,
                reason: str = "spam") -> dict[str, Any]:
    """Add a user to the local blocked store."""
    data = load()
    entry = data.get(int(user_id), {})
    entry.update({
        "id": int(user_id),
        "name": name or entry.get("name", ""),
        "username": username or entry.get("username", ""),
        "access_hash": access_hash if access_hash is not None else entry.get("access_hash"),
        "at": entry.get("at") or time.time(),
        "reason": reason or entry.get("reason", "spam"),
    })
    data[int(user_id)] = entry
    save(data)
    return entry


def remove_blocked(user_id: int) -> bool:
    """Remove a user from the local blocked store."""
    data = load()
    if int(user_id) in data:
        del data[int(user_id)]
        save(data)
        return True
    return False


async def block(tg, entity_or_id, name: str = "", reason: str = "spam") -> tuple[bool, str]:
    """Block a user on Telegram and record in blocked store."""
    user_id = getattr(entity_or_id, "id", entity_or_id)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False, f"Invalid user ID or entity: {entity_or_id!r}"

    if user_id == OWNER_ID:
        return False, "Cannot block Sanjar."
    if user_id in SERVICE_IDS:
        return False, "Cannot block Telegram service accounts."

    # Resolve entity / peer
    peer = None
    first = getattr(entity_or_id, "first_name", "") or ""
    last = getattr(entity_or_id, "last_name", "") or ""
    full_name = (" ".join(filter(None, [first, last]))).strip() or name
    uname = getattr(entity_or_id, "username", "") or ""
    ahash = getattr(entity_or_id, "access_hash", None)

    if hasattr(entity_or_id, "SUBCLASS_OF_ID"):  # Telethon entity
        peer = entity_or_id
    elif ahash is not None:
        peer = InputPeerUser(user_id, ahash)
    else:
        existing = load().get(user_id)
        if existing and existing.get("access_hash"):
            peer = InputPeerUser(user_id, existing["access_hash"])
            full_name = full_name or existing.get("name", "")
            uname = uname or existing.get("username", "")

    if peer is None:
        try:
            peer = await tg.get_input_entity(entity_or_id)
        except Exception:
            log.warning("could not resolve input entity for %r directly", entity_or_id)

    if peer is None:
        # Save locally even if we can't build input peer right now
        add_blocked(user_id, name=full_name, username=uname, access_hash=ahash, reason=reason)
        return False, f"Saved user {user_id} as blocked locally, but could not resolve Telegram peer."

    try:
        await tg(BlockRequest(id=peer))
        if hasattr(peer, "access_hash"):
            ahash = peer.access_hash
        add_blocked(user_id, name=full_name, username=uname, access_hash=ahash, reason=reason)
        log.info("blocked user %s (%s) on Telegram", user_id, full_name)
        display = full_name or (f"@{uname}" if uname else str(user_id))
        return True, f"Blocked {display} (id: {user_id}) on Telegram."
    except Exception as e:
        log.exception("failed to block %s on Telegram", user_id)
        add_blocked(user_id, name=full_name, username=uname, access_hash=ahash, reason=reason)
        return False, f"Could not block {user_id} on Telegram: {e}"


async def unblock(tg, entity_or_id, name: str = "") -> tuple[bool, str]:
    """Unblock a user on Telegram and remove from blocked store."""
    user_id = getattr(entity_or_id, "id", entity_or_id)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False, f"Invalid user ID or entity: {entity_or_id!r}"

    existing = load().get(user_id, {})
    full_name = name or existing.get("name", "")
    ahash = getattr(entity_or_id, "access_hash", None) or existing.get("access_hash")

    peer = None
    if hasattr(entity_or_id, "SUBCLASS_OF_ID"):
        peer = entity_or_id
    elif ahash is not None:
        peer = InputPeerUser(user_id, ahash)
    else:
        try:
            peer = await tg.get_input_entity(entity_or_id)
        except Exception:
            pass

    if peer is not None:
        try:
            await tg(UnblockRequest(id=peer))
        except Exception as e:
            log.exception("failed to unblock %s on Telegram", user_id)
            return False, f"Could not unblock {user_id} on Telegram: {e}"

    remove_blocked(user_id)
    log.info("unblocked user %s (%s) on Telegram", user_id, full_name)
    display = full_name or str(user_id)
    return True, f"Unblocked {display} (id: {user_id}) on Telegram."


async def sync_startup(tg) -> int:
    """Ensure all blocked users in local store are blocked on Telegram."""
    blocked_users = load()
    if not blocked_users:
        return 0
    count = 0
    for uid, info in blocked_users.items():
        try:
            peer = None
            if info.get("access_hash"):
                peer = InputPeerUser(uid, info["access_hash"])
            else:
                peer = await tg.get_input_entity(uid)
            await tg(BlockRequest(id=peer))
            count += 1
            log.info("startup sync: verified block on Telegram for %s (%s)", uid, info.get("name"))
        except Exception as e:
            log.warning("startup sync: could not block %s on Telegram: %s", uid, e)
    return count
