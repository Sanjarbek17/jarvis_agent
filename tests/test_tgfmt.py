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
    out = tgfmt.md("# Title\n- one\n* two\n```diff\n+ added <x>\n- gone\n```\n```\nls -la\n```")
    assert out.startswith("<b>Title</b>")
    assert "• one" in out and "• two" in out
    assert "✅ added &lt;x&gt;" in out and "⚠️ gone" in out     # Telegram shows no diff colours
    assert "<pre>ls -la</pre>" in out


def test_unclosed_code_block_still_shown():
    assert tgfmt.md("```\nx < y").endswith("<pre>x &lt; y</pre>")


def test_flat_md_for_quotes():
    out = tgfmt.md("a\n\n> note\n```\ncode\n```", flat=True)
    assert out == "a\n<i>note</i>\ncode"


def parsed(html_text):
    from telethon.extensions import html as th
    return th.parse(html_text)


def test_ticket_done_is_style2():
    out = tgfmt.ticket(CARD, "done", "Added **HTML** styling to updates.\n\n- ✅ new `core/tgfmt.py`\n- ⚠️ live after restart")
    lines = out.split("\n")
    assert lines[0] == "✅ <b>Done:</b> Decide: lesson plan &lt;fixed&gt; &amp; generated"
    assert lines[1] == "<code>aet-4</code> · AI English Teacher"
    assert lines[2] == "<blockquote>Added <b>HTML</b> styling to updates.</blockquote>"
    assert "<b>Details</b>\n<blockquote expandable>• ✅ new <code>core/tgfmt.py</code>\n• ⚠️ live after restart</blockquote>" in out
    assert out.endswith('<a href="https://github.com/x/y/issues/4?a=1&amp;b=2">Open the ticket</a>')
    text, ents = parsed(out)
    kinds = {type(e).__name__ for e in ents}
    assert "MessageEntityBlockquote" in kinds
    assert any(getattr(e, "collapsed", False) for e in ents)          # the details are folded


def test_diff_block_becomes_bullets_not_code():
    out = tgfmt.ticket(CARD, "done", "Done.\n\n```diff\n+ added x\n- y still broken\n```")
    assert "<pre>" not in out and "```" not in out
    assert "✅ added x" in out and "⚠️ y still broken" in out


def test_code_block_inside_details_is_plain_lines():
    out = tgfmt.ticket(CARD, "failed", "Tests failed.\n\n```\nE assert 1 == 2\n```")
    assert "<pre>" not in out and "E assert 1 == 2" in out
    assert out.startswith("❌ <b>Failed:</b>")


def test_needs_me_shows_question_in_quote():
    out = tgfmt.ticket(CARD, "needs_me", "Which **option**: fixed or generated?")
    assert out.startswith("❓ <b>Needs you:</b>")
    assert "<blockquote>Which <b>option</b>: fixed or generated?</blockquote>" in out
    assert "Details" not in out


def test_auto_answered_q_and_a():
    out = tgfmt.ticket(CARD, "auto_answered",
                       "It asked: Fixed or generated?\n\nI answered (docs): Fixed plan, per SDD. Generated plans wait for R2.")
    assert "<blockquote><b>Q:</b> Fixed or generated?\n<b>A (docs):</b> Fixed plan, per SDD.</blockquote>" in out
    assert "<blockquote expandable>Generated plans wait for R2.</blockquote>" in out


def test_long_summary_is_cut_to_first_sentence():
    short, rest = tgfmt.split_summary("First sentence is here. " + "more words " * 60 + "\n\nNext para.")
    assert short == "First sentence is here." and rest.endswith("Next para.")


def test_mac_stuck_daily():
    assert tgfmt.mac("open Spotify", "Opened it.", False).startswith("✅ <b>Mac:</b> open Spotify")
    st = tgfmt.stuck([("aet-9", "AI English Teacher", "[09] Renderer", 3, "")])
    assert st.startswith("⏸ <b>Stuck</b>\n<blockquote>1 ticket In Progress") and "expandable" in st
    d = tgfmt.daily("2 tickets done.\n\n- aet-1 done\n- aet-2 done")
    assert d.startswith("🗓 <b>Today</b>\n<blockquote>2 tickets done.</blockquote>")
    for html_text in (st, d):
        parsed(html_text)


def test_ticket_long_result_stays_under_limit():
    out = tgfmt.ticket(CARD, "done", "Summary.\n\n" + "line\n" * 2000)
    assert len(out) < tgfmt.LIMIT and "…" in out


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
