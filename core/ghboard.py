"""Projects whose tickets are GitHub Issues on a GitHub Project board instead of a Notion board.

config/projects.json: "github": {"repo": "owner/name", "owner": "owner", "project": 4, "prefix": "aet"}
(no "board"). board.py sends such projects here; everything else stays the same for the runner,
tools and brain.

- The ticket is the issue; its ref is "<prefix>-<number>" (e.g. aet-12), page_id "gh:<repo>#<number>".
- The Jarvis column is a label: jarvis:queued / working / needs-me / done / failed. Sanjar queues an
  issue by adding jarvis:queued (or Jarvis does it when asked).
- The board's own Status is the Project's Status field (Todo / In Progress / Test / Done); Done
  also closes the issue, Dropped closes it as "not planned".
- Results, answers and decisions are posted as issue comments. Step-by-step progress ("• ..." lines)
  is not: it would bury the issue. The short "Jarvis note" lives in var/state/gh_notes.json.
Uses the `gh` CLI (logged in as Sanjar, with the `project` scope).
"""
import asyncio
import json
import logging
import shutil
import time

from .config import PROJECTS, STATE, reload_projects

log = logging.getLogger("jarvis.ghboard")
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"
LABELS = {"Queued": "jarvis:queued", "Working": "jarvis:working", "Needs me": "jarvis:needs-me",
          "Done": "jarvis:done", "Failed": "jarvis:failed"}
STATE_OF = {v: k for k, v in LABELS.items()}
COLORS = {"Queued": "1d76db", "Working": "fbca04", "Needs me": "d93f0b", "Done": "0e8a16", "Failed": "b60205"}
STAGES = {"todo": "Todo", "doing": "In Progress", "done": "Done"}
NOTES = STATE / "gh_notes.json"     # page_id -> {"note", "jarvis", "at"}
CACHE_SECONDS = 120                 # one board read per 2 min: polling every 10 s used up GitHub's GraphQL
                                    # limit (5000/h) and every read then failed, which stalled the runner
BACKOFF = 300                       # after a failed read (e.g. rate limit): serve the last copy this long
ITEM_LIMIT = "200"

_cache: dict[str, tuple[float, list]] = {}
_fields: dict[str, dict] = {}       # project -> {"project_id", "status_field", "options": {name: id}}
_labels_ready: set[str] = set()
_backoff: dict[str, float] = {}     # project -> monotonic time until which we don't ask GitHub again
_item_ids: dict[str, dict[int, str]] = {}   # project -> issue number -> Project item id (from board reads)


def projects() -> dict[str, dict]:
    reload_projects()
    return {name: p["github"] for name, p in PROJECTS.items() if p.get("github")}


def is_gh(project: str) -> bool:
    return project in projects()


def is_gh_page(page_id: str) -> bool:
    return page_id.startswith("gh:")


