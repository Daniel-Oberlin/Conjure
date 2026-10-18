"""The mouth — which text-to-speech engine the voice client speaks through, and what each one can do.

`Settings.tts` (`CONJURE_TTS`) picks a **speech profile** from `PROFILES`. A profile knows three things
the rest of the system must not have to: how to build its pipecat service (a heavy, lazy import), what
sample rate it emits, and — the part that reaches the LLM — which **speech tags** the engine renders and
the guidance a model needs to use them well.

The tags are the whole reason this is a module and not a line in `voice.py`. An engine like Grok turns
`[laugh]` into a laugh and `<whisper>…</whisper>` into a whisper; Kokoro would read the brackets aloud.
So the vocabulary is a property of the *listener*, and it travels with the voice client: on connect it
sends the agent server a `register` message carrying this profile's `registration()` — the engine name,
its tag names, and its prompt guidance. The server injects the guidance into the system prompt (the
`{speech}` placeholder, docs/specs/agents.md §5.3) and renders each reply per connection: tags kept for
a client whose engine understands them, stripped for everyone else (`speech.strip_tags`). Nothing here
is persisted and no agent prompt names an engine — swap `CONJURE_TTS` and the vocabulary, the prompt and
the rendering all follow.

Keys: a cloud engine's key lives with the voice client, because the voice client is what calls the
engine. That is the one exception to "keys live with the agent server" (docs/specs/voice.md §1), and it
is a narrow one: the key buys audio, never a conversation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings


@dataclass(frozen=True)
class SpeechTags:
    """An engine's tag vocabulary, split the way it is written: `inline` tags stand alone at a point in
    the text (`[laugh]`); `wrapping` tags enclose a span (`<whisper>…</whisper>`). Names only — the
    rendering is fixed by the kind."""
    inline: tuple[str, ...] = ()
    wrapping: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return self.inline + self.wrapping


@dataclass(frozen=True)
class SpeechProfile:
    """One TTS engine as the voice client sees it. `build` is the only thing that imports pipecat."""
    name: str
    description: str
    sample_rate: int
    build: Callable[["Settings"], object]          # settings → a pipecat TTSService
    tags: SpeechTags = field(default_factory=SpeechTags)
    prompt: str = ""                                # LLM guidance for the tags; "" when there are none
    needs_key: Optional[str] = None                 # Settings field holding the engine's key, if any

    def registration(self) -> dict:
        """The `speech` block of the voice client's `register` message — what the agent server needs
        to prompt for and render this engine's tags. Flat tag names: stripping needs no more, and the
        inline/wrapping split is already written into the prompt."""
        return {"engine": self.name, "tags": list(self.tags.names), "prompt": self.prompt}


# --------------------------------------------------------------------------- Kokoro (local, no tags)

def _build_kokoro(settings: "Settings"):
    from pipecat.services.kokoro.tts import KokoroTTSService

    return KokoroTTSService(settings=KokoroTTSService.Settings(voice="af_heart"))


# --------------------------------------------------------------------------- Grok (xAI, speech tags)

# The vocabulary as documented at docs.x.ai (model-capabilities/audio/text-to-speech), 2026-10-18.
# Grouped the way the docs group them, because the prompt below is rendered from these rows and a model
# picks better from a short, categorised list than from one long one.
GROK_INLINE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pauses",             ("pause", "long-pause", "hum-tune")),
    ("laughter and crying", ("laugh", "chuckle", "giggle", "cry")),
    ("mouth sounds",       ("tsk", "tongue-click", "lip-smack")),
    ("breathing",          ("breath", "inhale", "exhale", "sigh")),
)
GROK_WRAPPING: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("volume and intensity", ("soft", "whisper", "loud", "build-intensity", "decrease-intensity")),
    ("pitch and speed",      ("higher-pitch", "lower-pitch", "slow", "fast")),
    ("vocal style",          ("sing-song", "singing", "emphasis")),
)
GROK_TAGS = SpeechTags(
    inline=tuple(t for _, ts in GROK_INLINE for t in ts),
    wrapping=tuple(t for _, ts in GROK_WRAPPING for t in ts),
)


def _grok_prompt() -> str:
    """Rendered from the tag tables so the vocabulary the model is told about and the vocabulary the
    server strips can never disagree. The guidance is xAI's own (sparingly; with punctuation; whole
    phrases), plus the one rule that matters in a shared conversation: the tags are for the ear, so a
    reply must still read as a complete sentence with every tag removed — text clients see exactly that."""
    inline = "\n".join(f"  - {cat}: " + " ".join(f"[{t}]" for t in ts) for cat, ts in GROK_INLINE)
    wrapping = "\n".join(f"  - {cat}: " + " ".join(f"<{t}>…</{t}>" for t in ts) for cat, ts in GROK_WRAPPING)
    return (
        "Your replies are spoken aloud by an engine that renders speech tags. Use them to give the "
        "delivery a mood — amused, hushed, emphatic, unhurried — the way a good narrator would.\n"
        "- Inline tags go at the point where the sound happens:\n" + inline + "\n"
        "- Wrapping tags enclose a span and change how it is delivered:\n" + wrapping + "\n"
        "- Sparingly: one or two tags in a reply, placed where the expression would occur naturally in "
        "conversation; none at all in a plain factual answer. Never stack tags, and never narrate them.\n"
        "- Combine a tag with punctuation (\"Really? [laugh] That's incredible!\"), and wrap complete "
        "phrases rather than single words (<whisper>It is a secret.</whisper>).\n"
        "- Use only the tags listed; anything else is read out as text.\n"
        "- The same reply reaches people reading a screen with every tag removed, so it must still be a "
        "complete, natural sentence without them."
    )


def _build_grok(settings: "Settings"):
    from pipecat.services.xai.tts import XAIHttpTTSService
    from pipecat.transcriptions.language import Language

    # The HTTP endpoint, not the WebSocket one: the agent server hands us whole sentences (each event is
    # its own utterance, docs/specs/voice.md §5), and the HTTP body streams as it is synthesised, so the
    # WebSocket's incremental text.delta buys nothing here while adding a connection to keep alive.
    return XAIHttpTTSService(
        api_key=settings.xai_api_key or "",
        settings=XAIHttpTTSService.Settings(voice=settings.xai_tts_voice, language=Language.EN),
    )


PROFILES: dict[str, SpeechProfile] = {
    "kokoro": SpeechProfile(
        name="kokoro", description="Kokoro — local, 24 kHz, no speech tags", sample_rate=24000,
        build=_build_kokoro),
    "grok": SpeechProfile(
        name="grok", description="xAI Grok TTS — cloud, 24 kHz, speech tags (laugh, whisper, …)",
        sample_rate=24000, build=_build_grok, tags=GROK_TAGS, prompt=_grok_prompt(),
        needs_key="xai_api_key"),
}


def profile_for(settings: "Settings") -> SpeechProfile:
    """The profile `Settings.tts` names, checked for what it needs to run. Raises ValueError with the
    fix in the message — a voice client that starts and then fails on the first reply is worse than one
    that refuses at the door."""
    name = (settings.tts or "").strip().lower()
    prof = PROFILES.get(name)
    if prof is None:
        raise ValueError(f"CONJURE_TTS={settings.tts!r} is not a speech engine; choose one of "
                         f"{', '.join(sorted(PROFILES))}")
    if prof.needs_key and not getattr(settings, prof.needs_key, None):
        from .config import env_var

        raise ValueError(f"CONJURE_TTS={name} needs {env_var(prof.needs_key)} (set it in .env)")
    return prof
