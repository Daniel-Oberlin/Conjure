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
    with pytest.raises(RuntimeError, match="refused the download.*by hand"):    # says what to do about it
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


@respx.mock
async def test_a_model_on_disk_is_reused_even_when_the_index_lost_it(tmp_path):
    # 2026-11-03: index.json held 4 of 27 downloads; a Beagle on disk was fetched again and the source
    # answered 403, so a model already in the library could not be placed. The sidecar is the record.
    (tmp_path / "beagle.glb").write_bytes(b"glTF" + bytes(40))
    (tmp_path / "beagle.json").write_text(json.dumps({
        "hash": "beagle", "title": "Beagle", "attribution": "", "licence": "", "creator": "", "tris": 712,
        "source_url": DL, "bbox_min": None, "bbox_max": None}))
    (tmp_path / "index.json").write_text(json.dumps({}))

    respx.get(SEARCH).mock(return_value=_search_response([{"Title": "Beagle", "Download": DL}]))
    blocked = respx.get(DL).mock(return_value=httpx.Response(403, html="blocked"))
    rec = await AssetResolver("key", tmp_path).resolve("tree")
    assert rec.hash == "beagle" and not blocked.called


# --- The local collection (docs/specs/library.md §3a). Poly Pizza's file host refuses automated downloads
# (Cloudflare Bot Fight Mode, free tier — confirmed by its owner 2026-11-17), so models are downloaded by
# hand into a folder of loose GLBs and pack zips, and resolved from there first. ---

import random
import zipfile

from conjure.assets import LocalCollection

GLB = b"glTF" + bytes(40)


def _collection(root):
    (root / "Tree by Quaternius - 2paAm1ja4w.glb").write_bytes(GLB + b"tree")
    with zipfile.ZipFile(root / "Animal Kit.undefined-glb.zip", "w") as zf:
        zf.writestr("Beagle.glb", GLB + b"beagle")
        zf.writestr("Cow.glb", GLB + b"cow-a")
        zf.writestr("Couch Small.glb", GLB + b"couch")
        zf.writestr("Car.glb", GLB + b"car")
        zf.writestr("Sports Car.glb", GLB + b"sports")
        zf.writestr("Bird-h5IzAUdltz.glb", GLB + b"bird")
    with zipfile.ZipFile(root / "Farm Animal Pack-glb.zip", "w") as zf:
        zf.writestr("Cow.glb", GLB + b"cow-b")
    with zipfile.ZipFile(root / "Vine Covered Tree by Z - dlW4hGKBpiS.zip", "w") as zf:
        zf.writestr("model.obj", "v 0 0 0")                       # OBJ-only: ignored
    return root


def test_the_collection_reads_loose_files_and_packs_and_skips_what_is_not_glb(tmp_path):
    c = LocalCollection(_collection(tmp_path))
    by = {(m.title, m.pack): m for m in c.models()}
    tree = by[("Tree", "")]
    assert (tree.creator, tree.pp_id) == ("Quaternius", "2paAm1ja4w")
    assert by[("Bird", "Animal Kit")].pp_id == "h5IzAUdltz", "a duplicate's id suffix is the Poly Pizza id"
    assert ("Cow", "Farm Animal Pack") in by
    assert not any("model" in m.title for m in c.models()), "the OBJ-only zip is skipped"


def test_names_match_exact_first_then_partial_and_plurals_fold(tmp_path):
    c = LocalCollection(_collection(tmp_path))
    assert c.find("beagle").title == "Beagle"
    assert c.find("Beagles").title == "Beagle"
    assert c.find("couch").title == "Couch Small", "the query inside a title"
    assert c.find("a big brown beagle").title == "Beagle", "a title inside the query"
    assert c.find("a red sports car").title == "Sports Car", "more of the query covered wins"
    assert c.find("giraffe") is None


def test_equally_good_matches_are_chosen_at_random_each_time(tmp_path):
    c = LocalCollection(_collection(tmp_path), rng=random.Random(1))
    packs = {c.find("cow").pack for _ in range(40)}
    assert packs == {"Animal Kit", "Farm Animal Pack"}


async def test_a_local_model_is_extracted_once_with_an_honest_credit_and_no_network(tmp_path):
    cache = tmp_path / "cache"
    r = AssetResolver(None, cache, local_dir=_collection(tmp_path))
    rec = await r.resolve("beagle")                                # respx not active: any request would fail
    assert (cache / f"{rec.hash}.glb").read_bytes() == GLB + b"beagle"
    assert rec.attribution == '"Beagle" from the Animal Kit pack on Poly Pizza'
    assert rec.licence == "", "a pack names no licence; blank beats guessed"
    again = await r.resolve("beagle")
    assert again.hash == rec.hash and len(list(cache.glob("*.glb"))) == 1


async def test_new_and_replaced_files_are_picked_up_without_a_restart(tmp_path):
    root, cache = _collection(tmp_path), tmp_path / "cache"
    r = AssetResolver(None, cache, local_dir=root)
    with pytest.raises(RuntimeError, match="nothing called 'giraffe'"):
        await r.resolve("giraffe")
    (root / "Giraffe by X - aaaaaaaaaa.glb").write_bytes(GLB + b"g1")
    first = await r.resolve("giraffe")
    import os
    p = root / "Giraffe by X - aaaaaaaaaa.glb"
    p.write_bytes(GLB + b"g2-replaced")
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10_000_000))
    second = await r.resolve("giraffe")
    assert first.hash != second.hash, "a replaced file is re-extracted, not served from the old copy"


@respx.mock
async def test_poly_pizza_search_bridges_a_synonym_onto_the_collection(tmp_path):
    respx.get(SEARCH_URL.format(query="dog")).mock(return_value=_search_response([
        {"ID": "zzz", "Title": "Labrador", "Download": DL},                         # not held
        {"ID": "qqq", "Title": "Beagle", "Download": DL, "Licence": "CC0", "Tri Count": 712,
         "Attribution": '"Beagle" by Quaternius', "Creator": {"Username": "Quaternius"}}]))
    dl = respx.get(DL).mock(return_value=httpx.Response(403, html="blocked"))
    rec = await AssetResolver("key", tmp_path / "cache", local_dir=_collection(tmp_path)).resolve("dog")
    assert rec.title == "Beagle" and rec.licence == "CC0", "the search result supplies the real credit"
    assert not dl.called


@respx.mock
async def test_when_nothing_is_held_the_error_says_why(tmp_path):
    respx.get(SEARCH_URL.format(query="giraffe")).mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(RuntimeError, match="not|nothing called 'giraffe'.*search failed"):
        await AssetResolver("key", tmp_path / "cache", local_dir=_collection(tmp_path)).resolve("giraffe")
