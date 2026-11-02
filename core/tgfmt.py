"""Telegram messages to Sanjar in HTML (parse_mode="html") so they're quick to read (Sanjar, 2026-11-02:
"the message you type for me is hard to read ... use html style output").

- md(): the light Markdown that workers and the brain write (**bold**, `code`, "- " bullets, "> " notes,
  [text](url), # headings) -> Telegram HTML. Everything else is escaped (&, <, >). ```diff blocks
  become ✅ / ⚠️ bullets (Telegram shows no diff colours); other code blocks are cut short.
- Style 2 (Sanjar picked it from real samples, 2026-11-02, ticket 29bac419) for every update: a header
  line (icon, what happened, title), ref + project, the short summary in a quote, then "Details" in an
  expandable quote (folded until tapped), then the link. ticket() / mac() / stuck() / daily() build it.
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


def _block(lang: str, code: list[str], flat: bool = False) -> list[str]:
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
    if flat:                                          # inside a quote: plain lines, no grey block
        return [esc(l) for l in shown if l.strip()]
    return ["<pre>" + esc("\n".join(shown)) + "</pre>"] if shown else []


def md(text: str, flat: bool = False) -> str:
    """Markdown-ish text -> Telegram HTML. flat: for inside a quote (no nested quotes, no <pre>,
    no blank lines)."""
    out, in_code, code, lang = [], False, [], ""
    for raw in (text or "").splitlines():
        if raw.strip().startswith("```"):
            if in_code:
                out += _block(lang, code, flat)
                code, in_code = [], False
            else:
                in_code, lang = True, raw.strip()[3:].strip().lower()
            continue
        if in_code:
            code.append(raw)
            continue
        line = raw.rstrip()
        if m := re.match(r"\s*>\s?(.*)", line):          # a "> note": a quote, kept short
            out.append(f"<i>{_inline(m.group(1))}</i>" if flat else f"<blockquote>{_inline(m.group(1))}</blockquote>")
        elif m := re.match(r"\s*#{1,6}\s+(.*)", line):
            out.append(f"<b>{_inline(m.group(1))}</b>")
        elif m := re.match(r"(\s*)[-*•]\s+(.*)", line):
            out.append(f"{'  ' * (len(m.group(1)) // 2)}• {_inline(m.group(2))}")
        elif re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            out.append("")
        else:
            out.append(_inline(line))
    if in_code:                                       # unclosed block: still show it
        out += _block(lang, code, flat)
    text = "\n".join(out).replace("</blockquote>\n<blockquote>", "\n")   # one quote per run of > lines
    return re.sub(r"\n{2,}" if flat else r"\n{3,}", "\n" if flat else "\n\n", text).strip()


def plain(html_text: str) -> str:
    """HTML -> plain text, for the fallback."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", html_text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _cut(text: str, n: int = BODY_LIMIT) -> str:
    return text if len(text) <= n else text[:n].rsplit("\n", 1)[0] + "\n…"


SUMMARY_MAX = 300                      # the always-visible quote stays short


def split_summary(text: str) -> tuple[str, str]:
    """(summary, details): the first paragraph (or its first sentence if long) and the rest."""
    text = (text or "").strip()
    if not text or text.startswith("```"):
        return "", text
    first, _, rest = text.partition("\n\n")
    lines = first.splitlines()
    if len(lines) > 1 and re.match(r"\s*([-*•]|\d+\.)\s", lines[1]):   # "Result:\n- a\n- b": keep the list
        first, rest = lines[0], "\n".join(lines[1:]) + ("\n\n" + rest if rest else "")
    first = re.sub(r"^\s*#{1,6}\s+", "", first).strip()
    if len(first) > SUMMARY_MAX:
        m = re.match(r"(.{20,%d}?[.!?])\s+(.*)" % SUMMARY_MAX, first, re.S)
        if m:
            first, rest = m.group(1), m.group(2) + ("\n\n" + rest if rest else "")
        else:
            first, rest = first[:SUMMARY_MAX].rsplit(" ", 1)[0] + "…", first + ("\n\n" + rest if rest else "")
    return first, rest.strip()


