# Plan — local models

**Status:** all phases open · **Opened:** 2026-09-19 · **Blocked on:** nothing

**This file is temporary.** It holds one sequence across the provider registry, the library's ingest
seams, the figure eval harness and the director's turn assembly — areas the specs deliberately keep
apart — and is deleted when the last phase has settled.

**Dissolves to:** [`providers.md`](../providers.md) for the registry rows (a local director, a local
captioner, a local judge) · [`specs/agents.md`](../specs/agents.md) **§4** (tool scoping) and **§5.3**
(prompt injection) for dynamic scoping · [`specs/library.md`](../specs/library.md) **§7** for the
captioner surface · the matching backlogs for whatever is deferred ·
[`decisions.md`](../decisions.md) for the forks in §8.

---

## 0. Why, and why in this order

Four motives, and they do **not** all survive contact with the hardware:

| motive | verdict |
|---|---|
| **Experience** running local models for Conjure | Cheap. The seams already exist. |
| **Latency** — beat the cloud round-trip | **Unlikely on the current machine, for the director.** Real for small single-shot jobs. |
| **Privacy** — keep it on the Mac | Real, and mostly *already won*. What's left is images. |
| **Multimodal** — room cameras, captioning, generation | The strongest case, and the one that *must* be local. |

The order below is set by that table. The two jobs with **no latency budget at all** go first (the
judge and the captioner) — they are offline, batched, already behind pluggable protocols, and they
collect the privacy and multimodal wins without betting anything on local throughput. The director
goes **last**, after the tool surface has shrunk, because a local director tested against 56 tools is
a local director tested in its worst possible configuration.

---

## 1. What this is built on

Measured 2026-09-19. Where one of these is wrong, the phase resting on it is wrong.

**The director ships ~16k tokens of preamble before it says anything.**

| part | size |
|---|---|
| 56 MCP tools (`mcp_server.py`) | 51,621 chars ≈ **13k tokens** |
| `agents/builder/prompt.md` | 10,692 chars ≈ 2.7k tokens |
| `{context}` injection | live, per turn |
| history | capped by `history_cap` |

And a turn is a *loop* — `MAX_TOOL_HOPS = 24`, a real build turn is several — so that preamble is
re-sent on every hop.

**Tool grants are already per-agent, and already vary by an order of magnitude.**

| agent | tools granted |
|---|---|
| `builder` | 55 of 56 |
| `outdoor` | 12 |
| `scratch` | 1 |

**The static prefix is contiguous.** `{context}` sits at line 75 of 76 in `builder/prompt.md`, so
everything ahead of the live injection is byte-identical turn to turn. A prefix cache (llama.cpp, MLX)
therefore covers the full tools-plus-prompt block, and hop 2+ costs far less than hop 1. This is not
an accident to be preserved by luck — if an injection moves earlier, this property dies silently.

**Three model seams are already pluggable**, each dispatching on a config string, and none of them
needs new architecture to take a local backend.

| seam | file | real backends today | test double | selected by |
|---|---|---|---|---|
| Director | `llm.py` — `LLM` protocol + `ROSTER` | Claude, Gemini, Chat, Grok | — | `ROSTER` row + key presence |
| Captioner | `captioner.py` — `Captioner` | Gemini | `FakeCaptioner` | `caption_provider` |
| Judge | `judge.py` — `Judge` | **Gemini, Claude** | `FakeJudge` | `judge_provider` |

The judge is the one that matters for phase 1: it is the only **non-roster** seam that has already
grown a *second* real backend, so `build_judge` is live proof that adding a third provider there is an
`if` and a class rather than a design.

`_OpenAICompatibleLLM` (`llm.py:200`) holds the entire tool-call loop; `GrokLLM` subclasses it by
setting **one attribute**, `base_url`. Ollama, llama.cpp's `llama-server`, LM Studio and `mlx-lm.server`
all speak that API.

**Local inference is already in the venv and already spending the Mac's memory.** Whisper
(faster-whisper, CPU float32 — CTranslate2 has no Metal backend), Kokoro TTS (MLX), and SigLIP
embeddings (`embeddings.py`, torch, currently CPU — no `mps` mapping). A local LLM would be the first
thing here to want the GPU in bulk, and it competes with all of the above for 32 GB.

