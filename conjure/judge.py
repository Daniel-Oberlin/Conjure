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
import importlib.util
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


def decide(scores: Sequence[float], options: Sequence[str], floor: float = 0.0) -> Verdict:
    """Turn one score per option into a `Verdict` — the whole decision the local judge makes.

    Pure and torch-free on purpose: this is where the argmax, the confidence and the abstention floor
    live, so it is the part worth testing, and testing it must not cost a model load.

    The softmax runs over ONLY the offered options, so the reported probability means "how sure
    between these answers" rather than being diluted by a 150k-token vocabulary that was never on
    offer. `-inf` marks an option whose letter the tokenizer could not produce; it scores zero and
    cannot win.
    """
    import math

    if not options:
        return Verdict(-1, "", "no options offered")
    finite = [x for x in scores if x != float("-inf")]
    if not finite:
        return Verdict(-1, "", "no option letter was representable")
    top = max(finite)
    exps = [math.exp(x - top) if x != float("-inf") else 0.0 for x in scores]
    total = sum(exps) or 1.0
    probs = [e / total for e in exps]
    choice = max(range(len(options)), key=lambda i: probs[i])
    conf = probs[choice]
    raw = f"({_LETTERS[choice]}) p={conf:.3f}"
    if conf < floor:
        return Verdict(-1, "", f"{raw} < floor {floor:.3f} — declined")
    return Verdict(choice, options[choice], raw)


class LocalJudge:
    """Judge via a local vision-language model — **the letter is read, never generated.**

    Every question here is multiple choice and the answer is one letter, so this runs a single forward
    pass and compares the logits of the candidate letter tokens. It does not sample, does not decode,
    and does not parse. Four things follow, and the first two are why this backend exists:

    * **Reproducible.** Same weights and same pixels give the same verdict, bit for bit. The hosted
      judges provably do not: `backlogs/figures.md` records Gemini answering "no" then "yes" to
      *identical images at temperature 0*, which cost four full 60-cell runs to attribute — "nothing
      was moving except the model". A regression net that moves under you is not a net.
    * **Private, and it cannot decline.** The figure corpus includes undressed exports, and a hosted
      model refusing to look at one is a gap in the battery that reads like a pass.
    * **Malformed answers are impossible.** The choice is an argmax over exactly the letters offered,
      so `parse_choice` has nothing to fail at and an out-of-range letter cannot occur.
    * **Cheap.** One prefill, zero decode — and no per-call cost, so the "generous budget" the eval
      harness was designed around stops being a budget.

    **Abstention is kept deliberately.** `choice == -1` is load-bearing (the recognition fixture must
    DECLINE the wrong kneel), and a plain argmax always answers — which would score a perfect
    abstention rate by destroying the signal. The primary decline path is unchanged and needs nothing
    from this class: the caller offers "none of these" as an option and it wins on its own merits.
    `floor` adds a second, opt-in net — abstain when the best candidate's share of the probability mass
    is below it. Default 0.0 (off), because a floor chosen without measurement is a thumb on the scale.

    The confidence is reported in `Verdict.raw` either way, so an advisory line a person reads says how
    close the call was. Uses `torch` + `transformers` — already present for SigLIP embeddings — so this
    needs a forward pass, not an inference server.
    """

    def __init__(self, model_id: str, *, device: str = "", floor: float = 0.0):
        self.name = model_id
        self._device = device
        self._floor = floor
        self._model = None
        self._proc = None
        self._torch = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self._torch = torch
        if not self._device:
            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        # bf16 on the GPU, fp32 on CPU. NOT fp16: it has the same 16 bits but far less exponent
        # range, and these models are trained in bf16 — the narrow range is a known source of
        # quiet quality loss rather than an error anyone would see. Half precision on CPU is
        # slower, not faster, and the accuracy there is not spare either.
        dtype = torch.float32 if self._device == "cpu" else torch.bfloat16
        model = AutoModelForImageTextToText.from_pretrained(self.name, dtype=dtype)
        self._model = model.to(self._device).eval()
        self._proc = AutoProcessor.from_pretrained(self.name)

    def _letter_ids(self, n: int) -> list[list[int]]:
        """The token ids that would START each option's answer, one list per option.

        A tokenizer may render "b" differently bare, capitalised, space-prefixed or after a bracket,
        and which one the model reaches for is its habit, not ours. So every plausible surface form is
        scored and the option takes its best — the alternative is guessing the convention and silently
        measuring the guess.
        """
        tok = self._proc.tokenizer
        out = []
        for i in range(n):
            ch = _LETTERS[i]
            ids = set()
            for variant in (ch, ch.upper(), f" {ch}", f" {ch.upper()}", f"({ch}", f"({ch.upper()}"):
                enc = tok.encode(variant, add_special_tokens=False)
                if enc:
                    ids.add(enc[0])
            out.append(sorted(ids))
        return out

    def _call(self, images: Sequence[Image], question: str, options: Sequence[str], mime: str) -> Verdict:
        import io

        from PIL import Image as PILImage

        torch = self._torch
        pics = [PILImage.open(io.BytesIO(data)).convert("RGB") for _, data in images]
        content: list = [{"type": "image"} for _ in pics]
        content.append({"type": "text", "text": prompt_for(images, question, options)})
        chat = self._proc.apply_chat_template(
            [{"role": "user", "content": content}], tokenize=False, add_generation_prompt=True)
        inputs = self._proc(text=[chat], images=pics or None, return_tensors="pt")
        inputs = {k: (v.to(self._device) if hasattr(v, "to") else v) for k, v in inputs.items()}

        with torch.no_grad():
            logits = self._model(**inputs).logits[0, -1].float()

        # One score per option: the best of its surface forms. The decision itself is `decide`.
        scores = [
            float(torch.max(logits[torch.tensor(ids, device=logits.device)]).item()) if ids
            else float("-inf")
            for ids in self._letter_ids(len(options))
        ]
        return decide(scores, options, self._floor)

    async def choose(self, images, question, options, *, mime="image/png") -> Verdict:
        import asyncio

        self._ensure()
        return await asyncio.to_thread(self._call, images, question, options, mime)


#: Each backend's model when config doesn't name one, so `judge_model` can stay empty and still mean
#: something sensible after a provider switch.
_DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "claude": "claude-sonnet-4-6",
                   "local": "Qwen/Qwen3-VL-4B-Instruct"}


def _have(*mods: str) -> bool:
    return all(importlib.util.find_spec(m) is not None for m in mods)


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
    if backend == "local":
        # No key to check, but the deps can be absent. Same contract as every other backend here:
        # None plus a message, so the harness still runs its geometry pass and says nobody looked at
        # the pictures — rather than an ImportError from inside `AutoProcessor` mid-battery.
        if not _have("torch", "transformers", "torchvision"):
            print("[conjure] judge_provider=local but torch/transformers/torchvision not installed "
                  "— no visual judging (install the optional 'vision' dependency group)")
            return None
        return LocalJudge(model, device=getattr(settings, "judge_device", "") or "",
                          floor=float(getattr(settings, "judge_floor", 0.0) or 0.0))
    print(f"[conjure] unknown judge_provider {backend!r} — no visual judging")
    return None