def style2(icon: str, label: str, title: str, sub: str = "", summary: str = "", details: str = "",
           link: str = "", summary_html: str = "") -> str:
    """Header, optional sub line, summary quote, folded Details quote, link. summary/details are
    Markdown-ish text; summary_html is used as is."""
    head = f"{icon} <b>{esc(label)}:</b> {esc(title)}".rstrip(": ") if title else f"{icon} <b>{esc(label)}</b>"
    quote = summary_html or (md(_cut(summary, SUMMARY_MAX * 2), flat=True) if summary.strip() else "")
    body = md(_cut(details), flat=True) if details.strip() else ""
    parts = [head, sub,
             f"<blockquote>{quote}</blockquote>" if quote else "",
             f"<b>Details</b>\n<blockquote expandable>{body}</blockquote>" if body else "",
             link]
    return "\n".join(p for p in parts if p)


def _link(url: str, text: str = "Open the ticket") -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>' if url else ""


def ticket(card, kind: str, text: str) -> str:
    """A runner update about one ticket, in Style 2."""
    icon, label = ICON.get(kind, "•"), WHAT.get(kind, kind.replace("_", " ").title())
    sub = f"<code>{esc(card.ref)}</code> · {esc(card.project)}"
    link = _link(getattr(card, "url", ""))
    if kind == "auto_answered":
        m = re.match(r"(?s)It asked:\s*(.*?)\n\s*\nI answered(?: \(([^)]*)\))?:\s*(.*)", text)
        if m:
            q, basis, a = m.group(1).strip(), m.group(2), m.group(3).strip()
            a_short, a_rest = split_summary(a)
            if m2 := re.match(r"(.{10,}?[.!?])\s+(\S.*)", a_short, re.S):   # only the first sentence shows
                a_short, a_rest = m2.group(1), m2.group(2) + ("\n\n" + a_rest if a_rest else "")
            quote = (f"<b>Q:</b> {md(_cut(q, SUMMARY_MAX), flat=True)}\n"
                     f"<b>A{' (' + esc(basis) + ')' if basis else ''}:</b> {md(a_short, flat=True)}")
            return style2(icon, label, card.title, sub, details=a_rest, link=link, summary_html=quote)
    if kind == "needs_me":                            # the question is the point: show it, not fold it
        short, rest = (text, "") if len(text) <= SUMMARY_MAX * 2 else split_summary(text)
        return style2(icon, label, card.title, sub, summary=short, details=rest, link=link)
    short, rest = split_summary(text)
    return style2(icon, label, card.title, sub, summary=short, details=rest, link=link)


def mac(task: str, text: str, question: bool) -> str:
    """A Mac task's result or question, in Style 2."""
    icon = "❓" if question else "❌" if text.startswith("Failed") else "✅"
    short, rest = split_summary(text)
    return style2(icon, "Mac needs you" if question else "Mac", task, summary=short, details=rest)


def stuck(rows: list[tuple]) -> str:
    """The stuck-ticket list: rows of (ref, project, title, hours, jarvis)."""
    lines = "\n".join(f"• <code>{esc(r)}</code> {esc(p)} · {esc(t)} — untouched {h:.0f} h"
                      + (f", Jarvis {esc(j)}" if j else "") for r, p, t, h, j in rows)
    quote = (f"{len(rows)} ticket{'s' if len(rows) != 1 else ''} In Progress with nobody working on "
             f"{'them' if len(rows) != 1 else 'it'}. Say \"queue <code>{esc(rows[0][0])}</code>\" to restart one.")
    return (f"⏸ <b>Stuck</b>\n<blockquote>{quote}</blockquote>\n"
            f"<b>Details</b>\n<blockquote expandable>{lines}</blockquote>")


def daily(text: str) -> str:
    short, rest = split_summary(text)
    return style2("🗓", "Today", "", summary=short, details=rest)


def section(label: str, body: str) -> str:
    return f"<b>{esc(label)}</b>\n{md(_cut(body))}" if body.strip() else ""


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