**Two-layer tool scoping already exists** (`specs/agents.md` §4): client-side filtering in
`director._scope_tools` (`director.py:117`) plus a hard gate in `mcp_server._GatedMCP`, in a separate
process from the LLM. Phase 4 extends this, it does not invent it.

**MCP itself cannot help yet.** Researched 2026-09-19:

- The current spec, **2026-07-28**, has no tool search. Its only context-relevant changes are minor:
  `tools/list` SHOULD return a deterministic order "to improve LLM prompt cache hit rates", and list
  results carry `ttlMs`/`cacheScope`. `server/discover` is version/capability negotiation, not tool
  subsetting.
- The **roadmap** (updated 2026-08-22) names *progressive discovery* under Priority Area 4, but the
  Core Primitives WG is "forming during this roadmap period" and the deliverable is to define what an
  experimental mechanism *would look like*.
- **SEP-1821** (a `query` param on `tools/list` + a `tools.filtering` capability) has been **draft with
  no sponsor since 2025-11-17**.

**Anthropic's Tool Search is provider-side and Anthropic-only.** Beta `advanced-tool-use-2025-11-20`,
`defer_loading: true` per tool; Anthropic report ~72K → ~8.7K tokens for a 50+ MCP-tool setup, and MCP
eval accuracy 49% → 74% (Opus 4) and 79.5% → 88.1% (Opus 4.5). Their recommended threshold is ≥10 tools
or >10k tokens of definitions — **Conjure is over both**. It costs nothing to turn on for the Claude
path and does **nothing** for a local model — so it is **its own change, §5a**, measured separately.
Pinned SDKs: `mcp` 1.27.2, `anthropic` 0.105.2.

**The hardware.** M2 Max, 32 GB, shared memory, bandwidth-bound. A 4-bit ~30B model is ~17 GB resident
and leaves little beside Whisper, Kokoro, SigLIP, a browser and the world server. **MoE** — mixture-of-experts, a model that keeps all its
parameters resident but routes each token through only a small fraction of them — is the right shape
here: it buys the quality of a big model at the decode speed of a small one, which is exactly the
trade a bandwidth-bound Mac wants. What changes the latency verdict on an
upgrade is **memory bandwidth**, not capacity: more RAM at the same bandwidth buys a bigger model at
the same speed.

---

## 2. Phase 1 — the judge goes local

**Revised 2026-09-19, before writing any code**, after reading the slice-2 record in
[`backlogs/figures.md`](../backlogs/figures.md). The acceptance criteria this section shipped with —
"agreement with the Gemini judge" — was wrong, and the backlog says why.

### 2a. What the judge actually is today

Two different questions live behind one seam, and they have opposite track records:

| framing | asked by | status |
|---|---|---|
| **Judgement** — "could a body hold this pose?" | the eval harness | **Broken on every hosted model tried.** Gemini 2.5 Flash and Claude Sonnet 4.6 both pass a figure whose *head is on backwards*. |
| **Recognition** — "which of these 13 poses is this?" | `scripts/pose_library.py --identify` | **Works** — 13/13 on Grace, and it declines the two wrong poses their signatures passed. |

The judge layer is therefore **advisory by default** (`--judge-gates` to make it count). Nothing is
gated on it. Two consequences for this phase:

- **"Agreement with Gemini" is not a bar.** For the judgement question Gemini is a *known-broken
  oracle*; agreeing with it would be a local model reproducing a defect. Struck.
- **Two calibration fixtures already exist**, both with known answers, and both already used to
  disqualify a model: the **head-on-backwards render** (judgement — must answer "no") and the **wrong
  `kneel`** (recognition — must decline it). The backlog kept the machinery explicitly because "the
  fixture is exactly what will detect a better model the day one exists." That is the bar.

### 2b. The argument for local that this record actually makes

Not cost, and not latency. **Reproducibility.**

> *"no" on one call, "yes" on another — **same images, temperature 0**" … "Nothing was moving except
> the model."*

