"""Where Sanjar asked for a ticket, so its result goes back there (ticket 799dda33: "You have send the
message to telegram but I've asked you on the Jarvis app").

App chats tell the tools server who they are with an HTTP header on every tool call (Brain.origin ->
X-Jarvis-Origin: "app:<chat id>"). add_task / queue_ticket / answer_task remember it per ticket ref;
jarvis.notify then sends that ticket's result into the same app chat (and the app's notifications)
instead of Telegram. Tickets with no record (Telegram, calls, the board itself) go to Telegram as before.
"""
import json
import time

from .config import STATE

FILE = STATE / "ticket_origin.json"      # ref -> {"channel": "app", "chat": id, "at": ts}
HEADER = "x-jarvis-origin"
KEEP = 30 * 86400


def _load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parse(value: str) -> dict:
    """'app:<chat id>' -> {"channel": "app", "chat": id}; anything else -> {} (Telegram)."""
    channel, _, chat = (value or "").strip().partition(":")
    return {"channel": "app", "chat": chat} if channel == "app" and chat else {}


def from_ctx(ctx) -> dict:
    """The origin of the tool call being served (empty = Telegram / call)."""
    try:
        return parse(ctx.request_context.request.headers.get(HEADER, ""))
    except Exception:
        return {}


def set(ref: str, o: dict) -> None:
    """Remember where this ticket was asked from; a later ask from Telegram clears an app origin."""
    data = {k: v for k, v in _load().items() if time.time() - v.get("at", 0) < KEEP}
    if o:
        data[ref] = {**o, "at": time.time()}
    else:
        data.pop(ref, None)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data))


def get(ref: str) -> dict:
    return _load().get(ref, {})


def label(o: dict, default: str) -> str:
    """How the ticket's note names the source ("added from the app")."""
    return "the app" if o.get("channel") == "app" else default
