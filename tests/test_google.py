"""Ticket c270b9ef: Jarvis's own Google sign-in for any of Sanjar's accounts, and editing inside Docs and Slides."""
import asyncio
import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from core import google, tools


def id_token(email):
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class FakeGoogle:
    """Google's token endpoint and APIs, enough to check what Jarvis sends."""
    def __init__(self):
        self.calls, self.refresh_ok, self.files = [], True, {"DOC1" + "x" * 20: "application/vnd.google-apps.document",
                                                              "DECK" + "x" * 20: "application/vnd.google-apps.presentation"}

    def __call__(self, request: httpx.Request):
        url, body = str(request.url), request.content.decode()
        self.calls.append((request.method, url, body))
        if url == google.TOKEN:
            form = parse_qs(body)
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"] and form["redirect_uri"] == [google.REDIRECT]
                return httpx.Response(200, json={"access_token": "A1", "refresh_token": "R1", "expires_in": 3600,
                                                 "id_token": id_token("sanjarbek200217@gmail.com")})
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "A2", "expires_in": 3600})
        assert request.headers["authorization"].startswith("Bearer A")
        if "/drive/v3/files/" in url and "/export" in url:
            return httpx.Response(200, text="Hello class", headers={"content-type": "text/plain"})
        if "/drive/v3/files/" in url:
            fid = url.split("/files/")[1].split("?")[0]
            if fid not in self.files:
                return httpx.Response(404, json={"error": {"message": "File not found"}})
            return httpx.Response(200, json={"name": "Lesson", "mimeType": self.files[fid]})
        if url.startswith(f"{google.DRIVE}/files"):
            return httpx.Response(200, json={"files": [{"id": "DOC1", "name": "Lesson", "mimeType": "application/vnd.google-apps.document",
                                                        "modifiedTime": "2026-11-07T10:00:00Z", "webViewLink": "https://docs.google.com/d/DOC1"}]})
        if url == google.SLIDES and request.method == "POST":
            return httpx.Response(200, json={"presentationId": "P1", "slides": [{"pageElements": [
                {"objectId": "t0", "shape": {"placeholder": {"type": "CENTERED_TITLE"}}},
                {"objectId": "s0", "shape": {"placeholder": {"type": "SUBTITLE"}}}]}]})
        if url == google.DOCS and request.method == "POST":
            return httpx.Response(200, json={"documentId": "D1"})
        if ":batchUpdate" in url:
            req = json.loads(body)["requests"][0]
            if "replaceAllText" in req:
                return httpx.Response(200, json={"replies": [{"replaceAllText": {"occurrencesChanged": 2}}]})
            return httpx.Response(200, json={"replies": [{}]})
        return httpx.Response(500, json={"error": {"message": "unexpected " + url}})


