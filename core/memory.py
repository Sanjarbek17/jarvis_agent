"""Jarvis's long-term memory: facts and past conversations that survive restarts, /clear and new calls.

Everything lives in var/state/memory/items.jsonl (one item per line, with its embedding). Kinds:
  fact  - something Sanjar told Jarvis to keep: a preference, decision, name, plan ("remember" tool)
  chat  - one Telegram exchange (his message + Jarvis's reply), logged automatically
  call  - one turn of a call, logged automatically; old call transcripts are indexed once at start
Search is by meaning (Gemini embeddings, works across Uzbek / Russian / English), with a keyword
fallback when embeddings are unavailable, so nothing breaks offline.

Items are linked, because one chat covers many topics and a single line out of context is useless:
- `thread`: items about the same topic share a thread id. A small fast Claude model (TOPIC_MODEL,
  through the `claude` CLI, so no Gemini rate limit) sees the
  new exchange and the labels of recent threads and says which thread it continues, or that it is
  a new topic (with a short label), plus any other threads it relates to (`related` links). This
  runs in the background after the reply, so it costs no time. Embedding similarity alone can't do
  this: all chat messages look alike to it. If the model is unavailable, a short follow-up within
  CONTINUE_WINDOW stays in the last thread, anything else starts a new one. All chunks of one call
  transcript are one thread.
- `prev`: the item said just before it in the same channel, so neighbours can be pulled in too.
Recall returns whole threads in time order, not isolated messages.
"""
import asyncio
import json
import logging
import re
import time
import uuid

import numpy as np
from google import genai
from google.genai import types

from .config import CALLS, GEMINI_API_KEY, STATE

log = logging.getLogger("jarvis.memory")
DIR = STATE / "memory"
ITEMS = DIR / "items.jsonl"
INDEXED = DIR / "indexed_calls.json"   # call transcript files already put into memory
MODEL, DIM = "gemini-embedding-001", 768
EMBED_TIMEOUT = 2.0                    # a call turn must not wait long for memory
# assigns exchanges to threads. Claude via the `claude` CLI (Sanjar: Gemini's free tier allows only
# ~5 requests/minute). Haiku: fast and cheap, and this runs after every exchange.
TOPIC_MODEL = "claude-haiku-5-5"
TOPIC_TIMEOUT = 90
THREADS = DIR / "threads.json"         # thread id -> {"label", "last", "count", "related": [...]}
CONTINUE_WINDOW = 10 * 60              # seconds
SHORT_FOLLOWUP = 6                     # words: "ok do it" right after = same thread
THREAD_ITEMS = 12                      # at most this many items of one thread in recall
RECENT_THREADS = 30                    # thread labels shown to the topic model

_gemini = genai.Client(api_key=GEMINI_API_KEY)
_items: list[dict] | None = None       # loaded lazily
_matrix: np.ndarray | None = None      # normalized vectors of _items (rows with no vector are zero)


def _load() -> list[dict]:
    global _items, _matrix
    if _items is None:
        DIR.mkdir(parents=True, exist_ok=True)
        _items, gone = [], set()
        if ITEMS.exists():
            for line in ITEMS.read_text().splitlines():
                try:
                    it = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if it.get("forget"):
                    gone.add(it["forget"])
                else:
                    _items.append(it)
        _items = [it for it in _items if it["id"] not in gone]
        _matrix = None
    return _items


def _vectors() -> np.ndarray:
    global _matrix
    items = _load()
    if _matrix is None or len(_matrix) != len(items):
        m = np.zeros((len(items), DIM), dtype=np.float32)
        for i, it in enumerate(items):
            if it.get("vec"):
                v = np.asarray(it["vec"], dtype=np.float32)
                m[i] = v / (np.linalg.norm(v) or 1)
        _matrix = m
    return _matrix