That instability cost four full 60-cell runs to diagnose, and run 4's "improvement" turned out to be
noise. A local model with pinned weights is bit-reproducible, and the design below makes it
*structurally* so: no sampling, no decode loop, no formatting to parse.

### 2c. How it is built — read the letter, do not generate it

Every judge question is multiple choice and the answer is a single letter. So the local backend does
**one forward pass and compares the logits of the candidate letter tokens** — it never generates.

This is strictly better than the grammar-constrained decoding §1 assumed it would need:

- **Malformed answers become impossible** — the choice is an argmax over exactly the offered letters,
  so `parse_choice` can never fail. (`prompt_for` is still shared, so it is asking the same question.)
- **Deterministic by construction** — no sampler, nothing to seed.
- **Fast** — one prefill, zero decode.
- **A confidence signal falls out free** — the softmax over the candidate letters.

**Abstention has to be preserved deliberately.** `choice == -1` means the model declined, and it is
load-bearing (the recognition fixture *must* decline the wrong kneel). A pure argmax always answers,
which would score a perfect abstention rate by destroying the signal. So the backend abstains when
top-1 probability over the candidate set falls below a floor, and reports the probability either way.

### 2d. No new runtime

Settles §8 fork 3 for this seam: `torch` 2.12.1 (MPS available) and `transformers` 5.12.1 are
**already in the venv** for SigLIP, and `Qwen3VLForConditionalGeneration` is present. Logit reading
needs a forward pass, not a server — so Ollama / llama.cpp / LM Studio are not required here at all.
That question survives only for the *director* (phase 5), where generation is the point.

**Acceptance:**
1. **Calibration first, and it gates the rest.** The head-on-backwards render must come back "no"; the
   wrong `kneel` must be declined. A model that fails these is reported as failing and nothing else
   about it is worth reading — same discipline the harness already applies.
2. **Reproducibility:** byte-identical verdicts across two full runs of the same corpus. This is the
   one the hosted models provably fail.
3. Recognition accuracy on the 13-pose identify task, against the recorded 13/13 baseline.
4. Wall-clock for a full corpus run, and the abstention rate with its probability floor stated.

---

### 2e. What it measured (2026-09-19)

Built as designed in §2c: `LocalJudge` in `conjure/judge.py`, `judge_provider=local`, no runtime.
`Qwen/Qwen3-VL-4B-Instruct` on MPS in **bf16** — not fp16, which has the same 16 bits and far less
exponent range than the precision these models are trained in.

**The mechanism is right, and that was checked rather than assumed.** A heavy bias toward one answer
is exactly what a logit read at the *wrong position* looks like, so the read was compared against what
the model actually generates: it emits `(a)`, its top next-token is `'(a'` at 39.97, and the scores the
backend computes match the top-8 logits exactly. The letter-variant map catches the bracketed form.

| acceptance | result |
|---|---|
| **1. Recognition calibration** — decline the wrong `kneel` | **PASSED** |
| **1. Judgement calibration** — see a head on backwards | **FAILED on Grace and Trish, passed on Saka** |
| **2. Reproducibility** | **PASSED** — every repeat identical, including the calibration on all three rigs |
| **3. Recognition accuracy** (13 poses × 3 rigs) | **27 of 39 misidentified** at 4B, **24 of 39 at 8B**, against Gemini's recorded 7 |
| **4. Wall clock** | ~1–2 s per call after a ~15 s model load; a 40-cell battery in minutes, no per-call cost |

**Three findings.**

1. **Reproducibility is real, and it is the one thing this backend clearly wins.** Every repeated call
   returned a byte-identical verdict. That is the defect `backlogs/figures.md` spent four runs chasing.
2. **The judgement question fails locally too — so it was never a hosted-model problem.** A local 4B
   passes a head-on-backwards figure on two rigs out of three, exactly as Gemini 2.5 Flash and Claude
   Sonnet 4.6 did. This *confirms* the backlog's narrowed finding from a third direction: the
   **framing** is what fails, not the vendor. Nothing here argues for making the anatomy verdict a gate.
