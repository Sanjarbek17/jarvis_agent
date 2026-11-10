"""The Jarvis Android app's server (ticket 27099a8a): projects, chats per project, text and voice
messages, and live calls, for Sanjar's phone on the home Wi-Fi.

Every request needs a token: header "Authorization: Bearer <token>", or ?token=<token> on the call
WebSocket. Two kinds (ticket 31ed52a7):
- APP_TOKEN from .env, built into the APK: home Wi-Fi / Tailscale only. Requests that come in through
  the public Cloudflare tunnel (they carry Cf-Connecting-IP) are refused with it, so a leaked APK alone
  can't reach Jarvis from the internet.
- a login session: the app asks for a code (POST /api/login/start), Jarvis sends a 6-digit code to
  Sanjar's Telegram, the app trades it for a long-lived session key (POST /api/login). Only its
  SHA-256 is stored (var/state/app_sessions.json). Works everywhere; POST /api/logout ends it.

    POST /api/login/start {device?}          sends a code to Sanjar's Telegram (rate-limited)
    POST /api/login {code, device?}          {token}: the session key; 5 wrong tries kill the code
    POST /api/logout                         ends the session in the Authorization header

    GET  /api/projects                       [{name, about}] ("General" first)
    GET  /api/chats?project=X                that project's chats, newest first
    POST /api/chats {project, title?}        a new chat
    POST /api/chats/{id}/move {project}      moves it to another project; messages and conversation stay
    GET  /api/chats/{id}/messages            [{role: "me"|"jarvis", text, ts, voice?}]
    POST /api/chats/{id}/messages {text, images?, reply_to?}  Jarvis's reply (waits for it); images: names from
                                             /api/uploads; reply_to: the "ts" of the message he's answering (ticket
                                             256550ce): Jarvis sees it quoted, the message keeps {"reply": quote}
    POST /api/uploads  (image body)          {name}: a photo Sanjar attaches (JPEG/PNG/WebP/HEIC/GIF, up to 20 MB),
                                             shrunk to IMAGE_MAX_PX JPEG; refused if it isn't an image
    POST /api/chats/{id}/voice?reply_to=ts (audio body) {heard, reply}: transcribed like a Telegram voice note
    GET  /api/images/{name}                  an image named in a message's "images" list (JPEG/PNG)
    GET  /api/files/{name}                   a file from a message's "files" list [{name, title, size}]
    GET  /api/notifications?after=<ts>       {chat, items}: what Jarvis wrote first (ticket news, "needs
                                             you", the daily summary) since ts; the phone polls this
    GET  /api/board?project=X                {project, local, columns, tickets: [...]}: any board, Notion too
    POST /api/board {project, title, text, queue?}  a new ticket; {ticket}
    GET  /api/tickets/{ref}                  {ticket, text, log: [{at, line}], notion_url?}
    POST /api/tickets/{ref} {action, ...}    queue / unqueue / done / drop / edit {title?, text?, status?,
                                             sprint?} (edit: boards on the Mac only); {ticket}
    GET  /api/mac/screen?w=1280              the Mac's screen as JPEG; X-Mac-Width/X-Mac-Height: its size in points
    POST /api/mac/input {action, ...}        click {x,y,right?,double?} / move {x,y} / drag {x1,y1,x2,y2} / scroll {dy,x?,y?}
                                             / type {text} / key {key: "cmd+tab"} / wake. Both: login session only (ticket 436ccc83)
    GET  /api/workers                        {count, workers: [{project, ref, title, url, started, step,
                                             active}], waiting}: the ticket workers running right now
    WS   /api/call?token=&chat=              a live call: phone sends 48 kHz mono s16 PCM, gets
                                             24 kHz mono s16 PCM back; text frames are transcript
                                             lines {"who", "text"}

The website (web/, ticket: website) is served from here too, so the app and the site share this one
backend: GET / and its few files need no login, everything they do goes through the /api routes above
with a login session (the site never has APP_TOKEN).

Each chat is its own Claude conversation (resumed by session id, stopped when idle), told which
project it is about, so Sanjar doesn't have to say it. Chats live in var/state/app_chats/.

"Updates from Jarvis" is the same conversation as Telegram: same brain, and what is said on one side
shows on the other (commands like /status and /2fa stay on Telegram).
Whatever Jarvis tells Sanjar on its own (push()) also lands in the "Updates from Jarvis" chat (ticket
e028892d); the app polls /api/notifications and shows a phone notification, no Google services needed.
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import board, localboard, macremote
from .brain import Brain, is_silent
from .config import PROJECTS, STATE, reload_projects
from .voice import transcribe_audio

log = logging.getLogger("jarvis.app")
PORT = 8790
CHATS = STATE / "app_chats"
IMAGES = STATE / "app_images"          # screenshots and photos shown in app chats (ticket 546d965e)
IMAGE_MAX_PX = 1280                    # longest side: plenty for a phone, small over mobile data
_IMAGE_NAME = re.compile(r"^[0-9a-f]{16}\.(jpg|png)$")
FILES = STATE / "app_files"            # copies of what send_file.py sends, for the app and the website
FILE_MAX = 500_000_000                 # bigger ones stay on Telegram only
FILE_KEEP = 30 * 86400
_FILE_NAME = re.compile(r"^[0-9a-f]{16}(\.[A-Za-z0-9]{1,8})?$")
UPLOAD_MAX = 20_000_000                # an attached photo (ticket 5d5a2c64)
UPLOADS_PER_MESSAGE = 10
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".heic"}
SESSIONS = STATE / "app_sessions.json"
CODE_TTL = 5 * 60                      # a login code works this long
CODE_TRIES = 5                         # wrong guesses before the code is dropped
CODE_GAP = 60                          # at most one code a minute...
CODES_PER_HOUR = 5                     # ...and this many an hour
IDLE_STOP = 15 * 60                    # stop a chat's Claude process after this long unused
GENERAL = "General"
UPDATES = "updates"                    # chat id of "Updates from Jarvis", the same conversation as Telegram
KEEP_UPDATES = 300
WEB = Path(__file__).resolve().parent.parent / "web"
WEB_FILES = {"": "index.html", "app.js": "app.js", "style.css": "style.css",   # only these are served
             "mic-worklet.js": "mic-worklet.js", "icon.svg": "icon.svg"}
WEB_HEADERS = {
    "Cache-Control": "no-cache",        # a new version shows on the next load
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self)",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
                               "connect-src 'self'; media-src blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}

_brains: dict[str, tuple[Brain, float]] = {}   # chat id -> (brain, last used)
start_call: Callable[[WebSocket, dict], Awaitable[None]] | None = None   # set by jarvis.py
worker_status: Callable[[], dict] | None = None                          # set by jarvis.py (runner.snapshot)
send_code: Callable[[str], Awaitable] | None = None                      # set by jarvis.py: text to Sanjar's Telegram
main_brain: Brain | None = None                                          # set by jarvis.py: the Telegram conversation
tell_telegram: Callable[[str], Awaitable] | None = None                  # set by jarvis.py: text to Sanjar's Telegram

_code: dict = {}                       # the one pending login code: {hash, until, tries}
_code_times: list[float] = []          # when codes were sent (rate limit)


def token() -> str:
    return os.environ.get("APP_TOKEN", "")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def public(req) -> bool:
    """Came in through the Cloudflare tunnel (Cloudflare always sets this header; LAN requests don't have it)."""
    return bool(req.headers.get("cf-connecting-ip"))


def _client(req) -> str:
    return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "?")


