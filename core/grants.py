"""Per-ticket permissions only Sanjar gives, through Jarvis (e.g. "you may push to main").

Kept in Jarvis's own state file, not on the Notion ticket, so a worker editing its ticket page
can't grant itself anything. A grant lasts until the ticket is Done, or GRANT_DAYS at most.
"""
import json
import time

from .config import STATE

GRANTS = STATE / "grants.json"
GRANT_DAYS = 7
PUSH_MAIN = "push_main"


def _load() -> dict[str, dict]:
    try:
        return json.loads(GRANTS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(grants: dict[str, dict]) -> None:
    GRANTS.write_text(json.dumps(grants, indent=2))


def allow(ref: str, what: str, words: str) -> None:
    """Record that Sanjar allowed `what` for ticket `ref`, with his words as the reason."""
    grants = _load()
    grants.setdefault(ref, {})[what] = {"at": time.time(), "words": words}
    _save(grants)


def allowed(ref: str, what: str) -> bool:
    g = _load().get(ref, {}).get(what)
    return bool(g) and time.time() - g["at"] < GRANT_DAYS * 86400


def revoke(ref: str, what: str | None = None) -> bool:
    """Remove one grant (or all of a ticket's). True if there was something to remove."""
    grants = _load()
    if ref not in grants or (what and what not in grants[ref]):
        return False
    if what:
        grants[ref].pop(what)
    if not what or not grants[ref]:
        grants.pop(ref)
    _save(grants)
    return True
