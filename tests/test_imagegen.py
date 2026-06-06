"""Image-generator registry + the Gemini response-extraction logic (no API calls)."""

from conjure.config import Settings
from conjure.imagegen import GeminiImageGenerator, available_providers, get_image_generator


def _settings(**overrides) -> Settings:
    base = dict(
        stt="whisper", tts="kokoro", llm="claude", llm_model="m",
        image_provider="gemini", image_model="im", skybox_model="sm", skybox_size="4K",
        anthropic_api_key=None, poly_pizza_api_key=None, openai_api_key=None, google_api_key=None,
        host="0.0.0.0", port=8080, world_url="http://localhost:8080",
    )
    base.update(overrides)
    return Settings(**base)


def test_registry_has_gemini():
    assert "gemini" in available_providers()


def test_no_key_means_no_generator():
    assert get_image_generator(_settings(google_api_key=None)) is None


def test_gemini_selected_when_key_present():
    g = get_image_generator(_settings(google_api_key="k"))
    assert g is not None and g.name == "gemini" and g.model == "im"


def test_gemini_extracts_inline_image(monkeypatch):
    from google import genai

    class _Blob:
        data = b"PNGDATA"
        mime_type = "image/png"

    class _Part:
        inline_data = _Blob()

    class _Content:
        parts = [_Part()]

    class _Cand:
        content = _Content()

    class _Resp:
        candidates = [_Cand()]

    class _FakeClient:
        def __init__(self, **kw):
            self.models = type("M", (), {"generate_content": lambda self, **kw: _Resp()})()

    monkeypatch.setattr(genai, "Client", _FakeClient)
    res = GeminiImageGenerator("key", "gemini-2.5-flash-image")._call(["a prompt"], None, None, None)
    assert res.data == b"PNGDATA" and res.mime_type == "image/png"
    assert res.provider == "gemini" and res.model == "gemini-2.5-flash-image"


def test_gemini_model_override(monkeypatch):
    from google import genai

    captured = {}

    class _Resp:
        candidates = [type("C", (), {"content": type("Ct", (), {
            "parts": [type("P", (), {"inline_data": type("B", (), {"data": b"x", "mime_type": "image/png"})()})()]})()})()]

    def _gen(self, **kw):
        captured.update(kw)
        return _Resp()

    monkeypatch.setattr(genai, "Client", lambda **kw: type("Cl", (), {"models": type("M", (), {"generate_content": _gen})()})())
    res = GeminiImageGenerator("key", "default-model")._call(["p"], "21:9", "4K", "gemini-3-pro-image")
    assert captured["model"] == "gemini-3-pro-image"      # per-call override used
    assert res.model == "gemini-3-pro-image"
