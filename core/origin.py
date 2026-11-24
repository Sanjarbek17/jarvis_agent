"""Where Sanjar asked for a ticket, so its result goes back there (ticket 799dda33: "You have send the
message to telegram but I've asked you on the Jarvis app").

App chats tell the tools server who they are with an HTTP header on every tool call (Brain.origin ->
X-Jarvis-Origin: "app:<chat id>"). add_task / queue_ticket / answer_task remember it per ticket ref;
jarvis.notify then sends that ticket's result into the same app chat (and the app's notifications)
instead of Telegram. Tickets with no record (Telegram, calls, the board itself) go to Telegram as before.
For Antigravity brain where agy connects over HTTP without custom headers, Brain tracks active origin
during ask/reply turns so tools automatically attribute requests to the originating app chat.
"""
import contextlib
import contextvars
import json
import time

from .config import STATE

FILE = STATE / "ticket_origin.json"      # ref -> {"channel": "app", "chat": id, "at": ts}
HEADER = "x-jarvis-origin"
KEEP = 30 * 86400

_current_origin: contextvars.ContextVar[str] = contextvars.ContextVar("current_origin", default="")
_active_origins: list[str] = []
_proc_origins: dict[int, str] = {}


def register_proc(pid: int, val: str) -> None:
    if pid and val:
        _proc_origins[pid] = val


def unregister_proc(pid: int) -> None:
    _proc_origins.pop(pid, None)


@contextlib.contextmanager
def active(val: str):
    """Context manager to mark the current active origin (e.g. for Antigravity brain turns)."""
    token = _current_origin.set(val or "")
    entry = val or ""
    _active_origins.append(entry)
    try:
        yield
    finally:
        _current_origin.reset(token)
        if entry in _active_origins:
            _active_origins.remove(entry)


def get_active() -> str:
    """Returns the currently active origin string (from ContextVar or process stack)."""
    ctx_val = _current_origin.get()
    if ctx_val:
        return ctx_val
    return _active_origins[-1] if _active_origins else ""


def set_active(val: str) -> None:
    """Set active origin (mostly for tests)."""
    global _active_origins
    _current_origin.set(val or "")
    _active_origins = [val] if val else []


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
        if ctx and hasattr(ctx, "request_context") and ctx.request_context:
            h = ctx.request_context.request.headers.get(HEADER, "")
            if h:
                return parse(h)
    except Exception:
        pass
    if act := get_active():
        return parse(act)
    return {}


def set(ref: str, o: dict) -> None:
    """Remember where this ticket was asked from. A call with no app chat never clears a recorded one
    (ticket 484828da): Jarvis answering or re-queueing the ticket itself (the self-check, the brain reading
    a worker's report) comes in with no header, the same as Telegram, and wiped the chat, so the result
    went to the project's latest chat instead of the one he asked in. Asked again from another app chat:
    that chat from then on."""
    data = {k: v for k, v in _load().items() if time.time() - v.get("at", 0) < KEEP}
    if o:
        data[ref] = {**o, "at": time.time()}
    elif ref in data:
        return                                       # keep the app chat he asked in
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data))


def get(ref: str) -> dict:
    return _load().get(ref, {})


def label(o: dict, default: str) -> str:
    """How the ticket's note names the source ("added from the app")."""
    return "the app" if o.get("channel") == "app" else default
