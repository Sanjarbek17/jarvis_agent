import asyncio
import time
from types import SimpleNamespace as NS

from core import board, ghboard


def test_one_unreadable_board_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(board, "boards", lambda: {"Good": "db1", "AI English Teacher": "github:x/y"})

    async def query(project, filter_=None):
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


def _page(pid, jarvis="Queued", edited="2026-11-05T10:00:00.000Z"):
    return {"id": pid, "url": f"https://notion.so/{pid}", "last_edited_time": edited, "properties": {
        "Name": {"type": "title", "title": [{"plain_text": pid}]},
        "Jarvis": {"type": "select", "select": {"name": jarvis} if jarvis else None}}}


def _fake_notion(monkeypatch, pages):
    """A board that counts its reads; set .fail to make them fail."""
    state = NS(reads=0, fail=False, on_read=None)

    async def post(url, json=None):
        assert "filter" not in json
        state.reads += 1
        if state.on_read:
            state.on_read()
        if state.fail:
            raise RuntimeError("502 Bad Gateway")
        return NS(raise_for_status=lambda: None, json=lambda: {"results": list(pages), "has_more": False})

    async def patch(url, json=None):
        pid = url.rsplit("/", 1)[1]
        return NS(raise_for_status=lambda: None,
                  json=lambda: _page(pid, (json["properties"]["Jarvis"]["select"] or {}).get("name")))

    monkeypatch.setattr(board, "boards", lambda: {"P": "db1"})
    monkeypatch.setattr(board, "_http", NS(post=post, patch=patch))
    monkeypatch.setattr(board, "_cache", {})
    monkeypatch.setattr(board, "_writes", {})
    return state


def test_notion_board_is_read_once_per_minute(monkeypatch):
    calls = _fake_notion(monkeypatch, [_page("a"), _page("b", "Working")])

    async def checks():
        for _ in range(5):
            assert [t.ref for t in await board.queued()] == ["a"]
        assert [t.ref for t in await board.with_jarvis("Working")] == ["b"]

    asyncio.run(checks())
    assert calls.reads == 1
    board._cache["P"] = (time.monotonic() - board.CACHE_SECONDS - 1, board._cache["P"][1])
    asyncio.run(board.queued())
    assert calls.reads == 2


def test_a_ticket_jarvis_changed_is_not_queued_in_the_cache(monkeypatch):
    calls = _fake_notion(monkeypatch, [_page("a")])

    async def run():
        t = (await board.queued())[0]
        await board.update(t, jarvis="Working")
        return await board.queued(), await board.with_jarvis("Working")

    queued, working = asyncio.run(run())
    assert not queued and [t.ref for t in working] == ["a"]
    assert calls.reads == 1


def test_a_write_during_a_read_does_not_get_cached_away(monkeypatch):
    calls = _fake_notion(monkeypatch, [_page("a")])
    calls.on_read = lambda: board._writes.update(P=1)
    asyncio.run(board.queued())
    calls.on_read = None
    asyncio.run(board.queued())
    assert calls.reads == 2


def test_notion_failure_serves_the_last_copy_and_waits_before_asking_again(monkeypatch):
    calls = _fake_notion(monkeypatch, [_page("a")])
    asyncio.run(board.queued())
    calls.fail = True
    board._cache["P"] = (time.monotonic() - board.CACHE_SECONDS - 1, board._cache["P"][1])
    assert [t.ref for t in asyncio.run(board.queued())] == ["a"]
    assert [t.ref for t in asyncio.run(board.queued())] == ["a"]
    assert calls.reads == 2


def test_find_any_rereads_the_board_for_a_ticket_it_does_not_know(monkeypatch):
    pages = [_page("a")]
    calls = _fake_notion(monkeypatch, pages)
    asyncio.run(board.queued())
    pages.append(_page("b", jarvis=None))
    assert asyncio.run(board.find_any("b")).ref == "b"
    assert calls.reads == 2


def test_recently_finished_filter_works_on_cached_notion_tickets(monkeypatch):
    _fake_notion(monkeypatch, [_page("old", "Done", "2020-01-01T00:00:00.000Z"),
                               _page("new", "Done", "2999-01-01T00:00:00.000Z")])
    assert "new" in asyncio.run(board.status_text()) and "old" not in asyncio.run(board.status_text())
