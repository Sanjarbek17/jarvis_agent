"""Telegram messages to Sanjar in HTML (parse_mode="html") so they're quick to read (Sanjar, 2026-11-02:
"the message you type for me is hard to read ... use html style output").

- md(): the light Markdown that workers and the brain write (**bold**, `code`, "- " bullets, "> " notes,
  [text](url), # headings) -> Telegram HTML. Everything else is escaped (&, <, >). ```diff blocks
  become ✅ / ⚠️ bullets (Telegram shows no diff colours); other code blocks are cut short.
- ticket(): a runner update: bold header (icon, what happened, ref), project + title, bold labelled
  sections (Question / Answer / Result ...), the link.
- send(): sends HTML, splits at the 4096-character limit, and falls back to plain text if Telegram
  rejects the HTML.
"""
import html
import logging
import re

log = logging.getLogger("jarvis.tgfmt")
LIMIT = 4096
BODY_LIMIT = 3000                      # characters of a section's source text (HTML adds tags)

ICON = {"done": "✅", "failed": "❌", "needs_me": "❓", "stopped": "⏹", "auto_answered": "🤖"}
WHAT = {"done": "Done", "failed": "Failed", "needs_me": "Needs you", "stopped": "Stopped",
        "auto_answered": "Answered for you"}


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _inline(line: str) -> str:
    """One escaped line: `code`, **bold**, __bold__, *italic*, [text](url)."""
    out, pos = [], 0
    for m in re.finditer(r"`([^`\n]+)`", line):          # code first: nothing inside it is formatted
        out.append(_marks(line[pos:m.start()]))
        out.append(f"<code>{esc(m.group(1))}</code>")
        pos = m.end()
    out.append(_marks(line[pos:]))
    return "".join(out)


def _marks(text: str) -> str:
    links: list[str] = []

    def keep_link(m):
        links.append(f'<a href="{html.escape(m.group(2), quote=True)}">{esc(m.group(1))}</a>')
        return f"\x00{len(links) - 1}\x00"

    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", keep_link, text)
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], text)


PRE_LINES = 6                          # longer code blocks are cut: Telegram shows them as grey walls


def _block(lang: str, code: list[str]) -> list[str]:
    """A fenced block. Telegram doesn't colour diffs (Sanjar: "the diff is not readable here"), so
    +/- lines become ✅ / ⚠️ bullets under bold headings; other blocks stay <pre>, cut short."""
    if lang == "diff" or (code and all(l[:1] in "+- " for l in code if l.strip())
                          and any(l.startswith(("+", "-")) for l in code)):
        done = [l[1:].strip() for l in code if l.startswith("+") and l[1:].strip()]
        todo = [l[1:].strip() for l in code if l.startswith("-") and l[1:].strip()]
        out = []
        if done:
            out += (["<b>What changed</b>"] if todo else []) + [f"✅ {_inline(l)}" for l in done]
        if todo:
            out += ([""] if done else []) + (["<b>Still to do / watch out</b>"] if done else []) + [f"⚠️ {_inline(l)}" for l in todo]
        return out
    shown = code[:PRE_LINES] + (["…"] if len(code) > PRE_LINES else [])
    return ["<pre>" + esc("\n".join(shown)) + "</pre>"] if shown else []


def md(text: str) -> str:
    """Markdown-ish text -> Telegram HTML."""
    out, in_code, code, lang = [], False, [], ""
    for raw in (text or "").splitlines():
        if raw.strip().startswith("```"):
            if in_code:
                out += _block(lang, code)
                code, in_code = [], False
            else:
                in_code, lang = True, raw.strip()[3:].strip().lower()
            continue
        if in_code:
            code.append(raw)
            continue
        line = raw.rstrip()
        if m := re.match(r"\s*>\s?(.*)", line):          # a "> note": a quote, kept short
            out.append(f"<blockquote>{_inline(m.group(1))}</blockquote>")
        elif m := re.match(r"\s*#{1,6}\s+(.*)", line):
            out.append(f"<b>{_inline(m.group(1))}</b>")
        elif m := re.match(r"(\s*)[-*•]\s+(.*)", line):
            out.append(f"{'  ' * (len(m.group(1)) // 2)}• {_inline(m.group(2))}")
        elif re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            out.append("")
        else:
            out.append(_inline(line))
    if in_code:                                       # unclosed block: still show it
        out += _block(lang, code)
    text = "\n".join(out).replace("</blockquote>\n<blockquote>", "\n")   # one quote per run of > lines
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def plain(html_text: str) -> str:
    """HTML -> plain text, for the fallback."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", html_text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _cut(text: str, n: int = BODY_LIMIT) -> str:
    return text if len(text) <= n else text[:n].rsplit("\n", 1)[0] + "\n…"


def section(label: str, body: str) -> str:
    return f"<b>{esc(label)}</b>\n{md(_cut(body))}" if body.strip() else ""


def ticket(card, kind: str, text: str) -> str:
    """A runner update about one ticket."""
    head = (f"{ICON.get(kind, '•')} <b>{WHAT.get(kind, kind.replace('_', ' ').title())}</b> · "
            f"<code>{esc(card.ref)}</code>\n<b>{esc(card.project)}</b> · {esc(card.title)}")
    if kind == "needs_me":
        parts = [section("Question", text)]
    elif kind == "auto_answered":
        m = re.match(r"(?s)It asked:\s*(.*?)\n\s*\nI answered(?: \(([^)]*)\))?:\s*(.*)", text)
        parts = ([section("Question", m.group(1)),
                  section(f"Answer ({m.group(2)})" if m.group(2) else "Answer", m.group(3))] if m
                 else [section("Answer", text)])
    elif kind == "stopped":
        parts = [section("Why", text)]
    else:
        parts = [section("Result", text)]
    link = f'<a href="{html.escape(card.url, quote=True)}">Open the ticket</a>' if card.url else ""
    return "\n\n".join(p for p in (head, *parts, link) if p)


def chunks(html_text: str, limit: int = LIMIT) -> list[str]:
    """Split at paragraph / line breaks so each part fits; never inside a <pre> block if avoidable."""
    if len(html_text) <= limit:
        return [html_text]
    parts, cur = [], ""
    for para in html_text.split("\n\n"):
        piece = para if not cur else "\n\n" + para
        if len(cur) + len(piece) <= limit:
            cur += piece
            continue
        if cur:
            parts.append(cur)
        while len(para) > limit:                      # one huge paragraph: cut it as plain text
            parts.append(esc(plain(para)[: limit - 50]) + "…")
            para = ""
        cur = para
    if cur:
        parts.append(cur)
    return parts


async def send(tg, chat, html_text: str, **kw):
    """Send HTML; plain text if Telegram rejects it. Returns the last message sent."""
    msg = None
    for part in chunks(html_text):
        try:
            msg = await tg.send_message(chat, part, parse_mode="html", **kw)
        except Exception as e:                        # bad entities etc.: the words matter more than style
            log.warning("HTML message rejected (%r); sending plain text", e)
            msg = await tg.send_message(chat, plain(part)[:LIMIT], parse_mode=None, **kw)
    return msg


async def reply(event, html_text: str, **kw):
    """Like event.reply, in HTML with the same fallback."""
    msg = None
    for i, part in enumerate(chunks(html_text)):
        send_fn = event.reply if i == 0 else event.respond
        try:
            msg = await send_fn(part, parse_mode="html", **kw)
        except Exception as e:
            log.warning("HTML reply rejected (%r); sending plain text", e)
            msg = await send_fn(plain(part)[:LIMIT], parse_mode=None, **kw)
    return msg
