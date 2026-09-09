"""Judge — images in, a CONSTRAINED verdict out. The check for what geometry cannot name.

Where a joint ends up is arithmetic: `tests/test_figures.py` already asserts it, and so does the
geometry pass of `conjure.pose_corpus`. What no assertion expresses is whether a render looks like a
*person* doing the thing — is that a raised arm or a dislocated shoulder, is the elbow inside the
ribcage. That is a recognition problem, and this is the seam that asks one.

**Multiple choice, never free-form.** Vision models are unreliable at 3-D spatial description and
reliable at recognition, so every question here offers a fixed list and the answer is a letter. A model
that will not pick one returns `choice == -1`, which is an ABSTENTION and must never be scored as a
pass — see `Verdict.answered`.

Distinct from `captioner.Captioner`, which returns prose for search. Same shape of seam, different
return type, so it lives beside it rather than inside it: a caption cannot be scored and a verdict
cannot be indexed.

`build_judge(settings)` returns None when nothing is configured, and the caller degrades to the
geometry pass alone rather than failing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

#: One image handed to a judge: a caption the question can refer to, and the PNG bytes. The caption is
#: not decoration — a paired comparison is meaningless unless the model is told which image is which.
Image = tuple[str, bytes]

_PREAMBLE = (
    "You are checking a render of a 3-D character. Study the images, then answer the question by "
    "choosing exactly ONE of the options.\n"
    "Reply with the letter of your choice and nothing else — no explanation, no punctuation."
)

#: 26 options is far past the point where a multiple choice measures anything; this is a guard on the
#: letter encoding, not a considered maximum.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Verdict:
    """What the judge picked. `choice` indexes the options it was offered, or -1 for no answer."""

    choice: int
    label: str
    raw: str

    @property
    def answered(self) -> bool:
        return self.choice >= 0


@runtime_checkable
class Judge(Protocol):
    name: str

    async def choose(self, images: Sequence[Image], question: str, options: Sequence[str], *,
                     mime: str = "image/png") -> Verdict: ...


def prompt_for(images: Sequence[Image], question: str, options: Sequence[str]) -> str:
    """The text half of the question, shared by every backend so they are asking the same thing.

    Images are introduced by caption *in order*, because that is the only handle the question has on
    them ("compared to the first image…").
    """
    if len(options) > len(_LETTERS):
        raise ValueError(f"{len(options)} options is too many to letter")
    lines = [_PREAMBLE, ""]
    for i, (caption, _) in enumerate(images, 1):
        lines.append(f"Image {i}: {caption}")
    lines += ["", question, ""]
    lines += [f"({_LETTERS[i]}) {opt}" for i, opt in enumerate(options)]
    return "\n".join(lines)


def parse_choice(text: str, options: Sequence[str]) -> int:
    """The letter the reply settles on, or -1. Lenient about format, strict about range.

    Instructed to answer with a letter, a model mostly does — but "b" and "(b)" and "b) forward" and a
    bare restatement of the option all turn up, and a run that scored those as no-answer would blame the
    tool description for the judge's formatting. What is NOT lenient: a letter outside the offered range
    is no answer at all, never the nearest one.
    """
    body = (text or "").strip()
    if not body:
        return -1
    valid = range(len(options))

    def take(letter: str) -> int:
        i = _LETTERS.find(letter.lower())
        return i if i in valid else -1

    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    solo = re.fullmatch(r"[(\[]?\s*([a-zA-Z])\s*[)\].:,]?", first)
    if solo and take(solo.group(1)) >= 0:
        return take(solo.group(1))
    for m in re.finditer(r"[(\[]\s*([a-zA-Z])\s*[)\]]", body):        # "…the answer is (c)."
        if take(m.group(1)) >= 0:
            return take(m.group(1))
    m = re.match(r"([a-zA-Z])\s*[)\].:]", first)                      # "c) forward"
    if m and take(m.group(1)) >= 0:
        return take(m.group(1))
    # Last resort: the model wrote the option out instead of lettering it. Longest first, so a short
    # option that is a substring of a longer one ("up" inside "up and out") cannot win by accident.
    folded = body.casefold()
    for i in sorted(valid, key=lambda k: -len(options[k])):
        if options[i].casefold() in folded:
            return i
    return -1


def _verdict(raw: str, options: Sequence[str]) -> Verdict:
    choice = parse_choice(raw, options)
    return Verdict(choice, options[choice] if choice >= 0 else "", raw)


class FakeJudge:
    """Deterministic, dependency-free judge — for tests and for a dry run of the harness offline.

    With no script it answers by hashing the question and the image bytes: stable across runs, and
    spread across the options so a report built on it exercises both the pass and the fail path. Pass
    `answers` (letters or option indices, consumed in order and then repeated) to script a run.
    """

    def __init__(self, answers: Optional[Sequence[int | str]] = None):
        self.name = "fake"
        self._answers = list(answers or [])
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    async def choose(self, images, question, options, *, mime="image/png") -> Verdict:
        self.asked.append((question, tuple(options)))
        if self._answers:
            pick = self._answers[(len(self.asked) - 1) % len(self._answers)]
            i = _LETTERS.find(pick.lower()) if isinstance(pick, str) else int(pick)
        else:
            seed = hashlib.sha256(question.encode() + b"".join(d for _, d in images)).digest()
            i = seed[0] % max(1, len(options))
        return Verdict(i, options[i], f"({_LETTERS[i]})") if 0 <= i < len(options) else Verdict(-1, "", "")


class GeminiJudge:
    """Judge via Gemini multimodal (google-genai) — the default for the bulk battery.

    Already wired and keyed for captioning, and cheap enough to run every cell of a 60-cell run.
    """

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.name = model
        self._api_key = api_key

    async def choose(self, images, question, options, *, mime="image/png") -> Verdict:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        parts = [types.Part(text=prompt_for(images, question, options))]
        for _, data in images:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        resp = await client.aio.models.generate_content(
            model=self.name, contents=parts,
            # A judgement that moves between runs cannot be a regression net.
            config=types.GenerateContentConfig(temperature=0.0),
        )
        return _verdict((resp.text or "").strip(), options)


class ClaudeJudge:
    """Judge via Claude vision — for the rubric-heavy questions, where following the instruction to
    pick one option matters more than the per-call cost. Same key the director already uses."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.name = model
        self._api_key = api_key

    async def choose(self, images, question, options, *, mime="image/png") -> Verdict:
        import base64

        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        content: list = [{"type": "text", "text": prompt_for(images, question, options)}]
        for _, data in images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(data).decode()}})
        resp = await client.messages.create(
            model=self.name, max_tokens=16, temperature=0.0,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return _verdict(text, options)


#: Each backend's model when config doesn't name one, so `judge_model` can stay empty and still mean
#: something sensible after a provider switch.
_DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "claude": "claude-sonnet-4-6"}


def build_judge(settings, provider: str = "") -> Optional[Judge]:
    """Pick the judge backend from config (or an explicit override), or None if it can't run.

    None is a legitimate outcome, not an error: the harness still runs its geometry pass, and says in
    its report that nothing looked at the pictures.
    """
    backend = (provider or getattr(settings, "judge_provider", "gemini") or "gemini").strip().lower()
    if backend in ("none", "off", ""):
        return None
    if backend == "fake":
        return FakeJudge()
    model = (getattr(settings, "judge_model", "") or "").strip() or _DEFAULT_MODELS.get(backend, "")
    if backend == "gemini":
        key = getattr(settings, "google_api_key", None)
        if key:
            return GeminiJudge(key, model)
        print("[conjure] judge_provider=gemini but GOOGLE_API_KEY not set — no visual judging")
        return None
    if backend == "claude":
        key = getattr(settings, "anthropic_api_key", None)
        if key:
            return ClaudeJudge(key, model)
        print("[conjure] judge_provider=claude but ANTHROPIC_API_KEY not set — no visual judging")
        return None
    print(f"[conjure] unknown judge_provider {backend!r} — no visual judging")
    return None