3. **Recognition accuracy at 4B is not good enough to replace Gemini** — 27/39 against 7/39. It clears
   its own calibration, so it is not broken; it is weaker. bf16 vs fp16 changed the aggregate not at
   all, so this is capacity, not precision.

**Size was tried, and it is not the lever.** `Qwen3-VL-8B-Instruct` — twice the parameters, 16 GB —
moved recognition from 27/39 to **24/39**. Three cells. It fails the judgement calibration on exactly
the same two rigs, with exactly the same stability. Precision was not the lever either (bf16 vs fp16:
no change in the aggregate). Two levers tried, both flat: the gap to Gemini is not a tuning problem.

**A fourth model, the same failure.** The head-on-backwards fixture has now been failed by Gemini 2.5
Flash, Claude Sonnet 4.6, Qwen3-VL-4B and Qwen3-VL-8B — cloud and local, 4B to frontier, all on the
same two rigs. That is as close to settled as this gets: **asking a VLM to judge anatomical
plausibility does not work, and no model on this list is going to be the one that fixes it.** The
advisory-by-default decision in `backlogs/figures.md` is correct and should stop being provisional.

**Status: shipped as an option, not as the default.**

| question | who should answer it | why |
|---|---|---|
| Recognition ("which pose is this?") | **Gemini** stays default | 7/39 vs 24/39 is not close |
| Anything needing a stable net | **local** | bit-reproducible; the hosted judges are not |
| Renders a hosted model may refuse | **local** | the undressed-export trap, which cannot be fixed by prompting |

The seam, the calibration and the determinism are all real and all built. What is missing is a local
model good enough at this particular recognition task, and the fixture is now sitting there ready to
detect one — which is exactly what the machinery was kept for.

---

## 3. Phase 2 — the captioner goes local

**Every image imported into the library is currently sent to Gemini** (`captioner.py`,
`caption_provider=gemini`). That is the largest remaining thing that leaves the machine, and
`captioner.py:76` already reserves the slot with a comment.

Three wins at once: it closes that hole, it removes a rate limit and a per-image cost from bulk
ingest — which is what the NAS photo library in [`backlogs/library.md`](../backlogs/library.md) would
need — and it is the on-ramp to room cameras, which are not a cloud workload at any price.

**Acceptance:** caption a fixed set of library images both ways; compare FTS hit quality on the
resulting labels (the captions exist to be *searched*, so that is the measure, not prose quality);
report throughput for a bulk backfill.

---

### 3a. What it measured (2026-09-19)

`LocalCaptioner` in `conjure/captioner.py`, `caption_provider=local`, same weights as the judge
(`Qwen3-VL-4B-Instruct`, bf16 on MPS) — so configuring both costs one download and one resident model.

Ten catalogued images captioned locally against the label already on the row:

| existing label | local caption |
|---|---|
| Cute bear cub in forest | Cute brown bear cub in forest, soft focus, natural lighting |
| Baroque portrait of woman | Portrait of a woman in soft lighting with vintage style |
| Seamless green grass texture | Green grass texture background |
| Whimsical pig seamless pattern | Cute pink pigs in sky with clouds and stars, watercolor style |
| Cute cartoon robot floating in sunny field | Cute robot flying over green hills under sunny sky, cartoon |

**Unlike the judge, this one is good enough as it stands.** The captions are *longer* than the prompt
asks for and, for the job captions actually do, mostly richer — "red-haired woman in blue floral dress"
gives FTS more to match than "elegant smiling woman". Recognition of a 3-D pose against thirteen
near-neighbours is a much harder ask than describing a picture, which is why the same 4B model clears
one bar and not the other.

- **Deterministic**: repeated captions identical, and `do_sample=False` is why. A re-index cannot
  silently rewrite search results.
- **Throughput**: 73 s for ten images including a ~15 s model load — roughly **5 s an image** steady
  state, no rate limit and no per-image cost. Slow next to Gemini, irrelevant for a backfill that runs
  once, and it is the only option that scales to a NAS-sized library.
- `clean_caption` earns its place: the model does emit preambles and trailing full stops, and both
  would otherwise be indexed verbatim.

---

## 4. Phase 3 — measure, before changing anything

