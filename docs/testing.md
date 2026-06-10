# Testing strategy

> Status: **adopted; first suite built.** Tiers 1–2 (35 fast tests) and the Tier-3 live canaries
> (`tests/test_live.py`) are implemented in `tests/` and green; a **pre-push hook** runs the fast
> suite on every push. Tier 4 is the manual checklist. Goal: **prevent silent breakage from
> changes** without drowning in trivial tests or burning API/LLM credits. See
> [`../tests/README.md`](../tests/README.md) for the inventory and
> [**Adding tests as you build**](#adding-tests-as-you-build) for the ongoing plan.

## Running the tests

```bash
pip install -e ".[dev]"     # pytest, pytest-asyncio, respx
pytest                       # Tiers 1–2: fast, free, deterministic (live canaries skipped by default)
pytest -m live               # Tier 3 canaries — opt-in, needs keys + network, a few ¢
```

Default `pytest` needs no API keys and no network. (The `[voice]`/`google-genai` deps make the
Tier-2 signature checks in `test_contracts.py` meaningful; without them those tests skip.)

## Philosophy

**Test what breaks, not what's easy.** Coverage percentage is a non-goal. We invest where failure
is (a) likely and (b) silent or expensive to discover. Everything here is justified by something
that *actually broke* this project, not by ritual.

### Where this project actually breaks (evidence from real incidents)

| Breakage source | Real example this build | Cheap to catch? |
|---|---|---|
| **External API / CDN drift** | PipeCat 1.x moved VAD to a processor, renamed the context; Poly Pizza's CDN started returning a Cloudflare 403; Gemini's flash model silently ignores `image_size` | Partly — import/signature checks are ~free; behavior drift needs occasional *live* checks |
| **Complex internal logic** | `_normalize` produced wrong scale when bbox was missing; patch/inverse + dotted-path application is fiddly | **Yes** — pure, deterministic, fast unit tests |
| **Our own contract seams** | MCP tool → server endpoint payloads; the patch shapes the JS client depends on; placeholder→swap flow | **Yes** — in-process integration tests with the real server + fake externals |
| **The asset bug itself** | A failed download (403 HTML) was saved as a `.glb`, cached, and served → invisible, no error | **Yes** — a 10-line integration test (mock a 403) would have caught the *code*; a live canary would have caught the *CDN change* |
| **LLM nondeterminism** | The director picking the wrong tool / bad args | **No** (don't assert exact output) — test the plumbing it calls; loose, opt-in evals only |
| **Visual / WebGL rendering** | Skybox texture not appearing; holodeck grid; model placement *look* | **No** (expensive/flaky to automate) — manual visual checklist |

The takeaway: **most of our value is cheap, deterministic tests of internal logic and our own
seams, plus a thin layer of opt-in live "canaries" for the external integrations that drift.** LLM
behavior and visual fidelity are deliberately *not* gated by automated tests.

## The layers (and what runs when)

We tier tests by **cost**, because the expensive layers (live APIs, LLMs, torch) can't run on every
change.

### Tier 1 — fast / free / deterministic ✦ run on every change
No network, no API keys, no LLM, no torch. Seconds to run. **~90% of the value.**

- **Unit — the complex, foundational, silent-if-wrong logic:**
  - `world.py`: patch application (add/update/remove/env), **inverse/undo correctness**,
    dotted-path set on nested/missing keys, `rev` monotonicity, replacing an existing entity.
  - `server.py` `_normalize`: scale-to-target-size + base-on-floor math, and the no-bbox fallback
    (the exact thing that silently mis-scaled models).
  - `schema.py`: patch-op discriminated union, World/Entity validate + round-trip.
  - `llm.py` image side: capability-aware generator registry + `select_generator` mediation;
    image-bytes extraction from *fake* Gemini/OpenAI response objects (no API call).
  - `assets.py` **validation logic**: given a fake HTTP response, assert it **rejects non-GLB /
    a 403 body**, accepts a valid GLB, handles empty search results, and self-heals a poisoned
    cache. *(This is the regression test for the bug we just fixed.)*
- **Integration — our own seams, with externals faked:**
  - FastAPI `TestClient` against the **real world server** with a **fake asset resolver / fake
    image generator** injected: `POST /place_asset` → placeholder patch *then* swap patch; failure
    path → placeholder removed + `ok:false` (no garbage entity); `/place_image`, `/edit_image`,
    `/set_skybox`, `/outpaint_image` produce the expected patches/material/env.
  - **WebSocket broadcast**: connect a test client, POST a patch, assert it's broadcast with the
    shape the JS client consumes.
  - **MCP ↔ server contract**: the MCP tools POST specific payloads to specific endpoints — assert
    they match (drift here = silent breakage).

### Tier 2 — library contract checks ✦ run on every change *(deps installed)* / on dependency bumps
Import + signature assertions for the external SDK classes we depend on — **no API calls, ~free.**
This is the cheap catch for the PipeCat-1.x-style drift that bit us repeatedly: assert
`VADProcessor`, `LLMContext`, `LLMContextAggregatorPair`, `WhisperSTTService.Settings`,
`KokoroTTSService` (`.tts`), `AnthropicLLMService(settings=...)`, the `google-genai`
`generate_content`/`ImageConfig` surface, and `mcp` types still exist with the shapes we call.
(These need the `[voice]`/genai deps but **no keys** and **no network**.)

### Tier 3 — live external "canaries" ✦ opt-in, cost-bounded — **built (`tests/test_live.py`)**
The whole point is detecting when an external service *changes*, so these can't be mocked. Marked
`@pytest.mark.live` and skipped by default; run with **`pytest -m live`**. Each skips if its key is
missing. Strictly minimal:
- Poly Pizza: search returns the expected fields **and a real download is a valid GLB**
  *(the test that would have flagged the Cloudflare 403 before a user hit it)*.
- Gemini: one **cheap** (flash, 1K) generate decodes to an image; the configured image/skybox
  **models still exist** (catches deprecation).
- Director eval: optionally one short turn (cheap model) — see LLM section. *(Not written yet.)*
Cost target: a few cents per run. No 4K skyboxes, no long LLM loops.

### Tier 4 — manual / visual checklist ✦ human, before releases or risky client changes
Not automated. A short documented checklist: headset loads over USB/HTTPS; a model places to-scale
on the floor; a painting renders; a skybox wraps; voice does one round-trip. Automating WebGL
visual correctness (does the skybox *look* right) is high-effort and flaky — a checklist is the
pragmatic call.

## Mocking strategy

The architecture already gives us clean seams to inject fakes — **use them instead of patching:**
- **Image generation**: `get_image_generator(settings)` returns a provider from a registry → inject
  a `FakeImageGenerator` returning a tiny fixed PNG. No Gemini, no key, deterministic.
- **Assets**: `AssetResolver` is constructed and held on the server module → inject a fake (or mock
  at the `httpx` boundary with a transport that returns canned 200-GLB / 403-HTML responses).
- **World server**: Starlette `TestClient` runs it in-process — no real ports, supports WebSocket
  testing.
- Mock **at the external boundary** (httpx transport) or **at our own interface** (fake
  resolver/generator) — *not* deep internals. Prefer injecting fakes; fall back to `httpx`
  `MockTransport`/`respx` for the resolver's raw HTTP.

## The LLM-nondeterminism question (answered)

- **Do not** assert exact director output — it's nondeterministic, costs credits, and is brittle.
- **Do** test everything *around* the LLM deterministically: the MCP tool schemas are valid and
  registered; each tool, given inputs, produces the right world patch; endpoints behave. The LLM is
  a black box that calls tools — we make the tools provably correct and let the LLM be the LLM.
- **Loose evals (optional, Tier 3):** a *handful* of prompts checked *loosely* and tolerantly — e.g.
  "add a red cube" → some `add_entity` call happened; "put a tree" → `place_asset` with a `size_m`.
  Graded as "did a reasonable tool fire," not exact args. Low N, cheap model, run manually as
  monitoring — **never a merge gate.**

## End-to-end

E2E without an LLM is cheap and worth it: **the CLI's direct commands are a deterministic driver.**
Spin up the server, run `conjure-cli add box` / `asset` (with a fake resolver) / etc., assert
`/world`. The full LLM path (`say "add a cube"` → `add_entity` fired) is a Tier-3 opt-in.

## What we deliberately **don't** test

- Trivial getters, pydantic field access, config plumbing.
- Exact LLM responses; voice TTS/STT audio internals (PipeCat real-time pipeline) — rely on Tier-2
  signature checks + the Tier-4 manual voice check.
- WebGL/visual fidelity — manual checklist.
- Aiming for a coverage number.

## The suite, by value (all built — inventory in [`../tests/README.md`](../tests/README.md))

1. `world.py` patch + inverse + dotted-path (pure, foundational — everything rides on it).
2. `assets.py` download validation: **403/HTML rejected**, valid GLB accepted, empty results,
   poisoned-cache self-heal *(direct regression for the bug that started this)*.
3. `place_asset` / `place_image` endpoint integration with fakes: placeholder→swap, and the
   **failure path leaves no garbage entity**.
4. `_normalize` scale/placement math incl. the no-bbox fallback.
5. Tier-2 library signature checks for PipeCat + google-genai + mcp (catches SDK drift ~free).
6. WebSocket broadcast shape; MCP-tool→endpoint payload contract.

That set is small, fast, free, and covers the failure modes we've actually hit. As of the first
build: **35 fast tests (~3 s) + 4 opt-in live canaries**, green.

## Tooling & layout

- **`pytest` + `pytest-asyncio`** (async endpoints), Starlette **`TestClient`**, and **`respx`** (or
  `httpx.MockTransport`) for HTTP — add `respx` to the `[dev]` extra.
- `tests/` mirrors `conjure/`; fixtures: a tiny valid GLB, a sample world, a fake Gemini response,
  `FakeImageGenerator`/`FakeAssetResolver`.
- Markers: `@pytest.mark.live` (Tier 3) — default `pytest` skips them (`addopts = -m 'not live'`);
  `pytest -m live` runs them, each skipping if its key is absent.
- **Pre-push hook (installed):** `scripts/git-hooks/pre-push` runs the fast suite and blocks a
  failing push; wired up by `scripts/setup.sh` via `git config core.hooksPath scripts/git-hooks`.
  Bypass once with `git push --no-verify`.
- **CI (optional, later):** a GitHub Action could run **Tier 1 only** on push (no keys, no torch —
  these tests are dependency-light and don't need the `[voice]` stack); Tier 2 where the heavier
  deps are installed; Tier 3 manual/scheduled. The pre-push hook covers the solo-dev case for now.

## Cost summary

| Tier | Network | Keys | LLM/$ | When |
|---|---|---|---|---|
| 1 unit + integration | none | none | none | every change (CI) |
| 2 library signatures | none | none | none | every change (deps present) / dep bumps |
| 3 live canaries | yes | yes | few ¢ | opt-in: manual / nightly |
| 4 manual visual | — | — | — | before releases / risky client changes |

Default `pytest` = Tiers 1–2: fast, free, deterministic, and would have caught the asset bug's
*code* defect, the PipeCat API drift, and the normalization bug. The Cloudflare/CDN *environment*
change is exactly what the opt-in Tier-3 canary exists to surface early.

## Adding tests as you build

The rule of thumb stays the same as the strategy above: **test what would silently break and waste
your time, not what's obvious.** Concretely, when you add a feature, reach for the matching row —
not every row. A new primitive type needs no test; a new external dependency or a new piece of
math does.

| When you add… | Add this test (tier) | Why |
|---|---|---|
| a new **patch op** or world-doc field | apply **+ inverse** round-trip, dotted-path (1) | undo and broadcast both ride on the inverse being exact |
| a new **server endpoint** | happy path + **failure leaves no garbage entity**, with fakes (1) | the asset bug was a failure path that wrote junk |
| a new **external provider** (asset source, image/STT/TTS/LLM) | mocked unit test of the adapter **+ one Tier-3 canary** (real call) | the canary is the only thing that catches an API/CDN change |
| **scaling / placement / geometry** math | a worked example incl. the **degenerate case** (no bbox, zero size) (1) | silent off-by-one puts objects underground or 100× too big |
| a tool the **director (LLM) calls** | MCP tool → endpoint **payload contract** with respx (1) | a renamed field fails silently mid-conversation |
| code pinned to a **3rd-party SDK signature** (PipeCat, google-genai, mcp) | a Tier-2 `importorskip` signature check (2) | catches dependency drift on `pip install -U`, ~free |
| a **parser / state machine / non-trivial pure function** (e.g. Z-machine, QuickJS glue) | focused unit tests at the edges (1) | complex logic is where unit tests earn their keep |
| a new **client/WebXR render path** | a Tier-4 line in the manual checklist | WebGL/headset output isn't worth automating yet |

Conventions to keep the suite cheap and honest:

- **Mock the boundary, not the logic.** Hit the real HTTP/SDK seam with `respx`/fakes; let your own
  code run for real. Reuse `FakeImageGenerator` / `FakeAssetResolver` from `conftest.py`.
- **One canary per service, and keep it cheap** — cheapest model, smallest size, gated on its key.
  Never a 4K skybox or an LLM loop in the default or live run.
- **Regression-first:** when a bug slips through, the fix lands *with* the test that would have
  caught it (as the asset bug did). That test is the durable part of the fix.
- **Don't chase coverage.** A trivial test that only restates the code is noise; delete it.
- Keep Tier-1/2 tests **out of the `[voice]` stack** (no torch import) so they stay seconds-fast and
  CI-able without the heavy deps.