def load_sessions() -> dict:
    try:
        return json.loads(SESSIONS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions(sessions: dict) -> None:
    SESSIONS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions))
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSIONS)


def _bearer(req) -> str:
    return req.headers.get("authorization", "").removeprefix("Bearer ").strip() or req.query_params.get("token", "")


def _authorized(req) -> bool:
    got = _bearer(req)
    if not got:
        return False
    sessions = load_sessions()
    hit = sessions.get(_hash(got))
    if hit:
        if time.time() - hit.get("used", 0) > 3600:    # note "last used", at most hourly
            hit["used"] = time.time()
            _save_sessions(sessions)
        return True
    want = token()
    return bool(want) and not public(req) and secrets.compare_digest(got, want)


def end_all_sessions() -> int:
    """Telegram /logoutall: every app and website login ends, so nobody can control the Mac until a new
    code is asked for. Returns how many there were."""
    sessions = load_sessions()
    _save_sessions({})
    return len(sessions)


def _session_of(req) -> dict | None:
    """The login session of this request. Only sessions may control the Mac: the APK's built-in token,
    which anyone with the APK file has, never can (not even on the home Wi-Fi)."""
    got = _bearer(req)
    return load_sessions().get(_hash(got)) if got else None


def _deny():
    return JSONResponse({"error": "unauthorized", "login": True}, status_code=401)


# --- login: a code to Sanjar's Telegram, traded for a session key -------------------------------

async def login_start(req: Request):
    now = time.time()
    _code_times[:] = [t for t in _code_times if now - t < 3600]
    if send_code is None:
        return JSONResponse({"error": "Jarvis can't send Telegram messages right now"}, status_code=503)
    if _code_times and now - _code_times[-1] < CODE_GAP:
        return JSONResponse({"error": "A code was just sent. Wait a minute and try again."}, status_code=429)
    if len(_code_times) >= CODES_PER_HOUR:
        return JSONResponse({"error": "Too many codes this hour. Try again later."}, status_code=429)
    device = str((await _json(req)).get("device") or "phone")[:60]
    code = f"{secrets.randbelow(1_000_000):06d}"
    _code.clear()
    _code.update({"hash": _hash(code), "until": now + CODE_TTL, "tries": 0})
    _code_times.append(now)
    where = "over the internet" if public(req) else "on the home network"
    log.info("app login code requested from %s (%s)", _client(req), where)
    await send_code(f"🔐 Jarvis app login code: {code}\n\nAsked for {where} by \"{device}\". It works for 5 minutes. "
                    "If that wasn't you, ignore this message.")
    return JSONResponse({"ok": True, "ttl": CODE_TTL})


