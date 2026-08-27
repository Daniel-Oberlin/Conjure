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


def _cluster_name(text: str, i: int) -> tuple[str, int]:
    """Name the emoji CLUSTER starting at `i`, and return where it ends.

    A cluster is one base codepoint plus whatever binds to it — a variation selector, a skin tone, or a
    zero-width joiner and the base it joins. 👨‍👩‍👧 and 👍🏽 are each one picture to a reader, so each is
    one name. Without an emoji database the best available name is the first component's, which is why
    the family reads as "man": accurate about what is there, if not about the whole."""
    n = len(text)
    first, j = text[i], i + 1
    while j < n and (_is_glue(text[j]) or (_is_glue(text[j - 1]) and _is_emoji(text[j]))):
        j += 1
    try:
        label = unicodedata.name(first).lower()
    except ValueError:                             # unnamed codepoint — better silent than "unknown"
        return "", j
    return re.sub(r"^(?:emoji component |emoji modifier )", "", label), j


def name_emoji(text: str) -> str:
    """Replace emoji with their names, so the engine says something rather than dropping them silently.

    One emoji is `<name> emoji` — 🍦 → *soft ice cream emoji*. **A run of several is every name followed
    by one plural** — 👍🎉🔥 → *thumbs up sign party popper fire emojis*. Slightly awkward to hear, and
    deliberately so: naming only the first would quietly discard the rest, and a listener cannot tell
    that something was dropped. Runs are rare; losing content is worse than reading it out."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if not _is_emoji(text[i]):
            out.append(text[i])
            i += 1
            continue
        names: list[str] = []                      # every cluster in this uninterrupted run
        while i < n and _is_emoji(text[i]):
            label, i = _cluster_name(text, i)
            if label:
                names.append(label)
        if names:
            out.append(f"{' '.join(names)} {'emojis' if len(names) > 1 else 'emoji'}")
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
