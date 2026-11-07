"""Google Drive, Docs, Sheets and Slides for Jarvis (ticket e6199929), through the Google Drive connector
Sanjar added to his Claude account (it is on sanjarsaidov173@gmail.com).

Jarvis's brains run with no user settings and only Jarvis's own MCP server, so they never see claude.ai
connectors. Loading his user settings in every brain would add ~100 tools (Notion, Codewalk, Claude Docs...)
and his own output style to every message, so instead the brain hands a Drive job to this helper: one
short `claude -p` that loads the user settings, may use only the Google Drive tools (no shell, no files),
does the job and says what it found or changed. Trashing a file is never allowed; sharing only when the
brain says he asked for it.

What the connector can do (tested 2026-11-07): search, read Docs / Sheets / Slides, create files (with text, which
becomes a Google Doc), copy, rename, move, star, comment. It can NOT change what's inside an existing Doc or
Slides deck: that needs Google's Docs/Slides API with his own sign-in. Until then the helper says so and offers
an edited copy as a new Doc; the brain can also do an in-place edit in Chrome with mac_task.
"""
import asyncio
import json
import logging

log = logging.getLogger("jarvis.drive")
MODEL = "claude-sonnet-5-5"          # quick and good at documents; the brain on Opus decides what to ask
TIMEOUT = 300
ACCOUNT = "sanjarsaidov173@gmail.com"
TOOLS = "mcp__claude_ai_Google_Drive__*"
NEVER = ["mcp__claude_ai_Google_Drive__trash_file"]
SHARING = ["mcp__claude_ai_Google_Drive__share_file", "mcp__claude_ai_Google_Drive__request_access"]

PROMPT = """You are Jarvis's Google Drive helper. Sanjar (his Drive is {account}) wants this done, using the
Google Drive tools (they cover Drive files and Google Docs, Sheets and Slides): {task}

His own words: {words}

- If the Google Drive tools aren't listed yet, use ToolSearch to load them (it waits for the connector).
- Find files by searching their names and contents; if several could be meant, use the most recently
  changed one and say which you used.
- Change only what the task asks. Never delete or trash anything.{share}
- These tools can't change the text inside an existing Doc or Slides deck. If the task needs that, don't
  fake it: say so, and if it's a Doc, offer to make an edited copy as a new Doc (the original stays).
- When you change or create a file, say exactly what changed and give its link.
- Answer for Jarvis in plain short text (no tables, no code blocks): what you found or did, with file names
  and links. If something can't be done with these tools, say so plainly."""


def command(task: str, words: str = "", share: bool = False) -> list[str]:
    prompt = PROMPT.format(account=ACCOUNT, task=task.strip(), words=(words or task).strip(),
                           share="" if share else " Don't share files or change who can see them.")
    return ["claude", "-p", prompt, "--model", MODEL,
            "--setting-sources", "user",                       # the claude.ai connectors come with these
            "--settings", json.dumps({"outputStyle": "default"}),  # not his chat style: this is for Jarvis
            "--mcp-config", json.dumps({"mcpServers": {}}),
            "--tools", "ToolSearch",                           # no shell, no files; ToolSearch waits for a
            "--allowedTools", f"{TOOLS},ToolSearch",           # connector that's still connecting (c270b9ef)
            "--disallowedTools", *(NEVER + ([] if share else SHARING)),
            "--no-session-persistence", "--output-format", "json"]


NOT_READY = ("still connecting", "not connected", "don't have", "do not have", "no google drive tools", "aren't available",
             "are not available", "isn't available", "unavailable")


def not_ready(text: str) -> bool:
    """The helper says the Drive tools weren't there (the connector hadn't finished connecting)."""
    low = text.lower()
    return "drive" in low and any(p in low for p in NOT_READY)


async def run(task: str, words: str = "", share: bool = False, timeout: float = TIMEOUT) -> str:
    """Do one Drive job; once more if the connector wasn't ready (ticket c270b9ef)."""
    text = await _run_once(task, words, share, timeout)
    if not_ready(text):
        log.warning("drive connector wasn't ready, trying again: %s", text[:200])
        await asyncio.sleep(10)
        text = await _run_once(task, words, share, timeout)
    log.info("drive helper: %s", text[:200].replace("\n", " "))
    return text


async def _run_once(task: str, words: str, share: bool, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(*command(task, words, share), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"The Google Drive job took longer than {timeout // 60:.0f} minutes and was stopped."
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        log.warning("drive helper gave no answer (exit %s)", proc.returncode)
        return "The Google Drive helper didn't answer. It may be the Claude usage limit or the connector being down."
    text = (envelope.get("result") or "").strip()
    if envelope.get("is_error"):
        return f"Google Drive failed: {text[:300]}"
    return text or "The Google Drive helper finished but said nothing."