`Director.context_stats()` (`director.py:215`) already attributes the last assembled turn across
prompt / space / tools / history, and `_last_injected` sizes each injection. Record a baseline per
shipped agent, and the per-hop growth across a real multi-hop build turn.

This exists so phases 4 and 5 have a number to move rather than an intuition. It is cheap — the
measurement is already taken while building each turn.

---

## 5. Phase 4 — dynamic tool scoping

The interesting phase, and the one that pays off whether or not a local director ever ships. Two
pieces, deliberately separated: a **flag** that only helps Claude, and a **layer** that helps every
provider. Ship them apart or neither number means anything.

### 5a. `defer_loading` on the Claude path — do this first, and alone

**Turn on Anthropic's Tool Search for the Claude director**
(`defer_loading: true`, beta `advanced-tool-use-2025-11-20`; `anthropic` 0.105.2 is current enough).
It is a flag, not a design, and Conjure is well over both of Anthropic's stated thresholds.

Do it **before** the provider-neutral work of §5, and ship it as its own commit. Two reasons. It is
the cheapest available measurement of *how much the 13k actually costs us* — if deferring the tools
moves nothing observable on the Claude path, the case for building §5 at all gets weaker, and that is
worth knowing before the work rather than after. And landing both at once makes the two effects
unreadable.

**Acceptance for 5a on its own:** over a fixed script of turns, before and after, report
(a) prompt tokens per turn from the provider's `usage`, (b) wall-clock per turn, (c) whether the same
tools got called. Anthropic's published figures are ~72K → ~8.7K tokens and MCP-eval accuracy 49% →
74% (Opus 4) / 79.5% → 88.1% (Opus 4.5) — our own numbers will differ, and the point of 5a is to have
ours instead of theirs. One thing to watch: deferral **moves the tool block out of the cached prefix**,
so a turn that searches may pay more latency than it saves even while the token count drops. (b) is
there to catch that, and it is the result most likely to surprise us.

### 5a-results. What it measured (2026-09-19)

Built (`ClaudeLLM._tools_for`, `tool_search` off by default) and measured with
`scripts/tool_search_ab.py` — real Claude, real 55-tool builder grant, **no tool executed**: the
harness records which tools were chosen and returns a stub, because what is being measured is the
request cost and the choice, and because the world server runs on live data shared across branches.

**One request, three configurations** — this isolates the claim:

| configuration | input tokens |
|---|---|
| all 55 tools loaded | **19,379** |
| all 55 deferred + the search tool | **3,481** |
| no tools at all (the prompt floor) | 2,771 |

So the tool block costs **16,608 tokens**, and deferral reduces it to **710** — a **96% cut**, matching
Anthropic's published figures. The mechanism does exactly what it says.

