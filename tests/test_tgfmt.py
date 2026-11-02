import asyncio
from types import SimpleNamespace as NS

from core import tgfmt

CARD = NS(ref="aet-4", project="AI English Teacher", title="Decide: lesson plan <fixed> & generated",
          url="https://github.com/x/y/issues/4?a=1&b=2")


def test_escapes_html():
    assert tgfmt.md("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_inline_marks():
    out = tgfmt.md("**Done** with `core/x.py` and *why* see [docs](https://e.com/a?x=1&y=2)")
    assert "<b>Done</b>" in out and "<code>core/x.py</code>" in out and "<i>why</i>" in out
    assert '<a href="https://e.com/a?x=1&amp;y=2">docs</a>' in out


def test_code_is_not_formatted_inside():
    assert tgfmt.md("`a **b** <c>`") == "<code>a **b** &lt;c&gt;</code>"


def test_bullets_headings_and_blocks():
    out = tgfmt.md("# Title\n- one\n* two\n```diff\n+ added <x>\n- gone\n```")
    assert out.startswith("<b>Title</b>")
    assert "• one" in out and "• two" in out
    assert "<pre>+ added &lt;x&gt;\n- gone</pre>" in out


def test_unclosed_code_block_still_shown():
    assert tgfmt.md("```\nx < y").endswith("<pre>x &lt; y</pre>")


def test_ticket_needs_me():
    out = tgfmt.ticket(CARD, "needs_me", "Which **option**?")
    assert out.startswith("❓ <b>Needs you</b> · <code>aet-4</code>")
    assert "lesson plan &lt;fixed&gt; &amp; generated" in out
    assert "<b>Question</b>\nWhich <b>option</b>?" in out
    assert 'href="https://github.com/x/y/issues/4?a=1&amp;b=2"' in out


def test_ticket_auto_answered_sections():
    out = tgfmt.ticket(CARD, "auto_answered", "It asked: Fixed or generated?\n\nI answered (docs): Fixed plan, per SDD.")
    assert "<b>Question</b>\nFixed or generated?" in out
    assert "<b>Answer (docs)</b>\nFixed plan, per SDD." in out


def test_ticket_done_and_long_result_is_cut():
    out = tgfmt.ticket(CARD, "done", "line\n" * 2000)
    assert out.startswith("✅ <b>Done</b>") and "<b>Result</b>" in out and "…" in out
    assert len(out) < tgfmt.LIMIT


def test_chunks_respect_limit():
    text = "\n\n".join(["para " + "x" * 900] * 12)
    parts = tgfmt.chunks(text)
    assert len(parts) > 1 and all(len(p) <= tgfmt.LIMIT for p in parts)


class FakeTG:
    def __init__(self, reject_html):
        self.reject, self.sent = reject_html, []

    async def send_message(self, chat, text, parse_mode=None, **kw):
        if parse_mode == "html" and self.reject:
            raise ValueError("Can't parse entities")
        self.sent.append((parse_mode, text))
        return NS(id=len(self.sent))


def test_send_html_and_plain_fallback():
    ok = FakeTG(reject_html=False)
    asyncio.run(tgfmt.send(ok, 1, "<b>hi</b> &amp; you"))
    assert ok.sent == [("html", "<b>hi</b> &amp; you")]
    bad = FakeTG(reject_html=True)
    asyncio.run(tgfmt.send(bad, 1, '<b>hi</b> &amp; <a href="https://e.com">you</a>'))
    assert bad.sent == [(None, "hi & you (https://e.com)")]
