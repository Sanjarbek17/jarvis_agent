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
    GET  /api/chats/{id}/messages            [{role: "me"|"jarvis", text, ts, voice?}]
    POST /api/chats/{id}/messages {text}     Jarvis's reply (waits for it)
    POST /api/chats/{id}/voice  (audio body) {heard, reply}: transcribed like a Telegram voice note
    GET  /api/images/{name}                  an image named in a message's "images" list (JPEG/PNG)
    GET  /api/notifications?after=<ts>       {chat, items}: what Jarvis wrote first (ticket news, "needs
                                             you", the daily summary) since ts; the phone polls this
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
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .brain import Brain, is_silent
from .config import PROJECTS, STATE, reload_projects
from .voice import transcribe_audio

log = logging.getLogger("jarvis.app")
PORT = 8790
CHATS = STATE / "app_chats"
IMAGES = STATE / "app_images"          # screenshots and photos shown in app chats (ticket 546d965e)
IMAGE_MAX_PX = 1280                    # longest side: plenty for a phone, small over mobile data
_IMAGE_NAME = re.compile(r"^[0-9a-f]{16}\.(jpg|png)$")
SESSIONS = STATE / "app_sessions.json"
CODE_TTL = 5 * 60                      # a login code works this long
CODE_TRIES = 5                         # wrong guesses before the code is dropped
CODE_GAP = 60                          # at most one code a minute...
CODES_PER_HOUR = 5                     # ...and this many an hour
IDLE_STOP = 15 * 60                    # stop a chat's Claude process after this long unused
GENERAL = "General"
UPDATES = "updates"                    # chat id of "Updates from Jarvis"
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
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}

_brains: dict[str, tuple[Brain, float]] = {}   # chat id -> (brain, last used)
start_call: Callable[[WebSocket, dict], Awaitable[None]] | None = None   # set by jarvis.py
worker_status: Callable[[], dict] | None = None                          # set by jarvis.py (runner.snapshot)
send_code: Callable[[str], Awaitable] | None = None                      # set by jarvis.py: text to Sanjar's Telegram

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


def context_line(project: str) -> str:
    if project == GENERAL:
        return "[Jarvis app or website · general chat]"
    return (f"[Jarvis app or website · this chat is about the project «{project}»: take everything "
            f"here as about {project} unless he clearly says otherwise]")


# --- talking ---------------------------------------------------------------------------------

def _brain(chat: dict) -> Brain:
    hit = _brains.get(chat["id"])
    if hit:
        brain = hit[0]
    else:
        brain = Brain("chat")
        brain.session_id = chat.get("session", "")
        brain.origin = f"app:{chat['id']}"     # tickets asked here report back here (ticket 799dda33)
    _brains[chat["id"]] = (brain, time.monotonic())
    return brain


async def ask(chat: dict, text: str, voice: bool = False) -> dict:
    """Sanjar says `text` in this chat; returns Jarvis's message (both are saved)."""
    earlier = _unseen_pushes(chat)
    chat["seen_pushes"] = time.time()
    chat["messages"].append({"role": "me", "text": text, "ts": time.time(), **({"voice": True} if voice else {})})
    if chat["title"] == "New chat":
        chat["title"] = text[:40] + ("…" if len(text) > 40 else "")
    save_chat(chat)
    brain = _brain(chat)
    try:
        reply = await brain.reply(f"{context_line(chat['project'])}\n{earlier}{text}")
    except Exception:
        log.exception("app chat %s: brain failed", chat["id"])
        reply = "Sorry, something went wrong on my side. Try again?"
    if is_silent(reply) or not reply.strip():
        reply = "(no reply)"
    chat = load_chat(chat["id"]) or chat               # another request may have added messages meanwhile
    msg = {"role": "jarvis", "text": reply, "ts": time.time()}
    chat["messages"].append(msg)
    chat["session"], chat["updated"] = brain.session_id, time.time()
    save_chat(chat)
    _brains[chat["id"]] = (brain, time.monotonic())
    return msg


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


def push(text: str, chat_id: str = "", photos=()) -> dict | None:
    """Jarvis wrote to Sanjar first: keep a copy for the app (its notifications poll for these).
    chat_id: also put it into that app chat (the one he asked from), where its Claude conversation
    picks it up with his next message. photos: images shown with it (ticket 546d965e)."""
    text = (text or "").strip()
    images = save_images(photos)
    if (not text and not images) or (text and is_silent(text)):
        return None
    extra = {"images": images} if images else {}
    target = load_chat(chat_id) if chat_id and chat_id != UPDATES else None
    if target:
        msg = {"role": "jarvis", "text": text[:4000], "ts": time.time(), "pushed": True, **extra}
        target["messages"].append(msg)
        target["updated"] = msg["ts"]
        save_chat(target)
    chat = load_chat(UPDATES) or {"id": UPDATES, "project": GENERAL, "title": "Updates from Jarvis",
                                  "created": time.time(), "session": "", "messages": []}
    msg = {"role": "jarvis", "text": text[:4000], "ts": time.time(), "pushed": True, **extra,
           **({"chat": target["id"], "chat_title": target["title"]} if target else {})}
    chat["messages"] = (chat["messages"] + [msg])[-KEEP_UPDATES:]
    chat["updated"] = msg["ts"]
    save_chat(chat)
    return msg


def updates_since(after: float, limit: int = 20) -> list[dict]:
    chat = load_chat(UPDATES)
    items = [m for m in chat["messages"] if m.get("pushed") and m["ts"] > after] if chat else []
    return items[-limit:]


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
        for cid, (brain, used) in list(_brains.items()):
            if time.monotonic() - used > IDLE_STOP and not brain.lock.locked():
                await brain.stop()
                _brains.pop(cid, None)


# --- routes ----------------------------------------------------------------------------------

async def projects(req: Request):
    if not _authorized(req):
        return _deny()
    reload_projects()
    items = [{"name": GENERAL, "about": "anything, no project"}]
    items += [{"name": n, "about": p.get("about", "")} for n, p in PROJECTS.items()]
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
        text = str((await req.json()).get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        return JSONResponse(await ask(chat, text))
    return JSONResponse(chat["messages"][-200:])


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
    return JSONResponse({"heard": heard, "reply": await ask(chat, heard, voice=True)})


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


async def notifications(req: Request):
    if not _authorized(req):
        return _deny()
    try:
        after = float(req.query_params.get("after") or 0)
    except ValueError:
        after = 0.0
    return JSONResponse({"chat": UPDATES, "title": "Updates from Jarvis", "items": updates_since(after)})


async def workers(req: Request):
    if not _authorized(req):
        return _deny()
    if worker_status is None:
        return JSONResponse({"count": 0, "workers": [], "waiting": "", "error": "the ticket runner isn't up"})
    return JSONResponse(worker_status())


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
    Route("/api/chats/{chat}/voice", voice, methods=["POST"]),
    Route("/api/images/{name}", image),
    Route("/api/notifications", notifications),
    Route("/api/workers", workers),
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
