"""Text on its way to a VOICE client — the last stage before it is spoken.

An LLM writes for a screen: asterisks for emphasis, emoji as punctuation, markdown it was never asked
for. On a screen that is free; through a text-to-speech engine it is noise at best and an interruption
at worst. This module is the one place that turns written text into *speakable* text.

**It runs per-connection, not per-turn.** The same reply goes to a CLI unchanged and to a voice client
filtered, so nothing here is persisted, echoed back, or visible in the transcript — `agent_server`
applies it at fan-out, to assistant text only (see `_conv_broadcast`).

Adding a case is adding a function to `_STAGES`. They run in order over the whole string; each takes and
returns text, so they compose and are individually testable. Deliberately small: an LLM's phrasing is the
agent prompt's job, and rewriting meaning here would make the spoken and written conversations diverge.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable

# Pictographic ranges — the blocks an LLM actually reaches for. Deliberately NOT `unicodedata.category
# == "So"`, which also catches ©, ® and ™: "copyright sign emoji" would be worse than saying nothing.
_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),   # pictographs, emoticons, transport, supplemental, extended-A
    (0x1F900, 0x1F9FF),   # supplemental symbols and pictographs (within the span above; explicit)
    (0x2600, 0x27BF),     # misc symbols + dingbats — ☀ ✅ ❤ ✨
    (0x1F000, 0x1F0FF),   # mahjong / dominoes / playing cards
    (0x1F1E6, 0x1F1FF),   # regional indicators (flags)
)

# Joiners and modifiers that belong to a neighbouring emoji rather than standing for anything spoken:
# zero-width joiner, variation selectors, skin tones, keycap combiner.
_EMOJI_GLUE = {0x200D, 0xFE0E, 0xFE0F, 0x20E3} | set(range(0x1F3FB, 0x1F400))


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _is_glue(ch: str) -> bool:
    return ord(ch) in _EMOJI_GLUE


def drop_asterisks(text: str) -> str:
    """Remove every `*`.

    LLMs emit them constantly — `**bold**`, `*emphasis*`, `*shrugs*`, bullet lists — and a TTS engine
    either says "asterisk" or stumbles over them. None of it carries meaning a listener can use. Runs of
    spaces left behind are collapsed so `**Done** — there` doesn't gain a stutter; newlines are kept,
    since they pace the speech."""
    out = text.replace("*", "")
    out = re.sub(r"[^\S\n]{2,}", " ", out)        # collapse runs of spaces/tabs, never newlines
    return re.sub(r"(?m)^[^\S\n]+", "", out)      # …and the indent a leading bullet left behind


def name_emoji(text: str) -> str:
    """Replace each emoji with its name followed by the word "emoji" — 🍦 → `soft ice cream emoji`.

    Otherwise the character is silently dropped by the engine and the sentence loses whatever the LLM
    meant by it. A RUN of adjacent emoji codepoints becomes ONE phrase, named for the first of them:
    joined sequences (👨‍👩‍👧) and skin-toned ones (👍🏽) are single pictures to a reader, and naming every
    component would bury the sentence."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if not _is_emoji(text[i]):
            out.append(text[i])
            i += 1
            continue
        first = text[i]                            # name the run after its FIRST namable codepoint
        while i < n and (_is_emoji(text[i]) or _is_glue(text[i])):
            i += 1
        try:
            label = unicodedata.name(first).lower()
        except ValueError:                         # unnamed codepoint — better silent than "unknown"
            continue
        # Names carry a leading category for some blocks; the noun is what a listener needs.
        label = re.sub(r"^(?:emoji component |emoji modifier )", "", label)
        out.append(f"{label} emoji")
    return "".join(out)


# Ordered. Asterisks first: stripping them can't create an emoji, but an emoji name could contain
# characters a later stage cares about.
_STAGES: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("asterisks", drop_asterisks),
    ("emoji", name_emoji),
)


def for_speech(text: str) -> str:
    """Run the whole stage over `text`. Safe on empty/None-ish input; never raises."""
    if not text:
        return text
    for _, stage in _STAGES:
        text = stage(text)
    return text
