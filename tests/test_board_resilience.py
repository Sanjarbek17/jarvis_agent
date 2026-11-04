import asyncio
import time
from types import SimpleNamespace as NS

from core import board, ghboard


def test_one_unreadable_board_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(board, "boards", lambda: {"Good": "db1", "AI English Teacher": "github:x/y"})

    async def query(project, filter_=None, sorts=None):
        if project == "AI English Teacher":
            raise RuntimeError("gh project item-list 4: unknown owner type")
        return [NS(ref="g1", project="Good")]

    monkeypatch.setattr(board, "_query", query)
    assert [t.ref for t in asyncio.run(board.queued())] == ["g1"]


def test_github_board_serves_last_copy_when_github_refuses(monkeypatch):
    monkeypatch.setattr(ghboard, "projects", lambda: {"P": {"repo": "x/y", "owner": "o", "project": 4, "prefix": "p"}})
    stale = [NS(ref="p-1")]
    ghboard._cache["P"] = (time.monotonic() - 999, stale)        # older than CACHE_SECONDS
    ghboard._backoff.pop("P", None)
    calls = []

    async def gh(*args, check=True):
        calls.append(args)
        raise RuntimeError("API rate limit already exceeded")

    monkeypatch.setattr(ghboard, "_gh", gh)
    assert asyncio.run(ghboard.tickets("P")) is stale
    assert asyncio.run(ghboard.tickets("P")) is stale            # backing off: GitHub not asked again
    assert len(calls) == 1
    ghboard._cache.pop("P", None)