@pytest.fixture
def g(tmp_path, monkeypatch):
    monkeypatch.setattr(google, "TOKENS", tmp_path / "tokens.json")
    monkeypatch.setattr(google, "PENDING", tmp_path / "pending.json")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    fake = FakeGoogle()
    monkeypatch.setattr(google, "_http", httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return fake


def signed_in(g):
    link = google.sign_in_link("sanjarbek200217@gmail.com")
    state = parse_qs(urlparse(link).query)["state"][0]
    return asyncio.run(google.finish(f"http://127.0.0.1:8791/?state={state}&code=C0DE&scope=x"))


def test_link_asks_for_docs_slides_drive_with_pkce(g):
    q = parse_qs(urlparse(google.sign_in_link("a@b.c")).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8791/"] and q["code_challenge_method"] == ["S256"]
    assert {"https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/presentations",
            "https://www.googleapis.com/auth/drive", "email"} <= set(q["scope"][0].split())
    assert q["access_type"] == ["offline"] and q["login_hint"] == ["a@b.c"]


def test_pasting_the_address_from_the_phone_signs_in(g):
    assert signed_in(g) == "Signed in to Google as sanjarbek200217@gmail.com."
    assert google.accounts() == ["sanjarbek200217@gmail.com"]
    assert google.TOKENS.stat().st_mode & 0o077 == 0                          # only Jarvis's user can read it


def test_bad_or_old_addresses_are_refused(g):
    assert "expired or wasn't started" in asyncio.run(google.finish("http://127.0.0.1:8791/?code=X&state=nope"))
    google.sign_in_link()
    assert "Google said: access_denied" in asyncio.run(google.finish("http://127.0.0.1:8791/?error=access_denied"))


def test_list_read_replace_add(g):
    signed_in(g)
    files = asyncio.run(google.list_files("sanjarbek200217@gmail.com", "lesson"))
    assert files[0]["name"] == "Lesson" and "fullText contains 'lesson'" in g.calls[-1][1].replace("+", " ").replace("%27", "'").replace("%20", " ")
    doc, deck = "DOC1" + "x" * 20, "DECK" + "x" * 20
    assert asyncio.run(google.read_text("sanjarbek200217@gmail.com", doc)) == "Lesson:\nHello class"
    assert asyncio.run(google.replace_text("sanjarbek200217@gmail.com", doc, "Hello", "Hi")) == "Replaced 2 time(s) in Lesson."
    assert "docs.googleapis.com" in g.calls[-1][1]
    asyncio.run(google.replace_text("sanjarbek200217@gmail.com", deck, "Hello", "Hi"))
    assert "slides.googleapis.com" in g.calls[-1][1]
    assert asyncio.run(google.add_text("sanjarbek200217@gmail.com", doc, "Homework: p. 3")) == "Added the text at the end of Lesson."
    with pytest.raises(google.GoogleError):
        asyncio.run(google.add_text("sanjarbek200217@gmail.com", deck, "x"))


def test_an_ended_sign_in_is_dropped_and_says_so(g):
    signed_in(g)
    tokens = json.loads(google.TOKENS.read_text())
    tokens["sanjarbek200217@gmail.com"]["expires"] = 0
    google.TOKENS.write_text(json.dumps(tokens))
    g.refresh_ok = False
    with pytest.raises(google.GoogleError, match="sign in again"):
        asyncio.run(google.list_files("sanjarbek200217@gmail.com"))
    assert google.accounts() == []


def test_which_account(g):
    with pytest.raises(google.GoogleError, match="No Google account"):
        google.which()
    signed_in(g)
    assert google.which() == google.which("200217") == "sanjarbek200217@gmail.com"
    with pytest.raises(google.GoogleError, match="isn't signed in"):
        google.which("saidovsanjarbek80")


def test_file_id_from_links():
    assert google.file_id("https://docs.google.com/document/d/1Z0Vg40tEqd5cBxsW3_vCH8Nv9Q_SmiJBzbm18NnYzco/edit") == \
        "1Z0Vg40tEqd5cBxsW3_vCH8Nv9Q_SmiJBzbm18NnYzco"
    assert google.file_id("https://drive.google.com/open?id=1Z0Vg40tEqd5cBxsW3_vCH8Nv9Q_SmiJBzbm18NnYzco") == \
        "1Z0Vg40tEqd5cBxsW3_vCH8Nv9Q_SmiJBzbm18NnYzco"


def test_the_callback_on_the_mac_finishes_by_itself(g, monkeypatch):
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(google, "PORT", port)
    link = google.sign_in_link()
    state = parse_qs(urlparse(link).query)["state"][0]

    async def go():
        server = asyncio.create_task(google.serve_callback())
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /?state={state}&code=C&scope=x HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await writer.drain()
        page = (await reader.read()).decode()
        server.cancel()
        return page

    page = asyncio.run(go())
    assert "200 OK" in page and "Signed in to Google as sanjarbek200217@gmail.com" in page
    assert google.accounts() == ["sanjarbek200217@gmail.com"]


def test_tools_through_the_server(g):
    srv = tools.make_server("Telegram")

    def call(name, args):
        return asyncio.run(srv.call_tool(name, args)).content[0].text

    assert "use google_sign_in" in call("google_files", {})
    link = call("google_sign_in", {"account": "sanjarbek200217@gmail.com"})
    assert "accounts.google.com" in link and "http://127.0.0.1:8791" in link
    state = parse_qs(urlparse(link.split("Sign-in link (works for 30 min): ")[1].split()[0]).query)["state"][0]
    assert "Signed in" in call("google_sign_in_finish", {"address": f"http://127.0.0.1:8791/?state={state}&code=C"})
    assert "Lesson" in call("google_files", {"search": "lesson"})
    assert "Replaced 2" in call("google_replace_text", {"account": "", "file": "https://docs.google.com/document/d/DOC1" + "x" * 20 + "/edit",
                                                        "find": "Hello", "replace": "Hi"})
    assert "isn't signed in" in call("google_add_text", {"account": "saidov", "file": "x", "text": "y"})


def test_a_new_deck_gets_real_slides(g):
    signed_in(g)
    out = asyncio.run(google.create_slides("sanjarbek200217@gmail.com", "How a ticket gets done",
                                           [{"title": "1. He asks", "body": "In the app\nor Telegram"},
                                            {"title": "2. A worker builds it", "body": "Tests, then dev",
                                             "image_url": "https://example.com/a.png"}], subtitle="Jarvis test"))
    assert out == "Made the deck \"How a ticket gets done\" with 3 slides: https://docs.google.com/presentation/d/P1/edit"
    method, url, body = g.calls[-1]
    reqs = json.loads(body)["requests"]
    kinds = [next(iter(r)) for r in reqs]
    assert url.endswith("/P1:batchUpdate")
    assert kinds.count("createSlide") == 2 and kinds.count("createImage") == 1
    texts = [r["insertText"]["text"] for r in reqs if "insertText" in r]
    assert texts[:2] == ["How a ticket gets done", "Jarvis test"] and "In the app\nor Telegram" in texts
    ids = [r["createSlide"]["objectId"] for r in reqs if "createSlide" in r]
    assert len(set(ids)) == 2 and all(5 <= len(i) <= 50 for i in ids)      # Slides wants unique 5-50 char ids


def test_adding_a_slide_twice_uses_new_ids(g):
    signed_in(g)
    deck = "DECK" + "x" * 20
    asyncio.run(google.add_slide("sanjarbek200217@gmail.com", deck, "More"))
    first = json.loads(g.calls[-1][2])["requests"][0]["createSlide"]["objectId"]
    asyncio.run(google.add_slide("sanjarbek200217@gmail.com", deck, "Again"))
    assert json.loads(g.calls[-1][2])["requests"][0]["createSlide"]["objectId"] != first
    with pytest.raises(google.GoogleError, match="isn't a Slides deck"):
        asyncio.run(google.add_slide("sanjarbek200217@gmail.com", "DOC1" + "x" * 20, "x"))


def test_a_new_doc_gets_headings_and_text_in_order(g):
    signed_in(g)
    out = asyncio.run(google.create_doc("sanjarbek200217@gmail.com", "Notes", [
        {"heading": "Plan 🙂", "text": "Step one"}, {"heading": "Next", "text": "Step two"}]))
    assert out.endswith("/document/d/D1/edit")
    reqs = json.loads(g.calls[-1][2])["requests"]
    inserts = [(r["insertText"]["location"]["index"], r["insertText"]["text"]) for r in reqs if "insertText" in r]
    assert inserts == [(1, "Plan 🙂\n"), (9, "Step one\n"), (18, "Next\n"), (23, "Step two\n")]   # 🙂 counts 2
    styles = [r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] for r in reqs if "updateParagraphStyle" in r]
    assert styles == ["HEADING_2", "NORMAL_TEXT", "HEADING_2", "NORMAL_TEXT"]