async def _embed(texts: list[str], query: bool = False) -> list[list[float]] | None:
    try:
        r = await asyncio.wait_for(_gemini.aio.models.embed_content(
            model=MODEL, contents=texts, config=types.EmbedContentConfig(
                output_dimensionality=DIM, task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT")),
            EMBED_TIMEOUT if query else 30)
        return [e.values for e in r.embeddings]
    except Exception as e:
        log.warning("embedding failed (%r); keyword search only for now", e)
        return None


_last: dict[str, dict] = {}            # channel -> last item added there (for threads and prev links)


def _threads() -> dict[str, dict]:
    try:
        return json.loads(THREADS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_threads(t: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    THREADS.write_text(json.dumps(t, ensure_ascii=False, indent=1))


TOPIC_PROMPT = """You file one new exchange from Sanjar's assistant into conversation threads.
Threads (id: label), most recent first:
{threads}
The previous exchange in this conversation was in thread: {last}

New exchange:
{text}

Answer with JSON only: {{"thread": "<an existing id, or new>", "label": "<if new: 3-8 word topic label, in English>", "related": ["<ids of other threads it clearly connects to>"]}}
Pick an existing thread when the exchange continues that topic (also short follow-ups like "ok do it"
that continue the previous exchange). Use "new" only for a genuinely different topic."""


async def _ask_claude_json(prompt: str) -> dict:
    """One quick, tool-less Claude call through the CLI; returns the JSON object it answers with."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", TOPIC_MODEL, "--output-format", "json", "--tools", "",
        "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TOPIC_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    envelope = json.loads(out)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude: {str(envelope.get('result'))[:200]}")
    text = envelope.get("result") or ""
    m = re.search(r"\{.*\}", text, re.S)       # tolerate ```json fences or stray words around it
    if not m:
        raise ValueError(f"no JSON in topic answer: {text[:200]!r}")
    return json.loads(m.group(0))


async def _pick_thread(text: str, channel: str, said: str, now: float) -> tuple[str, list[str]]:
    """(thread id, related thread ids) for a new item — see the module docstring."""
    threads = _threads()
    last = _last.get(channel)
    last_thread = last["thread"] if last and now - last["ts"] < CONTINUE_WINDOW else ""
    recent = sorted(threads.items(), key=lambda kv: -kv[1].get("last", 0))[:RECENT_THREADS]
    try:
        listing = "\n".join(f"{tid}: {t.get('label', '')}" for tid, t in recent) or "(none yet)"
        prompt = TOPIC_PROMPT.format(threads=listing, last=last_thread or "(none)", text=text[:2500])
        ans = await _ask_claude_json(prompt)
        tid = str(ans.get("thread", "new")).strip()
        related = [x for x in ans.get("related", []) if x in threads and x != tid]
        if tid not in threads:
            tid = uuid.uuid4().hex[:8]
            threads[tid] = {"label": str(ans.get("label") or text[:60]).strip()[:80], "count": 0, "related": []}
    except Exception as e:
        log.warning("topic model failed (%r); threading by time only", e)
        related = []
        if last_thread and len(said.split()) <= SHORT_FOLLOWUP:
            tid = last_thread
        else:
            tid = uuid.uuid4().hex[:8]
            threads[tid] = {"label": (said or text)[:60].replace("\n", " "), "count": 0, "related": []}
    t = threads.setdefault(tid, {"label": (said or text)[:60], "count": 0, "related": []})
    t["last"], t["count"] = now, t.get("count", 0) + 1
    for rid in related:   # links go both ways
        t.setdefault("related", [])
        if rid not in t["related"]:
            t["related"].append(rid)
        other = threads[rid].setdefault("related", [])
        if tid not in other:
            other.append(tid)
    _save_threads(threads)
    return tid, related


async def add(text: str, kind: str, source: str = "", thread: str = "", said: str = "") -> str:
    """Store one memory item, linked into a thread, and return its id. `said`: Sanjar's own words
    (used to spot short follow-ups); `thread`: force a thread (e.g. one per call transcript)."""
    text = text.strip()[:4000]
    if not text:
        return ""
    vec = await _embed([text])
    now = time.time()
    channel = source or kind
    if not thread:
        thread, _ = await _pick_thread(text, channel, said or text, now)
    elif thread not in (th := _threads()):
        th[thread] = {"label": text[:60].replace("\n", " "), "last": now, "count": 1, "related": []}
        _save_threads(th)
    prev = _last.get(channel)
    it = {"id": uuid.uuid4().hex[:8], "ts": now, "kind": kind, "source": source, "text": text,
          "thread": thread, "prev": prev["id"] if prev else "",
          "vec": [round(x, 5) for x in vec[0]] if vec else None}
    _last[channel] = it
    _load().append(it)
    with ITEMS.open("a") as f:
        f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return it["id"]


def forget(item_id: str) -> bool:
    items = _load()
    if not any(it["id"] == item_id for it in items):
        return False
    with ITEMS.open("a") as f:
        f.write(json.dumps({"forget": item_id}) + "\n")
    _items[:] = [it for it in items if it["id"] != item_id]
    global _matrix
    _matrix = None
    return True


_WORD = re.compile(r"\w+", re.U)


def _keyword_scores(query: str, items: list[dict]) -> np.ndarray:
    q = {w for w in _WORD.findall(query.lower()) if len(w) > 2}
    if not q:
        return np.zeros(len(items))
    return np.array([len(q & set(_WORD.findall(it["text"].lower()))) / len(q) for it in items])


async def search(query: str, k: int = 6, kinds: tuple[str, ...] = ()) -> list[tuple[float, dict]]:
    """Best matching memories for `query`, as (score, item), best first."""
    items = _load()
    if not items or not query.strip():
        return []
    qv = await _embed([query[:2000]], query=True)
    if qv:
        v = np.asarray(qv[0], dtype=np.float32)
        scores = _vectors() @ (v / (np.linalg.norm(v) or 1))
        scores = scores + 0.5 * _keyword_scores(query, items) * (scores == 0)   # items stored offline
    else:
        scores = _keyword_scores(query, items)
    now = time.time()
    ranked = []
    for s, it in zip(scores, items):
        if kinds and it["kind"] not in kinds:
            continue
        s = float(s) + (0.05 if it["kind"] == "fact" else 0) + 0.02 * np.exp(-(now - it["ts"]) / (30 * 86400))
        ranked.append((s, it))
    ranked.sort(key=lambda p: -p[0])
    return ranked[:k]


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def format_items(hits: list[tuple[float, dict]]) -> str:
    return "\n".join(f"- [{it['kind']} {_when(it['ts'])} id={it['id']}] {it['text'][:700]}" for _, it in hits)


def threads_for(hits: list[tuple[float, dict]], max_threads: int = 3) -> list[tuple[str, list[dict]]]:
    """Expand search hits into their whole threads (plus each hit's direct neighbours, and threads
    linked as related), each in time order, best-matching first: [(label, items), ...]."""
    items = _load()
    meta = _threads()
    order, seen = [], set()
    for _, it in hits:
        t = it.get("thread") or it["id"]
        if t not in seen:
            seen.add(t)
            order.append((t, it))
    for t, hit in list(order[:max_threads]):     # one hop along "related" links
        for rid in meta.get(t, {}).get("related", []):
            if rid not in seen:
                seen.add(rid)
                latest = max((it for it in items if it.get("thread") == rid), key=lambda it: it["ts"], default=None)
                if latest:
                    order.append((rid, latest))
    out = []
    for t, hit in order[:max_threads + 2]:
        chain = sorted((it for it in items if (it.get("thread") or it["id"]) == t), key=lambda it: it["ts"])
        if len(chain) > THREAD_ITEMS:   # keep the hit and the latest ones
            keep = {hit["id"]} | {it["id"] for it in chain[-(THREAD_ITEMS - 1):]}
            chain = [it for it in chain if it["id"] in keep]
        out.append((meta.get(t, {}).get("label", ""), chain))
    return out


def format_threads(threads: list[tuple[str, list[dict]]], per_item: int = 500, total: int = 6000) -> str:
    parts, used = [], 0
    for label, chain in threads:
        head = (f"Thread «{label or 'untitled'}» ({len(chain)} items, "
                f"{_when(chain[0]['ts'])} → {_when(chain[-1]['ts'])}):")
        lines = [head] + [f"  - [{it['kind']} {_when(it['ts'])} id={it['id']}] "
                          f"{it['text'][:per_item].replace(chr(10), ' / ')}" for it in chain]
        block = "\n".join(lines)
        if used + len(block) > total and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _strong(hits: list[tuple[float, dict]], min_score: float, margin: float = 0.05) -> list[tuple[float, dict]]:
    """Hits above `min_score` and close to the best one: weak matches would drag whole unrelated
    threads in, since everything scores somewhat similar."""
    if not hits or hits[0][0] < min_score:
        return []
    return [h for h in hits if h[0] >= max(min_score, hits[0][0] - margin)]


async def recall_text(query: str, min_score: float = 0.60) -> str:
    hits = _strong(await search(query, k=8), min_score)
    return format_threads(threads_for(hits)) if hits else ""


async def context_for(text: str, k: int = 5, min_score: float = 0.70) -> str:
    """A memory block (whole linked threads) to put in front of Sanjar's message, or ""."""
    hits = _strong(await search(text, k), min_score)
    if not hits:
        return ""
    return ("[MEMORY — earlier conversations and notes on this, as linked threads (oldest first). Use "
            "them instead of guessing; if they don't answer it, use recall or say you don't know]\n"
            + format_threads(threads_for(hits, max_threads=2), total=4000) + "\n\n")


async def log_exchange(channel: str, said: str, reply: str) -> None:
    """Keep one exchange (Sanjar's words and Jarvis's reply) so later sessions can find it."""
    if said.strip() and reply.strip():
        await add(f"Sanjar: {said.strip()[:1800]}\nJarvis: {reply.strip()[:1800]}", channel, said=said)


def mark_indexed(filename: str) -> None:
    """A transcript whose turns were already logged live: don't index it again."""
    done = set(json.loads(INDEXED.read_text())) if INDEXED.exists() else set()
    done.add(filename)
    DIR.mkdir(parents=True, exist_ok=True)
    INDEXED.write_text(json.dumps(sorted(done)))


async def index_file(f) -> int:
    """Put one call transcript into memory, in chunks, and mark it done."""
    text = f.read_text(errors="replace")
    added = 0
    for i in range(0, len(text), 1500):
        chunk = text[i:i + 1500].strip()
        if chunk:
            await add(f"(call {f.stem}) {chunk}", "call", f.name, thread=f"call-{f.stem}")
            added += 1
    mark_indexed(f.name)
    return added


async def backfill_calls() -> int:
    """Index call transcripts saved before memory existed (and any missed ones), once each."""
    if not CALLS.exists():
        return 0
    done = set(json.loads(INDEXED.read_text())) if INDEXED.exists() else set()
    added = 0
    for f in sorted(CALLS.glob("*.txt")):
        if f.name not in done:
            added += await index_file(f)
    if added:
        log.info("indexed %d chunks of old call transcripts into memory", added)
    return added