async def login(req: Request):
    body = await _json(req)
    code = str(body.get("code") or "").strip()
    if not _code or time.time() > _code["until"]:
        _code.clear()
        return JSONResponse({"error": "No valid code. Ask for a new one."}, status_code=400)
    if not secrets.compare_digest(_hash(code), _code["hash"]):
        _code["tries"] += 1
        log.warning("wrong app login code from %s (%d)", _client(req), _code["tries"])
        if _code["tries"] >= CODE_TRIES:
            _code.clear()
            return JSONResponse({"error": "Too many wrong tries. Ask for a new code."}, status_code=400)
        return JSONResponse({"error": "Wrong code."}, status_code=400)
    _code.clear()
    session = secrets.token_urlsafe(32)
    sessions = load_sessions()
    sessions[_hash(session)] = {"device": str(body.get("device") or "phone")[:60], "created": time.time(),
                                "used": time.time(), "from": _client(req)}
    _save_sessions(sessions)
    log.info("app logged in from %s", _client(req))
    return JSONResponse({"token": session})


async def logout(req: Request):
    sessions = load_sessions()
    if sessions.pop(_hash(_bearer(req)), None) is None:
        return _deny()
    _save_sessions(sessions)
    return JSONResponse({"ok": True})


async def _json(req: Request) -> dict:
    with contextlib.suppress(Exception):
        body = await req.json()
        if isinstance(body, dict):
            return body
    return {}


# --- chats on disk ---------------------------------------------------------------------------

def _path(chat_id: str):
    return CHATS / f"{chat_id}.json"