**And yet the whole turn got worse.** One real request ("put a picture of a misty pine forest on the
north wall"), per hop:

| | hops | input tokens per hop | total | wall clock |
|---|---|---|---|---|
| off | 3 | 19,390 · 19,603 · 19,699 | 58,692 | 9.0 s |
| on | 6 | **3,788** · 10,642 · 7,044 · 15,160 · 8,244 · 8,335 | 53,213 | 15.3 s |

Hop 1 falls 19,390 → 3,788 exactly as predicted. The total barely moves and the clock nearly doubles,
because **the turn took six hops instead of three** — searching for tools costs round trips, and each
round trip re-sends a conversation that is now growing. This is the risk §5a was written to catch, and
it fired: *"a turn that searches may pay more latency than it saves even while the token count drops."*

**The tools chosen differed**, and not obviously for the worse — the deferred arm ended with
`place_image`, actually putting the picture on the wall, where the loaded arm generated an image and
stopped at `show_annotations`. Suggestive, and no more than that: the stub returns a synthetic success
for every call, so everything after hop 1 is steered by a fake result. **This is exactly the gap a real
regression harness would close**, and the reason 5b stays blocked.

### The finding that matters more than tool search

**`cache_read` was 0 on every hop of every turn, in both arms — Conjure does not use prompt caching at
all.** `grep -rn cache_control conjure/` returns nothing. Every hop of every turn re-pays full price
for the entire ~16.6k tool block plus the prompt.

That is the larger, cheaper and safer win, and it is *orthogonal* to tool search:

- **It changes no behaviour.** Caching alters what a request costs, not what the model sees — so it
  needs none of the regression harness that blocks 5b.
- **It suits this shape of work exactly.** A turn is a loop over a byte-identical prefix; hops 2+ are
  precisely what a cache breakpoint is for. §1 already established the prefix is contiguous
  (`{context}` sits last), which is the property that makes it cacheable.
- **It compounds, where tool search competes.** Deferral shrinks hop 1 and adds hops; caching makes
  every hop after the first nearly free. On the measured turn, hops 2 and 3 alone are ~39k tokens of
  re-sent prefix.

**Recommendation: do prompt caching first, before any more work on tool search.** Then re-measure —
a cached 19.4k prefix may well beat a deferred-and-searched one, in which case 5b is unnecessary and
the whole scoping premise of this plan weakens. That is worth knowing before building it.

### 5a-caching. Prompt caching — built and measured (2026-09-19)

Acting on the finding above, before any more scoping work. `prompt_cache`, **on by default** — it
changes what a request costs, not what the model sees, so it needs none of the behavioural regression
testing that blocks 5b.

**Two breakpoints, covering deliberately different spans.** The API renders `tools` → `system` →
`messages`, and the invalidation rules are per-tier: a *system-prompt content change preserves the
tools cache*. That asymmetry is the whole design, because **Conjure's system prompt is not stable
across turns** — it carries the live `{context}` injection.

| marker | covers | survives |
|---|---|---|
| last **tool** definition (`_tools_for`) | the ~12.7k of tool schemas | **across turns** — a new `{context}` does not touch it |
| last **system** block (`_system_for`) | tools + prompt, ~19.4k | hops 2..n of one turn |
| top-level `cache_control` | the growing message tail | hop to hop |

Measured, three turns, builder agent, **with `{context}` changed every turn** (the production shape):

| | full-price input | cache read | cache write | effective | cost @ $3/MTok |
|---|---|---|---|---|---|
| caching off | 97,074 | 0 | 0 | 97,074 | **$0.2912** |
| caching on | 10 | 74,520 | 3,099 | 11,336 | **$0.0340** |

**88% cheaper**, and the mechanism is visible per hop. On a turn whose context the cache had never
seen, hop 1 read **16,292** (the tools) and wrote only **3,099** (the changed prompt) — where with the
system marker alone it wrote 19,392 and read nothing. That single comparison is why there are two
markers and not one; a first implementation with only the system breakpoint measured **zero**
cross-turn benefit.

TTL is the default 5 minutes and should stay there: hops are seconds apart, a read refreshes the
timer, and the 1-hour TTL's 2× write premium would buy nothing at this cadence.

### Where this leaves tool search, and 5b

Caching and deferral **compete for the same tokens**, and caching wins on this workload:

| | tool block on hop 1 | whole turn | behaviour risk |
|---|---|---|---|
| tool search | 19,379 → 3,481 tokens | **worse** — 3 hops → 6, 9.0 s → 15.3 s | changes what the model sees; needs the harness |
| prompt caching | 19,392 → 16,292 **read** + 3,099 write | 88% cheaper, no extra hops | none |

Deferral removes the tokens from the request; caching makes them nearly free while leaving them in
front of the model. The second is strictly better here — the model keeps its whole tool list, the turn
keeps its hop count, and the bill falls by the same order.

**So 5b's premise is now weak, and that is a real result rather than a deferral.** It was written to
cut a 13k tool block that costs ~$0.0009 a turn once cached. The remaining arguments for it are that
it is provider-neutral — **caching as built is Anthropic-only**, so a local director still pays the
full 15k — and that Anthropic's accuracy numbers suggest fewer tools help a model *choose* better,
which is an argument about quality, not tokens, and would need the harness to test either way.

**Recommendation: close 5b as not-worth-building for cost.** Reopen it only as a *quality* measure, or
when a local director makes the provider-neutral half matter. Caching for the other roster providers
(Gemini, OpenAI) is the better-value follow-up and is the same shape of change.

### 5a-caching-others. Gemini and OpenAI (2026-09-19)

**Neither has a `cache_control` equivalent — both cache automatically, and the work is measuring
whether they actually do.** OpenAI caches any prompt over 1024 tokens in 128-token increments; Gemini
2.5 has implicit caching on by default over the same threshold. Nothing to switch on.

Measured on the builder agent with `{context}` changing every turn (the production shape), after
teaching both adapters to report usage:

| | before | after |
|---|---|---|
| **OpenAI** gpt-4.1 (13,129-token prompt) | 0 / 1,792 / 0 cached | **13,056 / 13,056 / 0** |
| **Gemini** 2.5-flash (29,630-token prompt) | — | 0 / 0 / 1,990 / 1,982 / 3,974 / 1,991 → **~5%** |

**OpenAI: fixed, with a routing hint.** Automatic caching is *best effort* — a hit needs the request
to reach a machine that already holds the prefix, which is why the same prompt cached 0, then 1,792,
then 0. `prompt_cache_key` is the documented lever, and it took hits from ~5% to **99.4%** on two turns
of three. Keyed on the **tool set**, because that is the part of the prefix that is genuinely stable
(the system prompt carries the per-turn injection); two agents with different grants get different
keys rather than competing for one backend. Withheld from xAI, which mirrors the API but need not
accept the field — an unknown parameter there is a 400, not a graceful ignore.

It is still best effort: turn 3 cached nothing. Unlike Anthropic's explicit breakpoints, there is no
way to make it a guarantee.

**Gemini: implicit caching is on, and doing almost nothing** — 0–13%, averaging ~5% of a 29,630-token
prompt, consistently across six turns. There is no routing hint. The only remaining lever is
**explicit context caching** (`cachedContents`): create a cache object, hold a TTL, manage its
lifecycle. **Not recommended now** — that is real lifecycle machinery for a roster alternate rather
than the default director, and the same effort spent on the structural fix below would help all three.

### What the three providers together actually say

| provider | mechanism | control | measured |
|---|---|---|---|
| Anthropic | explicit `cache_control` | full — breakpoints where we choose | **84%** of the prefix, reliably |
| OpenAI | automatic + `prompt_cache_key` | routing hint only | 99% when it lands, 0% when it does not |
| Gemini | implicit only | none | ~5% |

**The structural fix is the one that would help everywhere.** All three cache by prefix and all three
say the same thing: keep the front stable, put what varies at the end. Conjure puts the live
`{context}` injection *inside the system prompt*, which is at the front. Anthropic's tools breakpoint
routes around that; OpenAI survives it only because the injection sits at the very end of the prompt
(line 75 of 76) and the 128-token granularity forgives the tail; Gemini does not survive it at all.

Moving that injection out of the system prompt and into a trailing message would put the volatile
content where every provider's documentation asks for it. It is a change to what the model sees —
position, not content — so unlike caching it is not free of behavioural risk, and it wants the harness
from §9. **This is the highest-value item left on this plan.**

**Incidental, and operational:** gpt-4.1 has a 30,000 TPM limit on this account, and a builder turn is
~13k tokens — so the OpenAI director throttles after about two turns a minute. Caching does not help;
cached tokens still count toward TPM. Gemini's tokenizer counts the same prompt as 29,630 tokens
against OpenAI's 13,129, so "15k of context" is model-relative.

### 5b. `search_tools` — the provider-neutral layer

Today's scoping is **static**: an agent's `ServerRef.tools` allow-list is fixed at load. The proposal
is to make it **dynamic** — a small always-loaded core plus a `search_tools` call that admits the rest
of the agent's grant on demand. Note what this is *not*: it does not widen an agent's authority.
`_GatedMCP` still refuses anything outside the static grant, so search selects **within** the
allow-list and the trust model of §4 is untouched.

Built client-side in `director.py`, it is **provider-neutral** — Claude, Gemini and a local Qwen all
benefit, and it does not wait on a working group. It is also the only one of the two pieces that does
anything at all for phase 5.

**Acceptance for 5b:** builder-turn tool context down from ~13k tokens to a stated target; **and** no
regression in cloud tool-calling across a scripted set of turns that between them reach for tools from
several different areas. The second half is the real bar — a scoping layer that saves tokens by hiding
the tool the director needed is a loss.

**Risk:** the search step is itself a hop. It is only a win because the hop is cheap and 13k is not,
which means it can invert on short turns. Measure both.

---

## 6. Phase 5 — a local director in the roster

Now, and not before. A `ROSTER` row plus a `_OpenAICompatibleLLM` subclass setting `base_url` at a
local server.

Point it at **`outdoor` (12 tools) first**, not `builder`. That is the configuration most likely to
work, it already exists, and it isolates "can this model call tools at all" from "can this model
survive 56 of them". Then `builder` with phase 4's scoping.

Runtime and model choice are §8 forks, not settled here. The shape to look for is MoE with native
tool calling; model *names* in this document would be stale within the month.

**Acceptance:** does it complete a scripted `outdoor` turn end to end; time-to-first-token cold vs.
warm prefix cache; full-turn wall clock and hop count against Claude on the same script. The honest
expected result is **slower than the cloud** — the phase is worth running anyway, because it converts
a prediction into a number and because privacy and offline operation do not require it to be faster.

---

## 7. Not in this plan

- **Local image generation.** The gap to Nano Banana on edit / outpaint / 4K-skybox is large and those
  are the ops the world server actually depends on. Stays a 💡 in `providers.md`.
- **MLX Whisper.** Already its own item in [`backlogs/voice.md`](../backlogs/voice.md) with its own
  measurement; this plan must not absorb it.
- **Room cameras.** Phase 2 is deliberately the on-ramp, but the camera work is its own plan.

---

## 8. Forks worth recording

1. **Is a local model a `ROSTER` row, or something else?** A row gets switching, addressing and the
   agent `llms` allow-list for free. It also implies one local model with one casual name, when the
   likely end state is several (a director, a VLM, a judge) behind one runtime.
2. **Is `search_tools` director-hosted (§5.5) or an MCP tool?** Director-hosted keeps it out of every
   server's business and out of `_GatedMCP`'s way; an MCP tool makes it inspectable like everything else.
3. **Which runtime.** ~~Decided by phase 1~~ — **partly settled 2026-09-19, and the question split in
   two.** The *vision* seams (judge, captioner) need a **forward pass, not a server**: reading letter
   logits beats generating a letter, so `torch` + `transformers`, already in the venv for SigLIP, is
   the whole dependency. No runtime. The fork survives only for the **director** (phase 5), where
   generation is the point: LM Studio (MLX backend, OpenAI-compatible) vs. Ollama (scriptable,
   headless) vs. `mlx-lm.server` (fastest on Apple Silicon, least turnkey tool calling).

---

## 9. Open questions

- Does `iogpu.wired_limit_mb` need raising, and what does that do to the rest of the stack under a
  real session? Unmeasured.
- Should SigLIP move to `mps` while we are here? Out of scope, but it is the same contention — and
  phase 1 puts torch on the GPU next to it, so the two now share a budget.
- The HF cache holds an **empty stub** for `Qwen--Qwen3-VL-4B-Instruct` (a `refs/main` and no weights),
  so something reached for this model before and never finished. Worth knowing why before assuming the
  choice is fresh.
- ~~Is there a regression harness for director tool-calling today?~~ **Answered 2026-09-19: no, and
  phase 4 is blocked on it.** What exists is mocked unit tests (`tests/test_director.py`, no real
  model) and tier-3 live canaries (`tests/test_live.py`) whose docstring rules out exactly the thing
  5a and 5b need to measure: *"a few cents per run — one short director turn; never a 4K skybox or a
  **multi-step tool loop**."* Both halves of phase 4 are accepted on "the same tools still get called
  across a scripted set of turns", and nothing can currently answer that.

  So phase 4 has a **phase 0**: a scripted multi-hop tool-calling harness. That is a real piece of
  work, and it reverses a deliberate project constraint on live-test spend — **a call for Daniel, not
  an assumption to make here.** Until then `tool_search` stays off by default, which is how it shipped.
