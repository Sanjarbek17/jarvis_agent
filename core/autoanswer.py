"""Jarvis answers a worker's "Needs me" question himself, for projects where Sanjar asked for that
("auto_answer": true in config/projects.json; first: AI English Teacher, 2026-11-02, "never ask me").

One tool-less-but-read-only Claude run (the brain's model) gets: the ticket and the worker's result,
the question, what Jarvis remembers about it (long-term memory of Sanjar's chats and calls), the
standing rules, and read access to all of ~/dev, .env files included (to find ids/tokens/configs from his
other projects, per his rule of 2026-11-04; values are never repeated). It answers as
Sanjar would, from what he already said or the docs decided; where nothing covers it, it picks the
sensible default and says so. Things only Sanjar can do in the real world (money, a SIM, an
account, a phone test) become "do everything else, write down what's left, carry on".
"""
import asyncio
import contextlib
import json
import logging
import re

from . import memory
from .brain import BRAIN_MODEL
from .config import DEV, PROJECTS, reload_projects, rules_text

log = logging.getLogger("jarvis.autoanswer")
TIMEOUT = 15 * 60
MAX_PER_TICKET = 4                    # after this many answers on one ticket, Sanjar gets it after all
# the one question that stays his (standing rule: main only with his explicit permission)
FOR_SANJAR = re.compile(r"\bmain\b|\bmaster\b", re.I)

PROMPT = """You are Jarvis, Sanjar's assistant. A worker building his project "{project}" stopped and
asked him a question. Sanjar is busy and told you to answer such questions yourself, in his place,
"from our previous conversations; never ask me for answers". Answer it now so the worker can go on.

Ticket {ref}: {title} ({url})
The worker's question:
{question}

The end of the worker's report:
{result}

What you remember from Sanjar's chats and calls that may bear on it:
{memories}

His standing rules:
{rules}

You can read the project's files (read-only): {folders}, and all of ~/dev. Look at the spec and design
docs (docs/*SRS*, docs/*SDD*, sdd/choices.json) and any skill/CLAUDE.md before deciding. If the question
is about an id, token, key, account or config, look for it in his other projects under ~/dev first,
.env files included, and tell the worker which file and name to take it from (never write the value).

How to answer:
- Decide. Use what Sanjar said or the docs already decided; if nothing covers it, pick the option
  that keeps the build moving with the least risk and say it's your call.
- If something doesn't exist yet (a bot, a key, an account), tell the worker to create it itself, and
  never to reuse another live bot's token. Only what truly can't be found or created (pay, buy a SIM,
  his own password, a code sent to his phone, a real-phone test) is left for Sanjar: tell the worker
  to do everything else, put that
  step in the issue as a clear checklist item for Sanjar, mark it as pending, and carry on; never
  tell it to wait for him.
- Never allow spending money, deploying to production, pushing to main, deleting data, or
  anything his rules forbid.
- Write 2-6 plain sentences addressed to the worker. No preamble.

Does Sanjar really have to do something himself? Only if nothing useful can go on without his own
action (a payment, his own password or phone code, a test on his own phone or account, a choice only
he can make). Then set "needs_sanjar": true and write "ask_sanjar": one or two short plain sentences
to him: exactly what to do and why, no jargon, no ticket numbers, no worker wording. Otherwise false.

Reply with JSON only: {{"answer": "<your answer to the worker>", "basis": "<memory | docs | your call>",
"needs_sanjar": false, "ask_sanjar": ""}}"""


async def answer(card, question: str, result: str) -> dict | None:
    """{"answer", "basis", "needs_sanjar", "ask_sanjar"}, or None if Jarvis shouldn't or couldn't
    answer it (then the runner sends the worker's question to Sanjar as before)."""
    if FOR_SANJAR.search(question):
        return None
    reload_projects()
    folders = [str(DEV / f) for f in PROJECTS.get(card.project, {}).get("folders", []) if (DEV / f).is_dir()]
    try:
        memories = await memory.recall_text(f"{card.project} {card.title} {question}")
    except Exception as e:
        log.warning("memory recall failed: %r", e)
        memories = ""
    prompt = PROMPT.format(project=card.project, ref=card.ref, title=card.title, url=card.url,
                           question=question, result=result[-3000:], memories=memories or "(nothing found)",
                           rules=rules_text() or "(none)", folders=", ".join(folders) or "(none)")
    cmd = ["claude", "-p", prompt, "--model", BRAIN_MODEL, "--output-format", "json",
           "--tools", "Read,Glob,Grep", "--add-dir", str(DEV), "--setting-sources", "",
           "--strict-mcp-config", "--no-session-persistence"]
    for f in folders:
        cmd += ["--add-dir", f]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=folders[0] if folders else None,
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        envelope = json.loads(out)
        text = envelope.get("result") or ""
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {"answer": text.strip(), "basis": "your call"}
        out = {"answer": str(data.get("answer") or "").strip(), "basis": str(data.get("basis") or ""),
               "needs_sanjar": bool(data.get("needs_sanjar")), "ask_sanjar": str(data.get("ask_sanjar") or "").strip()}
        if out["needs_sanjar"] and not out["ask_sanjar"]:
            out["ask_sanjar"] = question
        return out if (out["answer"] or out["needs_sanjar"]) else None
    except Exception as e:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        log.warning("auto-answer failed for %s: %r", card.ref, e)
        return None


def enabled(project: str) -> bool:
    """Every project's worker questions go to Jarvis first (Sanjar, 2026-11-04: "only send it to you
    not me ... then you tell me if needed"), unless a project sets "auto_answer": false."""
    reload_projects()
    return PROJECTS.get(project, {}).get("auto_answer", True) is not False
