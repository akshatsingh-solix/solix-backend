"""The in-process cache must not grow without bound: it ran the API out of memory on Render's 512 MB instance."""
import asyncio

import cache


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_invalidation_drops_old_copies_immediately():
    cache.clear()

    async def go():
        for i in range(50):  # every account change bumps the namespace, then a page load fills it again
            await cache.memo("radar_companies", "all", 15, lambda: _big())
            cache.bump("radar_companies")
        await cache.memo("radar_companies", "all", 15, lambda: _big())
    run(go())
    assert cache.size().get("radar_companies") == 1


def test_expired_entries_are_pruned():
    cache.clear()

    async def go():
        await cache.memo("x", "a", 0.0, lambda: _big())
        await cache.memo("y", "b", 60, lambda: _big())
    run(go())
    assert "x" not in cache.size() and cache.size()["y"] == 1


def test_concurrent_callers_share_one_load():
    cache.clear()
    calls = []

    async def produce():
        calls.append(1)
        await asyncio.sleep(0.05)
        return [1] * 1000

    async def go():
        return await asyncio.gather(*[cache.memo("radar_companies", "all", 15, produce) for _ in range(10)])
    out = run(go())
    assert len(calls) == 1 and all(o is out[0] for o in out)


def test_a_failed_load_is_not_cached_and_waiters_see_the_error():
    cache.clear()

    async def boom():
        await asyncio.sleep(0.01)
        raise RuntimeError("db down")

    async def go():
        return await asyncio.gather(*[cache.memo("n", "k", 15, boom) for _ in range(3)], return_exceptions=True)
    out = run(go())
    assert all(isinstance(o, RuntimeError) for o in out) and "n" not in cache.size()


async def _big():
    return [{"id": i, "text": "x" * 100} for i in range(100)]
