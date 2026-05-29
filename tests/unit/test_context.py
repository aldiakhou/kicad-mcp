from kicad_mcp.context import BoundedCache


def test_bounded_cache_evicts_least_recently_used_entry():
    cache = BoundedCache(max_entries=2)
    cache["first"] = 1
    cache["second"] = 2

    assert cache["first"] == 1

    cache["third"] = 3

    assert "first" in cache
    assert "second" not in cache
    assert "third" in cache
