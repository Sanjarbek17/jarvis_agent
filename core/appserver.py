"""The Jarvis Android app's server (ticket 27099a8a): projects, chats per project, text and voice
messages, and live calls, for Sanjar's phone on the home Wi-Fi.

Every request needs the app token (APP_TOKEN in .env): header "Authorization: Bearer <token>", or
?token=<token> on the call WebSocket.

    GET  /api/projects                       [{name, about}] ("General" first)
    GET  /api/chats?project=X                that project's chats, newest first
    POST /api/chats {project, title?}        a new chat
    GET  /api/chats/{id}/messages            [{role: "me"|"jarvis", text, ts, voice?}]
    POST /api/chats/{id}/messages {text}     Jarvis's reply (waits for it)
    POST /api/chats/{id}/voice  (audio body) {heard, reply}: transcribed like a Telegram voice note
    GET  /api/notifications?after=<ts>       {chat, items}: what Jarvis wrote first (ticket news, "needs
                                             you", the daily summary) since ts; the phone polls this
    GET  /api/workers                        {count, workers: [{project, ref, title, url, started, step,
                                             active}], waiting}: the ticket workers running right now
    WS   /api/call?token=&chat=              a live call: phone sends 48 kHz mono s16 PCM, gets
                                             24 kHz mono s16 PCM back; text frames are transcript
                                             lines {"who", "text"}

Each chat is its own Claude conversation (resumed by session id, stopped when idle), told which
project it is about, so Sanjar doesn't have to say it. Chats live in var/state/app_chats/.

Whatever Jarvis tells Sanjar on its own (push()) also lands in the "Updates from Jarvis" chat (ticket
e028892d); the app polls /api/notifications and shows a phone notification, no Google services needed.
"""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
import uuid
from typing import Awaitable, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .brain import Brain, is_silent
from .config import PROJECTS, STATE, reload_projects
from .voice import transcribe_audio

log = logging.getLogger("jarvis.app")
PORT = 8790
CHATS = STATE / "app_chats"
IDLE_STOP = 15 * 60                    # stop a chat's Claude process after this long unused
GENERAL = "General"
UPDATES = "updates"                    # chat id of "Updates from Jarvis"
KEEP_UPDATES = 300

_brains: dict[str, tuple[Brain, float]] = {}   # chat id -> (brain, last used)
start_call: Callable[[WebSocket, dict], Awaitable[None]] | None = None   # set by jarvis.py
worker_status: Callable[[], dict] | None = None                          # set by jarvis.py (runner.snapshot)


def token() -> str:
    return os.environ.get("APP_TOKEN", "")


def _authorized(req) -> bool:
    want = token()
    got = req.headers.get("authorization", "").removeprefix("Bearer ").strip() or req.query_params.get("token", "")
    return bool(want) and secrets.compare_digest(got, want)


def _deny():
    return JSONResponse({"error": "unauthorized"}, status_code=401)


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
        return "[Jarvis app on Sanjar's phone · general chat]"
    return (f"[Jarvis app on Sanjar's phone · this chat is about the project «{project}»: take everything "
            f"here as about {project} unless he clearly says otherwise]")


# --- talking ---------------------------------------------------------------------------------

def _brain(chat: dict) -> Brain:
    hit = _brains.get(chat["id"])
    if hit:
        brain = hit[0]
    else:
        brain = Brain("chat")
        brain.session_id = chat.get("session", "")
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


def push(text: str) -> dict | None:
    """Jarvis wrote to Sanjar first: keep a copy for the app (its notifications poll for these)."""
    text = (text or "").strip()
    if not text or is_silent(text):
        return None
    chat = load_chat(UPDATES) or {"id": UPDATES, "project": GENERAL, "title": "Updates from Jarvis",
                                  "created": time.time(), "session": "", "messages": []}
    msg = {"role": "jarvis", "text": text[:4000], "ts": time.time(), "pushed": True}
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
    return JSONResponse({"ok": True, "authorized": _authorized(req)})


app = Starlette(routes=[
    Route("/api/health", health),
    Route("/api/projects", projects),
    Route("/api/chats", chats, methods=["GET", "POST"]),
    Route("/api/chats/{chat}/messages", messages, methods=["GET", "POST"]),
    Route("/api/chats/{chat}/voice", voice, methods=["POST"]),
    Route("/api/notifications", notifications),
    Route("/api/workers", workers),
    WebSocketRoute("/api/call", call),
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
