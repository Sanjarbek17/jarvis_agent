"""Jarvis's long-term memory: facts and past conversations that survive restarts, /clear and new calls.

Everything lives in var/state/memory/items.jsonl (one item per line, with its embedding). Kinds:
  fact  - something Sanjar told Jarvis to keep: a preference, decision, name, plan ("remember" tool)
  chat  - one Telegram exchange (his message + Jarvis's reply), logged automatically
  call  - one turn of a call, logged automatically; old call transcripts are indexed once at start
Search is by meaning (Gemini embeddings, works across Uzbek / Russian / English), with a keyword
fallback when embeddings are unavailable, so nothing breaks offline.
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


async def add(text: str, kind: str, source: str = "") -> str:
    """Store one memory item and return its id."""
    text = text.strip()[:4000]
    if not text:
        return ""
    vec = await _embed([text])
    it = {"id": uuid.uuid4().hex[:8], "ts": time.time(), "kind": kind, "source": source, "text": text,
          "vec": [round(x, 5) for x in vec[0]] if vec else None}
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


async def context_for(text: str, k: int = 5, min_score: float = 0.70) -> str:
    """A memory block to put in front of Sanjar's message, or "" if nothing relevant."""
    hits = [h for h in await search(text, k) if h[0] >= min_score]
    if not hits:
        return ""
    return ("[MEMORY — notes and past conversations that may be relevant. Use them instead of guessing; "
            "if they don't answer it, use recall or say you don't know]\n" + format_items(hits) + "\n\n")


async def log_exchange(channel: str, said: str, reply: str) -> None:
    """Keep one exchange (Sanjar's words and Jarvis's reply) so later sessions can find it."""
    if said.strip() and reply.strip():
        await add(f"Sanjar: {said.strip()[:1800]}\nJarvis: {reply.strip()[:1800]}", channel)


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
            await add(f"(call {f.stem}) {chunk}", "call", f.name)
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