async def _gh(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(GH, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    out, err = await proc.communicate()
    if check and proc.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {err.decode(errors='replace').strip()[:300]}")
    return out.decode(errors="replace")


def _notes() -> dict:
    try:
        return json.loads(NOTES.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_note(page_id: str, **kv) -> None:
    notes = _notes()
    notes.setdefault(page_id, {}).update(kv, at=time.time())
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    NOTES.write_text(json.dumps(notes, ensure_ascii=False, indent=1))


def _split(page_id: str) -> tuple[str, int]:
    repo, num = page_id.removeprefix("gh:").rsplit("#", 1)
    return repo, int(num)


def _project_of(page_id: str) -> str:
    repo = _split(page_id)[0]
    return next(name for name, g in projects().items() if g["repo"] == repo)


def _ticket(project: str, item: dict, notes: dict):
    from .board import Ticket
    g, c = projects()[project], item["content"]
    page_id = f"gh:{c['repository']}#{c['number']}"
    jarvis = next((STATE_OF[l] for l in item.get("labels") or [] if l in STATE_OF), "")
    return Ticket(page_id=page_id, ref=f"{g['prefix']}-{c['number']}", project=project,
                  title=c.get("title") or item.get("title", ""), status=item.get("status") or "",
                  jarvis=jarvis, note=notes.get(page_id, {}).get("note", ""), sprint=item.get("sprint") or "",
                  url=c.get("url", ""), edited="")


async def tickets(project: str, fresh: bool = False) -> list:
    """Every issue on the project's GitHub board (cached a few seconds), in the board's Order."""
    hit = _cache.get(project)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if hit and time.monotonic() < _backoff.get(project, 0):
        return hit[1]                     # GitHub refused recently: the last good copy, don't hammer it
    g = projects()[project]
    try:
        data = json.loads(await _gh("project", "item-list", str(g["project"]), "--owner", g["owner"],
                                    "--format", "json", "--limit", ITEM_LIMIT))
    except Exception:
        _backoff[project] = time.monotonic() + BACKOFF
        if hit:
            log.warning("GitHub board %s unreadable; using the copy from %.0f s ago", project, time.monotonic() - hit[0])
            return hit[1]
        raise
    notes = _notes()
    items = [it for it in data.get("items", []) if (it.get("content") or {}).get("type") == "Issue"]
    _item_ids[project] = {it["content"]["number"]: it["id"] for it in items}
    # the board's Order field is the build sequence (dependencies first); issue number breaks ties
    items.sort(key=lambda it: (it.get("order") if isinstance(it.get("order"), (int, float)) else 1e9,
                               it["content"]["number"]))
    out = [_ticket(project, it, notes) for it in items]
    _cache[project] = (time.monotonic(), out)
    return out


def _forget(project: str) -> None:
    _cache.pop(project, None)


async def _ensure_labels(repo: str) -> None:
    if repo in _labels_ready:
        return
    for state, name in LABELS.items():
        await _gh("label", "create", name, "-R", repo, "--color", COLORS[state], "--force",
                  "--description", f"Jarvis: {state}", check=False)
    _labels_ready.add(repo)


async def _field_info(project: str) -> dict:
    if project not in _fields:
        g = projects()[project]
        fields = json.loads(await _gh("project", "field-list", str(g["project"]), "--owner", g["owner"],
                                      "--format", "json"))["fields"]
        status = next(f for f in fields if f["name"] == "Status")
        view = json.loads(await _gh("project", "view", str(g["project"]), "--owner", g["owner"], "--format", "json"))
        _fields[project] = {"project_id": view["id"], "status_field": status["id"],
                            "options": {o["name"]: o["id"] for o in status.get("options", [])}}
    return _fields[project]


async def _item_id(project: str, number: int, url: str = "") -> str:
    """The issue's item on the Project board (added to it if missing). Known items come from the last
    board read: a full read per status change used up GitHub's rate limit."""
    if number in _item_ids.get(project, {}):
        return _item_ids[project][number]
    g = projects()[project]
    data = json.loads(await _gh("project", "item-list", str(g["project"]), "--owner", g["owner"],
                                "--format", "json", "--limit", ITEM_LIMIT))
    for it in data.get("items", []):
        if (it.get("content") or {}).get("number") == number:
            return it["id"]
    url = url or f"https://github.com/{g['repo']}/issues/{number}"
    return json.loads(await _gh("project", "item-add", str(g["project"]), "--owner", g["owner"],
                                "--url", url, "--format", "json"))["id"]


async def set_status(project: str, number: int, name: str, url: str = "") -> bool:
    info = await _field_info(project)
    option = info["options"].get(name)
    if not option:
        return False
    for attempt in range(3):          # GitHub sometimes answers bursts with "Something went wrong"
        try:
            await _gh("project", "item-edit", "--id", await _item_id(project, number, url),
                      "--project-id", info["project_id"], "--field-id", info["status_field"],
                      "--single-select-option-id", option)
            return True
        except RuntimeError:
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    return False


async def comment(page_id: str, text: str) -> None:
    repo, num = _split(page_id)
    await _gh("issue", "comment", str(num), "-R", repo, "--body", text[:60000])


async def update(t, *, jarvis: str | None = None, note: str | None = None, stage: str | None = None) -> None:
    repo, num = _split(t.page_id)
    if jarvis:
        await _ensure_labels(repo)
        others = ",".join(l for s, l in LABELS.items() if s != jarvis)
        await _gh("issue", "edit", str(num), "-R", repo, "--add-label", LABELS[jarvis], "--remove-label", others)
    if note is not None or jarvis:
        _save_note(t.page_id, **({"note": note} if note is not None else {}), **({"jarvis": jarvis} if jarvis else {}))
    if stage:
        await set_status(t.project, num, STAGES[stage], t.url)
        if stage == "done":
            await _gh("issue", "close", str(num), "-R", repo, "--reason", "completed", check=False)
        elif stage == "doing":
            await _gh("issue", "reopen", str(num), "-R", repo, check=False)
    _forget(t.project)


async def clear_jarvis(t, reason: str) -> None:
    repo, num = _split(t.page_id)
    await _gh("issue", "edit", str(num), "-R", repo, "--remove-label", ",".join(LABELS.values()), check=False)
    _save_note(t.page_id, note=reason, jarvis="")
    _forget(t.project)


async def append_log(page_id: str, lines: list[str]) -> None:
    """Results and decisions become one issue comment; worker step lines ("• ...") are skipped."""
    keep = [l for l in lines if l and not l.startswith("• ")]
    if keep:
        await comment(page_id, "**Jarvis:** " + "\n\n".join(keep))


async def add_ticket(project: str, title: str, body: str, queued: bool = True):
    g = projects()[project]
    await _ensure_labels(g["repo"])
    args = ["issue", "create", "-R", g["repo"], "--title", title[:200], "--body", body[:60000]]
    if queued:
        args += ["--label", LABELS["Queued"]]
    url = (await _gh(*args)).strip().splitlines()[-1]
    number = int(url.rstrip("/").rsplit("/", 1)[1])
    await set_status(project, number, "Todo", url)
    _forget(project)
    return next(t for t in await tickets(project, fresh=True) if t.page_id.endswith(f"#{number}"))


async def drop(t, reason: str) -> str:
    repo, num = _split(t.page_id)
    await clear_jarvis(t, f"dropped: {reason}")
    await _gh("issue", "close", str(num), "-R", repo, "--reason", "not planned", "--comment",
              f"**Jarvis:** Dropped: {reason}")
    return "issue closed as not planned"


async def body_lines(page_id: str) -> list[str]:
    repo, num = _split(page_id)
    data = json.loads(await _gh("issue", "view", str(num), "-R", repo, "--json", "body"))
    return [l for l in (data.get("body") or "").splitlines() if l.strip()]


def recently_finished(t, within: float = 86400) -> bool:
    n = _notes().get(t.page_id, {})
    return n.get("jarvis") in ("Done", "Failed") and time.time() - n.get("at", 0) < within
