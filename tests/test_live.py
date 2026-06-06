"""Tier 3 — live canaries. Opt-in only: `pytest -m live` (the default run skips them).

These hit real external APIs *minimally* to catch drift the mocked tests can't: API-shape changes,
model deprecation, auth/quota, and the kind of CDN block that made models invisible. Each skips if
its key is missing. Cost: a few cents (one cheap flash image); never a 4K skybox or an LLM loop.
"""

import io

import httpx
import pytest
from PIL import Image

from conjure.config import get_settings

pytestmark = pytest.mark.live
S = get_settings()


@pytest.mark.skipif(not S.poly_pizza_api_key, reason="no POLY_PIZZA_API_KEY")
def test_poly_pizza_search_shape():
    """The search API still returns the fields the resolver reads."""
    from conjure.assets import SEARCH_URL

    r = httpx.get(SEARCH_URL.format(query="tree"), headers={"x-auth-token": S.poly_pizza_api_key},
                  params={"Limit": 1}, timeout=20)
    r.raise_for_status()
    item = r.json()["results"][0]
    for field in ("Title", "Download", "Licence", "Tri Count", "Creator"):
        assert field in item, f"Poly Pizza search result missing {field!r}"


@pytest.mark.skipif(not S.poly_pizza_api_key, reason="no POLY_PIZZA_API_KEY")
async def test_poly_pizza_downloads_a_real_glb(tmp_path):
    """The canary that surfaces a CDN block: a real model download must be a valid GLB, not an
    error page. (Currently expected to FAIL while Poly Pizza's CDN is Cloudflare-challenging us —
    that failure is the canary doing its job.)"""
    from conjure.assets import AssetResolver

    rec = await AssetResolver(S.poly_pizza_api_key, tmp_path).resolve("tree")
    assert rec is not None
    assert (tmp_path / f"{rec.hash}.glb").read_bytes()[:4] == b"glTF"


@pytest.mark.skipif(not S.google_api_key, reason="no GOOGLE_API_KEY")
async def test_gemini_generates_a_decodable_image():
    """One cheap flash generation actually returns a usable image."""
    from conjure.imagegen import get_image_generator

    res = await get_image_generator(S).generate("a small red circle on a white background", image_size="1K")
    img = Image.open(io.BytesIO(res.data))
    assert img.size[0] > 0 and img.size[1] > 0


@pytest.mark.skipif(not S.google_api_key, reason="no GOOGLE_API_KEY")
def test_configured_gemini_models_exist():
    """The image + skybox models we're configured to use are still offered (catches deprecation)."""
    from google import genai

    client = genai.Client(api_key=S.google_api_key)  # keep a ref alive during pagination
    names = {m.name.split("/")[-1] for m in client.models.list()}
    assert S.image_model in names, f"image_model {S.image_model!r} not available"
    assert S.skybox_model in names, f"skybox_model {S.skybox_model!r} not available"