def load_chat(chat_id: str) -> dict | None:
    if not chat_id.replace("-", "").isalnum():
        return None
    try:
        return json.loads(_path(chat_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_chat(chat: dict) -> None:
    CHATS.mkdir(parents=True, exist_ok=True)
    tmp = _path(chat["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(chat, ensure_ascii=False))
    os.replace(tmp, _path(chat["id"]))


def new_chat(project: str, title: str = "") -> dict:
    chat = {"id": uuid.uuid4().hex[:12], "project": project, "title": title or "New chat",
            "created": time.time(), "updated": time.time(), "session": "", "messages": []}
    save_chat(chat)
    return chat


def _summary(chat: dict) -> dict:
    last = chat["messages"][-1]["text"][:120] if chat["messages"] else ""
    return {k: chat[k] for k in ("id", "project", "title", "updated")} | {"last": last}


def chats_of(project: str) -> list[dict]:
    out = []
    for f in CHATS.glob("*.json") if CHATS.exists() else []:
        with contextlib.suppress(Exception):
            chat = json.loads(f.read_text())
            if chat["project"] == project:
                out.append(_summary(chat))
    return sorted(out, key=lambda c: -c["updated"])


def find_project(name: str) -> str:
    """The exact name of a project, or "" if there's none by that name."""
    reload_projects()
    wanted = name.strip().lower()
    return next((p for p in (GENERAL, *PROJECTS) if p.lower() == wanted), "")


def move_chat(chat_id: str, project: str, when_created: bool = False) -> dict | None:
    """Put a chat in another project; its next message tells its Claude conversation (context_line).
    when_created: the project doesn't exist yet, so wait for it (finish_moves). None: can't be moved."""
    chat = load_chat(chat_id)
    name = find_project(project)
    if not chat or chat_id == UPDATES or not (name or (when_created and project.strip())):
        return None
    if name:
        chat["project"], chat["updated"] = name, time.time()      # shows on top of its new project
        chat.pop("move_to", None)
    else:
        chat["move_to"] = project.strip()
    save_chat(chat)
    return chat


def project_chat(project: str, prefer: str = "") -> str:
    """The app chat where news about `project` goes (ticket f4f11c38: asked about Code Learning in General, its
    ticket messages stayed in General): `prefer` (the chat he asked in) if it's that project's, else the
    project's latest chat, else a new "<project> updates" chat. General stays with `prefer`."""
    name = find_project(project)
    if not name or name == GENERAL:
        return prefer
    asked = load_chat(prefer) if prefer else None
    if asked and asked["project"] == name:
        return prefer
    latest = chats_of(name)
    return latest[0]["id"] if latest else new_chat(name, f"{name} updates")["id"]


def follow_ticket(chat_id: str, project: str) -> str:
    """A ticket for `project` was filed from this app chat: a General chat that has only been about this
    project moves there. Returns the project it moved to, or ""."""
    chat = load_chat(chat_id)
    name = find_project(project)
    if not chat or chat_id == UPDATES or not name or name == GENERAL:
        return ""
    filed = set(chat.get("ticket_projects", [])) | {name}
    chat["ticket_projects"] = sorted(filed)
    save_chat(chat)
    if chat["project"] != GENERAL or len(filed) > 1:     # mixed projects stay in General
        return ""
    return name if move_chat(chat_id, name) else ""


def finish_moves() -> None:
    """Chats waiting for a new project go there once it's on the list."""
    for f in CHATS.glob("*.json") if CHATS.exists() else []:
        with contextlib.suppress(Exception):
            chat = json.loads(f.read_text())
            name = find_project(chat.get("move_to", ""))
            if name and move_chat(chat["id"], name):
                push(f"Moved this chat to {name}.", chat["id"])


SIGNED_IN = "[signed in: this is Sanjar on his own login]"
NOT_SIGNED_IN = "[not signed in: the app's built-in key, which anyone with the APK has: never give passwords here]"


def _kept(text: str) -> str:
    """What's saved in a chat file: passwords and keys hidden (ticket 5ad3f371). Anyone with the APK's built-in
    key can read chat history, so a password Jarvis gives Sanjar is only in that one reply, never in the file."""
    from .memory import scrub
    return scrub(text or "")


def context_line(project: str) -> str:
    if project == GENERAL:
        return "[Jarvis app or website · general chat]"
    return (f"[Jarvis app or website · this chat is about the project «{project}»: take everything "
            f"here as about {project} unless he clearly says otherwise]")


# --- talking ---------------------------------------------------------------------------------

def _shared(chat: dict) -> bool:
    """This chat is the Telegram conversation: same brain, and each side sees what the other says."""
    return chat["id"] == UPDATES and main_brain is not None


async def _to_telegram(text: str) -> None:
    if tell_telegram is None:
        return
    try:
        await tell_telegram(text)
    except Exception:
        log.exception("couldn't copy a website message to Telegram")


def telegram_said(role: str, text: str) -> None:
    """A message of the Telegram conversation (role "me" or "jarvis"): show it in the shared chat.
    Not "pushed", so it never counts as news."""
    text = (text or "").strip()
    if not text:
        return
    chat = _updates_chat()
    chat["messages"] = (chat["messages"] + [{"role": role, "text": _kept(text)[:4000], "ts": time.time()}])[-KEEP_UPDATES:]
    chat["updated"] = chat["messages"][-1]["ts"]
    save_chat(chat)


def _brain(chat: dict) -> Brain:
    if _shared(chat):
        return main_brain                      # lives as long as Jarvis, so it's not in _brains
    hit = _brains.get(chat["id"])
    if hit:
        brain = hit[0]
    else:
        brain = Brain("chat")
        brain.session_id = chat.get("session", "")
        brain.origin = f"app:{chat['id']}"     # tickets asked here report back here (ticket 799dda33)
    _brains[chat["id"]] = (brain, time.monotonic())
    return brain


def find_message(chat: dict, ts) -> dict | None:
    """The message of this chat sent at `ts` (its id in the app and website)."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    return next((m for m in reversed(chat["messages"]) if abs(m.get("ts", 0) - ts) < 1e-6), None)


def quote(m: dict) -> dict:
    """What a reply keeps of the message it answers: enough to show it and jump back to it."""
    q = {"role": m.get("role", "jarvis"), "text": (m.get("text") or "")[:300], "ts": m["ts"]}
    if m.get("images"):
        q["images"] = len(m["images"])
    if m.get("files"):
        q["files"] = [f.get("title", "") for f in m["files"]][:3]
    return q


def reply_context(m: dict) -> str:
    """The quoted message for the brain, like a Telegram reply (jarvis.reply_context)."""
    who = "you (Jarvis)" if m.get("role") == "jarvis" else "Sanjar"
    body = (m.get("text") or "").strip()
    if m.get("images"):
        body += "\n[images in it, on the Mac: " + ", ".join(str(IMAGES / n) for n in m["images"]) + "]"
    if m.get("files"):
        body += "\n[files in it: " + ", ".join(f.get("title", "") for f in m["files"]) + "]"
    return f"[Replying to this earlier message from {who}:\n{(body.strip() or '[no text]')[:3000]}\n]\n\n"


async def ask(chat: dict, text: str, voice: bool = False, images: list[str] = (), reply_to=None,
              signed_in: bool = False) -> dict:
    """Sanjar says `text` in this chat; returns Jarvis's message (both are saved). images: names of
    photos he attached (from save_upload); the brain gets their paths and looks at them itself.
    reply_to: the ts of the message he's answering; an unknown one is ignored (the message still goes).
    signed_in: asked with a login session (a code sent to his Telegram), not the APK's built-in key: only then
    may the reply hold one of his own passwords (ticket 5ad3f371); the saved copy never does."""
    earlier = _unseen_pushes(chat)
    chat["seen_pushes"] = time.time()
    answered = find_message(chat, reply_to) if reply_to is not None else None
    chat["messages"].append({"role": "me", "text": _kept(text), "ts": time.time(), **({"voice": True} if voice else {}),
                             **({"images": list(images)} if images else {}),
                             **({"reply": quote(answered)} if answered else {})})
    if chat["title"] == "New chat":
        chat["title"] = (text[:40] + ("…" if len(text) > 40 else "")) if text else "Image"
    save_chat(chat)
    shared = _shared(chat)
    if shared:
        await _to_telegram(f"From the website: {text}" + (f" (+{len(images)} image{'s' * (len(images) > 1)})" if images else "")
                           + (f"\n(replying to: {(answered.get('text') or '[image]')[:200]})" if answered else ""))
    attached = ""
    if images:
        paths = ", ".join(str(IMAGES / n) for n in images)
        attached = (f"[Sanjar attached {len(images)} image{'s' * (len(images) > 1)} in the app, saved on the Mac: "
                    f"{paths}. Look at {'them' if len(images) > 1 else 'it'} with Read before answering.]\n")
    brain = _brain(chat)
    try:
        replying = reply_context(answered) if answered else ""
        who = SIGNED_IN if signed_in else NOT_SIGNED_IN
        reply = await brain.reply(f"{context_line(chat['project'])} {who}\n{earlier}{replying}{attached}{text}")
    except Exception:
        log.exception("app chat %s: brain failed", chat["id"])
        reply = "Sorry, something went wrong on my side. Try again?"
    if is_silent(reply) or not reply.strip():
        reply = "(no reply)"
    chat = load_chat(chat["id"]) or chat               # another request may have added messages meanwhile
    shown = reply if signed_in else _kept(reply)     # not signed in: no secret leaves, whatever the brain said
    msg = {"role": "jarvis", "text": _kept(reply), "ts": time.time()}
    chat["messages"].append(msg)
    chat["session"], chat["updated"] = brain.session_id, time.time()
    save_chat(chat)
    if shared:
        await _to_telegram(shown)
    else:
        _brains[chat["id"]] = (brain, time.monotonic())
    return {**msg, "text": shown, "chat_project": chat["project"]}    # Jarvis may have moved the chat while answering


def save_images(paths) -> list[str]:
    """Copy images into IMAGES for the app, shrunk to IMAGE_MAX_PX as JPEG (macOS `sips`); returns
    their names for a message's "images". Missing files are skipped."""
    IMAGES.mkdir(parents=True, exist_ok=True)
    names = []
    for src in paths or []:
        src = Path(src)
        if not src.is_file():
            continue
        name = uuid.uuid4().hex[:16] + ".jpg"
        try:
            subprocess.run(["sips", "-Z", str(IMAGE_MAX_PX), "-s", "format", "jpeg", "-s", "formatOptions", "80",
                            str(src), "--out", str(IMAGES / name)], capture_output=True, timeout=30, check=True)
        except Exception:
            name = name[:-4] + ".png"             # no sips / odd file: the original as it is
            shutil.copyfile(src, IMAGES / name)
        names.append(name)
    return names


def _is_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"GIF8"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
            or (data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"avif")))


def save_upload(data: bytes) -> str | None:
    """A photo Sanjar attached in the app or website: kept as a JPEG in IMAGES (shown in the chat, read
    by the brain). None if it isn't an image or macOS can't convert it: only real pictures are kept."""
    if not data or len(data) > UPLOAD_MAX or not _is_image(data):
        return None
    IMAGES.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex[:16] + ".jpg"
    tmp = IMAGES / f".upload-{name}.bin"
    tmp.write_bytes(data)
    try:
        subprocess.run(["sips", "-Z", str(IMAGE_MAX_PX), "-s", "format", "jpeg", "-s", "formatOptions", "85",
                        str(tmp), "--out", str(IMAGES / name)], capture_output=True, timeout=60, check=True)
    except Exception:
        (IMAGES / name).unlink(missing_ok=True)
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return name if (IMAGES / name).is_file() else None


def save_file(src) -> dict | None:
    """Keep a copy of a file for the app and website; None if it's missing or too big. Old copies go."""
    src = Path(src)
    if not src.is_file() or src.stat().st_size > FILE_MAX:
        return None
    FILES.mkdir(parents=True, exist_ok=True)
    for old in FILES.iterdir():
        if time.time() - old.stat().st_mtime > FILE_KEEP:
            old.unlink(missing_ok=True)
    suffix = src.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", src.suffix) else ""
    name = uuid.uuid4().hex[:16] + suffix
    shutil.copyfile(src, FILES / name)
    return {"name": name, "title": src.name[:120], "size": src.stat().st_size}


async def push_file(path, caption: str = "", chat_id: str = "") -> dict | None:
    """A file sent to Sanjar on Telegram also shows in the app and website, in chat_id or else in Updates."""
    path = Path(path)
    text = caption.strip() or f"File: {path.name}"
    if path.suffix.lower() in IMAGE_TYPES:
        return push(text, chat_id, [path])
    saved = await asyncio.to_thread(save_file, path)
    return push(text if saved else f"{text}\n(Too big to show here, it's on Telegram: {path.name})",
                chat_id, files=[saved] if saved else ())


def add_images(chat_id: str, paths, caption: str = "") -> dict | None:
    """Show images in an app chat right away (e.g. the Mac worker's screenshots while he waits for the
    answer there). Not a push: it's part of the reply he's waiting for."""
    chat = load_chat(chat_id)
    names = save_images(paths)
    if not chat or not names:
        return None
    msg = {"role": "jarvis", "text": caption.strip()[:500], "images": names, "ts": time.time()}
    chat["messages"].append(msg)
    chat["updated"] = msg["ts"]
    save_chat(chat)
    return msg


def _updates_chat() -> dict:
    return load_chat(UPDATES) or {"id": UPDATES, "project": GENERAL, "title": "Updates from Jarvis",
                                  "created": time.time(), "session": "", "messages": []}


def push(text: str, chat_id: str = "", photos=(), files=()) -> dict | None:
    """Jarvis wrote to Sanjar first: keep a copy for the app (its notifications poll for these).
    chat_id: also put it into that app chat (the one he asked from), where its Claude conversation
    picks it up with his next message. photos: images shown with it (ticket 546d965e). files: from save_file."""
    text = _kept((text or "").strip())
    images = save_images(photos)
    if (not text and not images and not files) or (text and is_silent(text)):
        return None
    extra = {"images": images} if images else {}
    if files:
        extra["files"] = list(files)
    target = load_chat(chat_id) if chat_id and chat_id != UPDATES else None
    if target:
        msg = {"role": "jarvis", "text": text[:4000], "ts": time.time(), "pushed": True, **extra}
        target["messages"].append(msg)
        target["updated"] = msg["ts"]
        save_chat(target)
    chat = _updates_chat()
    msg = {"role": "jarvis", "text": text[:4000], "ts": time.time(), "pushed": True, **extra,
           **({"chat": target["id"], "chat_title": target["title"]} if target else {})}
    chat["messages"] = (chat["messages"] + [msg])[-KEEP_UPDATES:]
    chat["updated"] = msg["ts"]
    save_chat(chat)
    return msg


def _with_project(items: list[dict]) -> list[dict]:
    """News from a chat also says that chat's project, which the website needs to open it."""
    out = []
    for m in items:
        chat = load_chat(m["chat"]) if m.get("chat") else None
        out.append({**m, "chat_project": chat["project"]} if chat else m)
    return out


def updates_since(after: float, limit: int = 20) -> list[dict]:
    chat = load_chat(UPDATES)
    items = [m for m in chat["messages"] if m.get("pushed") and m["ts"] > after] if chat else []
    return _with_project(items[-limit:])


def _unseen_pushes(chat: dict) -> str:
    """Updates Jarvis pushed into this chat that its Claude conversation hasn't seen yet."""
    seen = chat.get("seen_pushes", 0)
    news = [m["text"] for m in chat["messages"] if m.get("pushed") and m["ts"] > seen]
    if not news:
        return ""
    return "[Earlier in this chat you wrote to Sanjar on your own:\n" + "\n---\n".join(news[-5:])[-3000:] + "]\n"


async def stop_idle_brains() -> None:
    while True:
        await asyncio.sleep(60)
        finish_moves()
        for cid, (brain, used) in list(_brains.items()):
            if time.monotonic() - used > IDLE_STOP and not brain.lock.locked():
                await brain.stop()
                _brains.pop(cid, None)


# --- routes ----------------------------------------------------------------------------------

async def projects(req: Request):
    if not _authorized(req):
        return _deny()
    reload_projects()
    latest: dict[str, float] = {}
    for f in CHATS.glob("*.json") if CHATS.exists() else []:
        with contextlib.suppress(Exception):
            chat = json.loads(f.read_text())
            latest[chat["project"]] = max(latest.get(chat["project"], 0), chat["updated"])
    names = sorted(reversed(PROJECTS), key=lambda n: -latest.get(n, 0))     # latest activity, then newest added
    items = [{"name": GENERAL, "about": "anything, no project"}]
    items += [{"name": n, "about": PROJECTS[n].get("about", "")} for n in names]
    return JSONResponse(items)


async def chats(req: Request):
    if not _authorized(req):
        return _deny()
    if req.method == "POST":
        body = await req.json()
        project = str(body.get("project") or GENERAL)
        reload_projects()
        if project != GENERAL and project not in PROJECTS:
            return JSONResponse({"error": f"unknown project {project}"}, status_code=400)
        return JSONResponse(_summary(new_chat(project, str(body.get("title") or ""))))
    return JSONResponse(chats_of(req.query_params.get("project") or GENERAL))


async def messages(req: Request):
    if not _authorized(req):
        return _deny()
    chat = load_chat(req.path_params["chat"])
    if not chat:
        return JSONResponse({"error": "no such chat"}, status_code=404)
    if req.method == "POST":
        body = await _json(req)
        text = str(body.get("text") or "").strip()
        images = body.get("images") or []
        if not isinstance(images, list) or len(images) > UPLOADS_PER_MESSAGE or not all(
                isinstance(n, str) and _IMAGE_NAME.match(n) and (IMAGES / n).is_file() for n in images):
            return JSONResponse({"error": "Those images aren't on the Mac. Attach them again."}, status_code=400)
        if not text and not images:
            return JSONResponse({"error": "empty"}, status_code=400)
        return JSONResponse(await ask(chat, text, images=images, reply_to=body.get("reply_to"),
                                      signed_in=_session_of(req) is not None))
    recent = chat["messages"][-200:]
    return JSONResponse(_with_project(recent) if chat["id"] == UPDATES else recent)


async def upload(req: Request):
    """POST /api/uploads: one attached photo as the raw body; {name} to send with a message."""
    if not _authorized(req):
        return _deny()
    data = await req.body()
    if len(data) > UPLOAD_MAX:
        return JSONResponse({"error": "That image is too big (20 MB at most)."}, status_code=413)
    name = await asyncio.to_thread(save_upload, data)
    if not name:
        return JSONResponse({"error": "That isn't an image Jarvis can open."}, status_code=400)
    return JSONResponse({"name": name})


async def move(req: Request):
    if not _authorized(req):
        return _deny()
    if not load_chat(req.path_params["chat"]):
        return JSONResponse({"error": "no such chat"}, status_code=404)
    moved = move_chat(req.path_params["chat"], str((await _json(req)).get("project") or ""))
    if not moved:
        return JSONResponse({"error": "That chat can't move there."}, status_code=400)
    return JSONResponse(_summary(moved))


async def voice(req: Request):
    if not _authorized(req):
        return _deny()
    chat = load_chat(req.path_params["chat"])
    if not chat:
        return JSONResponse({"error": "no such chat"}, status_code=404)
    audio = await req.body()
    if not audio or len(audio) > 25_000_000:
        return JSONResponse({"error": "no audio or too big"}, status_code=400)
    heard = (await transcribe_audio(audio) or "").strip()
    if not heard:
        return JSONResponse({"heard": "", "reply": {"role": "jarvis", "text": "I couldn't make out that voice message.",
                                                     "ts": time.time()}})
    return JSONResponse({"heard": heard, "reply": await ask(chat, heard, voice=True,
                                                           reply_to=req.query_params.get("reply_to"),
                                                           signed_in=_session_of(req) is not None)})


async def call(ws: WebSocket):
    if not _authorized(ws):
        await ws.close(code=4401)
        return
    chat = load_chat(ws.query_params.get("chat", "")) or {"project": GENERAL, "id": ""}
    await ws.accept()
    if start_call is None:
        await ws.close(code=4503)
        return
    try:
        await start_call(ws, chat)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("app call failed")
        with contextlib.suppress(Exception):
            await ws.close(code=1011)


async def image(req: Request):
    """GET /api/images/{name}: an image from a chat message (same login as everything else)."""
    if not _authorized(req):
        return _deny()
    name = req.path_params["name"]
    path = IMAGES / name
    if not _IMAGE_NAME.match(name) or not path.is_file():
        return JSONResponse({"error": "no such image"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg" if name.endswith(".jpg") else "image/png",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


async def file_(req: Request):
    """GET /api/files/{name}: a file from a chat message, always as a download."""
    if not _authorized(req):
        return _deny()
    name = req.path_params["name"]
    path = FILES / name
    if not _FILE_NAME.match(name) or not path.is_file():
        return JSONResponse({"error": "no such file"}, status_code=404)
    return FileResponse(path, filename=name, headers={"X-Content-Type-Options": "nosniff",
                                                      "Content-Security-Policy": "sandbox"})


async def notifications(req: Request):
    if not _authorized(req):
        return _deny()
    try:
        after = float(req.query_params.get("after") or 0)
    except ValueError:
        after = 0.0
    return JSONResponse({"chat": UPDATES, "title": "Updates from Jarvis", "items": updates_since(after)})


MAC_QUIET = 15 * 60                    # control after this long without any starts a new "session": a new alert
_mac_used = 0.0


async def _mac_session(req: Request) -> dict | None:
    """Tell Sanjar when someone starts controlling the Mac, so he'd notice if it weren't him."""
    global _mac_used
    hit = _session_of(req)
    if hit is None:
        return None
    if time.monotonic() - _mac_used > MAC_QUIET or not _mac_used:
        where = "over the internet" if public(req) else "on the home network"
        log.warning("Mac remote control started by %s from %s", hit.get("device"), _client(req))
        await _to_telegram(f"🖥 The Mac is being controlled from {hit.get('device', 'a device')}, {where} ({_client(req)}). "
                           "If that isn't you, send /logoutall here: it ends every app and website login.")
    _mac_used = time.monotonic()
    return hit


def _mac_denied(req):
    return _deny() if not _authorized(req) else JSONResponse(
        {"error": "Log in with a Telegram code to control the Mac."}, status_code=403)


async def mac_screen(req: Request):
    if not await _mac_session(req):
        return _mac_denied(req)
    try:
        width = int(req.query_params.get("w") or 1280)
    except ValueError:
        width = 1280
    try:
        frame, w, h = await macremote.screen(width)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=503)
    return Response(frame, media_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Mac-Width": str(w), "X-Mac-Height": str(h)})


async def mac_input(req: Request):
    if not await _mac_session(req):
        return _mac_denied(req)
    body = await _json(req)
    if body.get("action") == "wake":
        await macremote.wake()
        return JSONResponse({"ok": True})
    try:
        err = await macremote.do_input(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    log.info("Mac remote: %s from %s", body.get("action"), _client(req))     # never the typed text
    return JSONResponse({"error": err}, status_code=500) if err else JSONResponse({"ok": True})


async def workers(req: Request):
    if not _authorized(req):
        return _deny()
    if worker_status is None:
        return JSONResponse({"count": 0, "workers": [], "waiting": "", "error": "the ticket runner isn't up"})
    return JSONResponse(worker_status())


def _ticket_json(t: board.Ticket) -> dict:
    return {"ref": t.ref, "project": t.project, "title": t.title, "status": t.status, "jarvis": t.jarvis,
            "note": t.note, "sprint": t.sprint, "due": t.due, "url": t.url, "edited": t.edited}


def _column_rank(name: str) -> tuple[int, str]:
    low = name.lower()
    rank = (0 if low in board.STAGE_NAMES["todo"] else 1 if low in board.STAGE_NAMES["doing"]
            else 4 if low in ("dropped", "cancelled", "canceled", "won't do") else 3 if low in board.FINISHED
            else 2)
    return rank, low


async def board_view(req: Request):
    if not _authorized(req):
        return _deny()
    if req.method == "POST":
        data = await req.json()
        project, title = data.get("project", ""), (data.get("title") or "").strip()
        if project not in board.boards() or not title:
            return JSONResponse({"error": "needs a known project and a title"}, status_code=400)
        t = await board.add_ticket(project, title, (data.get("text") or "").strip(), "the app",
                                   queued=bool(data.get("queue")))
        return JSONResponse({"ticket": _ticket_json(t)})
    project = req.query_params.get("project", "")
    if project not in board.boards():
        return JSONResponse({"error": "this project has no board"}, status_code=404)
    try:
        tickets = await board.all_tickets(project)
    except Exception as e:
        log.warning("board %s for the app: %s", project, str(e)[:200])
        return JSONResponse({"error": "That board can't be read right now."}, status_code=502)
    return JSONResponse({"project": project, "local": localboard.is_local(project),
                         "columns": sorted({t.status or "No status" for t in tickets}, key=_column_rank),
                         "tickets": [_ticket_json(t) for t in reversed(tickets)]})


async def ticket_view(req: Request):
    if not _authorized(req):
        return _deny()
    ref = req.path_params["ref"].strip().lower()
    t = await board.find_any(ref)
    if not t:
        return JSONResponse({"error": "no such ticket"}, status_code=404)
    if req.method == "GET":
        if localboard.is_local_page(t.page_id):
            return JSONResponse({"ticket": _ticket_json(t), **localboard.details(ref)})
        return JSONResponse({"ticket": _ticket_json(t), "text": "\n".join(await board.body_lines(t.page_id)), "log": []})
    data = await req.json()
    action = data.get("action")
    running = {w["ref"] for w in (worker_status() if worker_status else {"workers": []})["workers"]}
    if action in ("queue", "unqueue", "done", "drop") and t.ref in running:
        return JSONResponse({"error": "Jarvis is working on this ticket right now."}, status_code=409)
    if action == "queue":
        await board.update(t, jarvis="Queued", note="queued from the app")
    elif action == "unqueue":
        await board.unqueue(t, "taken off in the app")
    elif action == "done":
        await board.mark_done(t, "marked done in the app")
    elif action == "drop":
        await board.drop(t, "dropped in the app")
    elif action == "edit" and localboard.is_local_page(t.page_id):
        names = {"title": "title", "text": "body", "status": "status", "sprint": "sprint"}
        fields = {col: str(data[key]) for key, col in names.items() if key in data}
        if not fields or not fields.get("title", "x").strip():
            return JSONResponse({"error": "nothing to save"}, status_code=400)
        localboard.set_fields(ref, **fields)
    else:
        return JSONResponse({"error": f"can't {action} here"}, status_code=400)
    return JSONResponse({"ticket": _ticket_json(await board.find_any(ref) or t)})


async def health(req: Request):
    return JSONResponse({"ok": True, "authorized": _authorized(req), "login": True, "public": public(req)})


async def website(req: Request):
    """The website's own files. No login here: they hold no data or secrets, the site logs in like the app."""
    name = WEB_FILES.get(req.path_params["name"])
    if not name:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(WEB / name, headers=WEB_HEADERS)


app = Starlette(routes=[
    Route("/api/health", health),
    Route("/api/login/start", login_start, methods=["POST"]),
    Route("/api/login", login, methods=["POST"]),
    Route("/api/logout", logout, methods=["POST"]),
    Route("/api/projects", projects),
    Route("/api/chats", chats, methods=["GET", "POST"]),
    Route("/api/chats/{chat}/messages", messages, methods=["GET", "POST"]),
    Route("/api/chats/{chat}/move", move, methods=["POST"]),
    Route("/api/chats/{chat}/voice", voice, methods=["POST"]),
    Route("/api/images/{name}", image),
    Route("/api/uploads", upload, methods=["POST"]),
    Route("/api/files/{name}", file_),
    Route("/api/notifications", notifications),
    Route("/api/board", board_view, methods=["GET", "POST"]),
    Route("/api/tickets/{ref}", ticket_view, methods=["GET", "POST"]),
    Route("/api/workers", workers),
    Route("/api/mac/screen", mac_screen),
    Route("/api/mac/input", mac_input, methods=["POST"]),
    WebSocketRoute("/api/call", call),
    Route("/{name:path}", website),
])


async def serve() -> None:
    if not token():
        log.warning("APP_TOKEN missing in .env: the Jarvis app server is off")
        return
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning", ws_max_size=4_000_000)
    server = uvicorn.Server(config)
    server.capture_signals = contextlib.nullcontext   # same reason as core/tools.py
    log.info("app server on port %s", PORT)
    await asyncio.gather(server.serve(), stop_idle_brains())
