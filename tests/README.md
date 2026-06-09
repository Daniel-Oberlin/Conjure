# Tests

```bash
pip install -e ".[dev]"     # pytest, pytest-asyncio, respx
pytest                       # fast suite — no keys, no network, ~3s (live canaries skipped)
pytest -m live               # Tier-3 canaries — opt-in, need keys + network, a few ¢
```

A **pre-push git hook** runs `pytest` and blocks a failing push (installed by `scripts/setup.sh`
via `git config core.hooksPath scripts/git-hooks`; bypass once with `git push --no-verify`).

## What's here

| File | Covers |
|---|---|
| `test_world.py` | patch protocol: apply / inverse / dotted-path / env (the foundation) |
| `test_director.py` | director routing (switch / address an LLM mid-conversation) + orchestration with fake LLMs |
| `test_llm.py` | LLM roster: attribution rendering, Claude/Gemini tool-call loops (faked SDKs), registry |
| `test_assets.py` | `AssetResolver` download validation — **regression for the 403-HTML-as-GLB bug** |
| `test_server.py` | world-server endpoints w/ faked externals: normalization, placeholder→swap, failure-leaves-no-garbage, place/edit image, skybox, route + WS-broadcast contracts |
| `test_imagegen.py` | generator registry + Gemini response extraction + per-call model override |
| `test_contracts.py` | Tier-2 SDK signature checks (PipeCat / google-genai / mcp drift) |
| `test_mcp.py` | MCP tool → server-endpoint payload contracts |
| `test_live.py` | **Tier-3 canaries (opt-in)**: Poly Pizza search shape + real GLB download; Gemini generate + configured-models-exist |

Fakes/fixtures are in `conftest.py` (`FakeImageGenerator`, `FakeAssetResolver`, a temp asset cache,
a clean world). Strategy + the **"add tests as you build"** guide: [`../docs/testing.md`](../docs/testing.md).
