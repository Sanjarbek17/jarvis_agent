"""Ticket 917f6c5a: slide decks with content go up through the Claude Drive connector as a small .pptx."""
import asyncio
import base64
import io
import re
import zipfile

import xml.dom.minidom

from core import drive, slides

SPEC = {"title": "How a ticket gets done", "subtitle": "From your message to the finished work",
        "slides": [{"icon": "📱", "title": "You ask Jarvis", "short": "You ask", "lines": ["In the app", "Or Telegram"]},
                   {"icon": "📋", "title": "It becomes a ticket", "short": "Ticket", "lines": ["On the board"]},
                   {"icon": "✅", "title": "The result comes back", "short": "Result"}]}


def pages(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    doc = xml.dom.minidom.parseString(z.read("content.xml"))
    out = []
    for page in doc.getElementsByTagName("draw:page"):
        out.append(" ".join(n.data for n in _walk(page) if n.nodeType == n.TEXT_NODE))
    return z, out


def _walk(node):
    for c in node.childNodes:
        yield c
        yield from _walk(c)


def test_deck_has_title_steps_and_recap():
    z, t = pages(slides.build(SPEC))
    assert len(t) == 5                                                    # title + 3 steps + recap
    assert "How a ticket gets done" in t[0] and "📱" in t[0] and "✅" in t[0]
    assert "You ask Jarvis" in t[1] and "• In the app" in t[1] and "1 / 3" in t[1]
    assert "All together" in t[4] and "Ticket" in t[4]


def test_it_is_a_valid_odp_on_googles_page_size():
    z, _ = pages(slides.build(SPEC))
    assert z.namelist()[0] == "mimetype" and z.read("mimetype").decode() == slides.MIME
    assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    for name in ("content.xml", "styles.xml", "META-INF/manifest.xml"):
        xml.dom.minidom.parseString(z.read(name))                         # well-formed
    assert 'fo:page-width="27.94cm"' in z.read("styles.xml").decode()     # Google's page for an imported deck
    sizes = [int(v) for v in re.findall(r'fo:font-size="(\d+)pt"', z.read("content.xml").decode())]
    assert min(sizes[-3:]) >= 44                                          # the emoji styles: smaller ones vanished


def test_deck_is_small_enough_to_copy():
    data = slides.build(SPEC)
    assert len(slides.build_base64(SPEC)) < 4000
    assert base64.b64decode(slides.build_base64(SPEC)) == data


def test_text_is_escaped():
    _, t = pages(slides.build({"title": "Q&A <1>", "slides": [{"icon": "📱", "title": "a & b"}]}))
    assert "Q&A <1>" in t[0] and "a & b" in t[1]


def test_no_recap_for_one_step():
    _, t = pages(slides.build({"title": "x", "slides": [{"icon": "📱", "title": "Only"}]}))
    assert len(t) == 2


def test_upload_asks_a_careful_model_to_copy_it_and_check_titles(monkeypatch):
    seen = {}

    async def fake_run(task, words="", share=False, timeout=0, prompt="", model=""):
        seen.update(prompt=prompt, model=model, timeout=timeout)
        return "https://docs.google.com/presentation/d/X/edit — all 3 slide titles are there"

    monkeypatch.setattr(drive, "run", fake_run)
    out = asyncio.run(drive.upload_slides(SPEC))
    assert "all 3 slide titles" in out
    assert seen["model"] == drive.UPLOAD_MODEL and seen["timeout"] == drive.UPLOAD_TIMEOUT
    b64 = seen["prompt"].split("BEGIN\n")[1].split("\nEND")[0]
    assert base64.b64decode(b64) == slides.build(SPEC)                    # exactly the deck we drew
    assert '"You ask Jarvis"; "It becomes a ticket"; "The result comes back"' in seen["prompt"]
    assert slides.MIME in seen["prompt"]


def test_upload_command_uses_the_given_model():
    cmd = drive.command("", prompt="p", model="claude-opus-5-5")
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5" and cmd[cmd.index("-p") + 1] == "p"
