"""AssetResolver tests — the regression suite for the bug that started all this:
a failed/garbage download must never be cached or returned as a model."""

import json

import httpx
import pytest
import respx

from conjure.assets import SEARCH_URL, AssetResolver

SEARCH = SEARCH_URL.format(query="tree")
DL = "https://static.poly.pizza/x.glb"


def _search_response(results):
    return httpx.Response(200, json={"results": results})


@respx.mock
async def test_accepts_valid_glb(tmp_path):
    respx.get(SEARCH).mock(return_value=_search_response([{"Title": "Tree", "Download": DL, "Tri Count": 100,
                                                           "Licence": "CC-BY", "Attribution": "a", "Creator": {"Username": "u"}}]))
    respx.get(DL).mock(return_value=httpx.Response(200, content=b"glTF" + bytes(40)))
    rec = await AssetResolver("key", tmp_path).resolve("tree")
    assert rec is not None and rec.title == "Tree"
    assert (tmp_path / f"{rec.hash}.glb").read_bytes()[:4] == b"glTF"


@respx.mock
async def test_rejects_403_and_caches_nothing(tmp_path):
    respx.get(SEARCH).mock(return_value=_search_response([{"Title": "T", "Download": DL}]))
    respx.get(DL).mock(return_value=httpx.Response(403, html="<!DOCTYPE html><html>blocked</html>"))
    with pytest.raises(httpx.HTTPStatusError):
        await AssetResolver("key", tmp_path).resolve("tree")
    assert not list(tmp_path.glob("*.glb"))  # nothing poisoned the cache


@respx.mock
async def test_rejects_200_html_body(tmp_path):
    # Some CDNs serve an error page with a 200; the GLB magic check must still reject it.
    respx.get(SEARCH).mock(return_value=_search_response([{"Title": "T", "Download": DL}]))
    respx.get(DL).mock(return_value=httpx.Response(200, content=b"<!DOCTYPE html>"))
    with pytest.raises(RuntimeError):
        await AssetResolver("key", tmp_path).resolve("tree")
    assert not list(tmp_path.glob("*.glb"))


@respx.mock
async def test_empty_results_returns_none(tmp_path):
    respx.get(SEARCH).mock(return_value=_search_response([]))
    assert await AssetResolver("key", tmp_path).resolve("tree") is None


@respx.mock
async def test_poisoned_cache_self_heals(tmp_path):
    # Pre-seed a poisoned entry (HTML saved as .glb) like the old bug produced.
    (tmp_path / "bad.glb").write_bytes(b"<!DOCTYPE html>")
    (tmp_path / "bad.json").write_text(json.dumps({
        "hash": "bad", "title": "Stale", "attribution": "", "licence": "", "creator": "", "tris": 0,
        "source_url": DL, "bbox_min": None, "bbox_max": None}))
    (tmp_path / "index.json").write_text(json.dumps({DL: "bad"}))

    respx.get(SEARCH).mock(return_value=_search_response([{"Title": "Good", "Download": DL}]))
    respx.get(DL).mock(return_value=httpx.Response(200, content=b"glTF" + bytes(40)))
    rec = await AssetResolver("key", tmp_path).resolve("tree")
    assert rec.title == "Good"  # re-fetched, not the poisoned "Stale"
    assert (tmp_path / f"{rec.hash}.glb").read_bytes()[:4] == b"glTF"
