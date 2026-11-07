"""Jarvis's own Google sign-in, for any of Sanjar's Google accounts (ticket c270b9ef), so it can edit the
inside of his Docs and Slides, which the Claude Google Drive connector (core/drive.py) can't.

Sign-in: google_sign_in gives a Google link (OAuth with PKCE, the OAuth client of ~/dev/server/gdrive_uploader,
GOOGLE_OAUTH_CLIENT_ID / _SECRET in .env). He signs in and presses Allow. Google then sends the browser to
http://127.0.0.1:8791/?code=...:
  - on the Mac, Jarvis's small callback server below catches it and finishes by itself
  - on his phone, that page can't load: he copies its address and sends it, and finish() takes it from there
Each account's refresh token is kept in var/state/google_tokens.json (never in git), keyed by its email.

Then: list files, read a file's text, replace text in a Doc or Slides deck, add text at the end of a Doc.
Google's own error text (an API not enabled, no access, ...) is passed on as it is: it says what to do.
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import STATE

log = logging.getLogger("jarvis.google")
PORT = 8791
REDIRECT = f"http://127.0.0.1:{PORT}/"
SCOPES = ("openid email https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/documents "
          "https://www.googleapis.com/auth/presentations")
TOKENS = STATE / "google_tokens.json"     # email -> {"refresh_token", "access_token", "expires"}
PENDING = STATE / "google_pending.json"   # state -> {"verifier", "at", "hint"}
PENDING_FOR = 30 * 60
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/drive/v3"
MIME = {"application/vnd.google-apps.document": "doc", "application/vnd.google-apps.presentation": "slides",
        "application/vnd.google-apps.spreadsheet": "sheet"}
_http = httpx.AsyncClient(timeout=30)


class GoogleError(Exception):
    pass


def _load(path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _client() -> tuple[str, str]:
    cid, secret = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""), os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not cid or not secret:
        raise GoogleError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are missing in Jarvis's .env")
    return cid, secret


def accounts() -> list[str]:
    return sorted(_load(TOKENS))


# --- signing in ------------------------------------------------------------------------------

def sign_in_link(hint: str = "") -> str:
    """A Google sign-in link for one account; hint: its email, to pre-fill it."""
    cid, _ = _client()
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    pending = {k: v for k, v in _load(PENDING).items() if time.time() - v["at"] < PENDING_FOR}
    pending[state] = {"verifier": verifier, "at": time.time(), "hint": hint}
    _save(PENDING, pending)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline",
         "prompt": "consent select_account", "include_granted_scopes": "true"}
    if hint:
        q["login_hint"] = hint
    return f"{AUTH}?{urlencode(q)}"


def _email_from(id_token: str) -> str:
    """The account's email from the id_token Google just sent us over TLS."""
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")


async def finish(address: str) -> str:
    """Finish a sign-in with the address Google sent the browser to (or just its code). Returns a sentence."""
    q = parse_qs(urlparse(address.strip()).query) if "code=" in address or "error=" in address else {}
    if q.get("error"):
        return f"Google said: {q['error'][0]}. Nothing was connected; try the sign-in link again."
    code = (q.get("code") or [address.strip()])[0]
    pending = _load(PENDING)
    state = (q.get("state") or [""])[0]
    if state not in pending:                          # just a code pasted: the newest sign-in waiting
        state = max(pending, key=lambda k: pending[k]["at"], default="")
    if not state or not code or time.time() - pending[state]["at"] > PENDING_FOR:
        return "That sign-in has expired or wasn't started here. Ask me for a new Google sign-in link."
    cid, secret = _client()
    r = await _http.post(TOKEN, data={"client_id": cid, "client_secret": secret, "code": code, "grant_type":
                                      "authorization_code", "redirect_uri": REDIRECT,
                                      "code_verifier": pending[state]["verifier"]})
    got = r.json()
    if r.status_code != 200 or "refresh_token" not in got:
        return f"Google didn't accept it ({got.get('error_description') or got.get('error') or r.status_code}). Try a new link."
    email = _email_from(got.get("id_token", "")) or pending[state].get("hint") or "unknown"
    tokens = _load(TOKENS)
    tokens[email] = {"refresh_token": got["refresh_token"], "access_token": got["access_token"],
                     "expires": time.time() + got.get("expires_in", 3600) - 60}
    _save(TOKENS, tokens)
    pending.pop(state, None)
    _save(PENDING, pending)
    log.info("google: signed in %s", email)
    return f"Signed in to Google as {email}."


async def serve_callback() -> None:
    """Catch Google's redirect on this Mac (sign-in done in the Mac's browser)."""
    async def handle(reader, writer):
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode(errors="replace")
            path = line.split(" ")[1] if " " in line else "/"
            text = await finish(REDIRECT.rstrip("/") + path) if "code=" in path or "error=" in path else "Waiting for Google."
        except Exception as e:
            log.exception("google callback failed")
            text = f"Something went wrong: {e}"
        body = f"<html><body style='font:20px sans-serif;padding:40px'>{text}<br><br>You can close this tab.</body></html>"
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                     f"{len(body.encode())}\r\nConnection: close\r\n\r\n{body}".encode())
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()
    try:
        server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    except OSError as e:                  # sign-ins still work by pasting the address back
        log.warning("google sign-in callback can't listen on %s: %s", PORT, e)
        return
    log.info("google sign-in callback on %s", REDIRECT)
    async with server:
        await server.serve_forever()


# --- using it --------------------------------------------------------------------------------

def which(account: str = "") -> str:
    """The signed-in account meant: an email (or part of it), or the only one there is."""
    have = accounts()
    if not have:
        raise GoogleError("No Google account is signed in yet. Use google_sign_in to get him a sign-in link.")
    if not account:
        if len(have) == 1:
            return have[0]
        raise GoogleError(f"Which account? Signed in: {', '.join(have)}")
    match = [a for a in have if account.lower() in a.lower()]
    if len(match) != 1:
        raise GoogleError(f"{account!r} isn't signed in. Signed in: {', '.join(have)}. A new one: google_sign_in.")
    return match[0]


async def access_token(email: str) -> str:
    tokens = _load(TOKENS)
    t = tokens[email]
    if t.get("access_token") and time.time() < t.get("expires", 0):
        return t["access_token"]
    cid, secret = _client()
    r = await _http.post(TOKEN, data={"client_id": cid, "client_secret": secret, "grant_type": "refresh_token",
                                      "refresh_token": t["refresh_token"]})
    got = r.json()
    if r.status_code != 200:
        if got.get("error") == "invalid_grant":
            tokens.pop(email)
            _save(TOKENS, tokens)
            raise GoogleError(f"Google ended the sign-in for {email}. He needs to sign in again (google_sign_in).")
        raise GoogleError(f"Google refused the token: {got.get('error_description') or got.get('error')}")
    t.update(access_token=got["access_token"], expires=time.time() + got.get("expires_in", 3600) - 60)
    _save(TOKENS, tokens)
    return t["access_token"]


async def api(email: str, method: str, url: str, **kw):
    r = await _http.request(method, url, headers={"Authorization": f"Bearer {await access_token(email)}"}, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = r.text[:300]
        raise GoogleError(f"Google ({r.status_code}): {msg}")
    return r.json() if "json" in r.headers.get("content-type", "") else r.text


def file_id(ref: str) -> str:
    """A file id from a Google link or the id itself."""
    m = re.search(r"/d/([\w-]{20,})", ref) or re.search(r"[?&]id=([\w-]{20,})", ref)
    return m.group(1) if m else ref.strip()


async def list_files(email: str, search: str = "", n: int = 10) -> list[dict]:
    q = "trashed = false"
    if search:
        s = search.replace("\\", "\\\\").replace("'", "\\'")
        q += f" and (name contains '{s}' or fullText contains '{s}')"
    got = await api(email, "GET", f"{DRIVE}/files", params={
        "q": q, "pageSize": n, "orderBy": "modifiedTime desc", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink)"})
    return got.get("files", [])


async def kind(email: str, fid: str) -> tuple[str, str]:
    meta = await api(email, "GET", f"{DRIVE}/files/{fid}", params={"fields": "name,mimeType", "supportsAllDrives": "true"})
    return MIME.get(meta["mimeType"], meta["mimeType"]), meta["name"]


async def read_text(email: str, fid: str, limit: int = 20000) -> str:
    what, name = await kind(email, fid)
    as_ = "text/csv" if what == "sheet" else "text/plain"
    if what not in MIME.values():
        raise GoogleError(f"{name} isn't a Google Doc, Sheet or Slides file")
    text = await api(email, "GET", f"{DRIVE}/files/{fid}/export", params={"mimeType": as_})
    return f"{name}:\n{str(text)[:limit]}"


async def replace_text(email: str, fid: str, find: str, replace: str) -> str:
    what, name = await kind(email, fid)
    req = {"replaceAllText": {"containsText": {"text": find, "matchCase": True}, "replaceText": replace}}
    if what == "doc":
        got = await api(email, "POST", f"https://docs.googleapis.com/v1/documents/{fid}:batchUpdate", json={"requests": [req]})
    elif what == "slides":
        got = await api(email, "POST", f"https://slides.googleapis.com/v1/presentations/{fid}:batchUpdate",
                        json={"requests": [req]})
    else:
        raise GoogleError(f"{name} isn't a Doc or Slides deck")
    n = ((got.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged", 0)
    return f"Replaced {n} time(s) in {name}." if n else f"{find!r} wasn't found in {name}; nothing changed."


async def add_text(email: str, fid: str, text: str) -> str:
    what, name = await kind(email, fid)
    if what != "doc":
        raise GoogleError(f"{name} isn't a Google Doc")
    await api(email, "POST", f"https://docs.googleapis.com/v1/documents/{fid}:batchUpdate",
              json={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "\n" + text}}]})
    return f"Added the text at the end of {name}."
