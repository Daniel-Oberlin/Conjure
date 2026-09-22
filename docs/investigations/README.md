# Investigations

A **spec** says what the system does. A **backlog** says what it should do next. Neither has room for
what we *tried* — and that turns out to be the expensive knowledge.

An investigation is the durable record of one debugging campaign: the symptom, the experiments, what
each one proved, what was **ruled out**, and what is still theory. Its purpose is that the campaign
never has to be re-run, and that a fix already shown not to work is not re-proposed a month later with
enthusiasm.

## What belongs here

A doc earns a place here when it has produced **negative knowledge** worth keeping — a hypothesis
eliminated by measurement, a fix tried and rejected, a platform limit confirmed. A bug that was simply
found and fixed does not need an investigation; the fix and its test are the record.

### One legitimate guest: a device-run checklist

A doc that plans a run — *what to do on the headset, what to look at, what a failure would mean* — has
none of the required sections below, because nothing has happened yet. It still belongs here rather than
in [`plans/`](../plans/), which means something narrower: a cross-area sequence that dissolves into the
specs. A run checklist is single-area and its output is measurements, not spec content.

So it lives here as a **consumable**. When the run is done it either grows into a real investigation —
symptom, experiments, what was ruled out — or it is deleted and its findings go to the backlog, as
`figures-device-run-2026-09.md` was. A checklist that is still here months after its run is neither.

## Required sections

Every investigation carries these, whatever else it has:

| Section | Why |
|---|---|
| **Symptom** | what was actually observed, in the observer's words, with the conditions |
| **Experiments and what each proved** | one row per experiment — the result *and* the conclusion drawn |
| **Tried and rejected** | the load-bearing section. Each entry says what was tried, why it failed, and what new evidence would justify retrying it |
| **Remaining theories** | with a likelihood, and how each would be tested |
| **Fixes shipped** | symptom → cause → fix → knob → commit, so the record ties to code |

The "tried and rejected" entries should be written to be read by someone about to suggest that exact
thing. Say what would change your mind.

## Current investigations

| Doc | Subject | Outcome |
|---|---|---|
| [`pops-and-jitters.md`](./pops-and-jitters.md) | visible motion that shouldn't be there — seams, cracks, content shimmer, and the walking micro-stutter | five fixes shipped; the residual stutter diagnosed as a **WebXR/Quest platform limit** (dropped-frame positional reprojection during translation), our code exonerated by measurement |
| [`wall-art-behind-wall.md`](./wall-art-behind-wall.md) | a wall-art image intermittently lands *behind* its wall | Fix A landed; **Fix B tried and rejected** — do not retry without new evidence |
| [`stt-accuracy.md`](./stt-accuracy.md) | speech-to-text gets words wrong — is it the recognizer or the microphone? | model fix shipped (`base` → `small.en`, measured); **four options rejected**, two by measurement; mic-vs-model still open pending a real-audio corpus |
| [`rooms-device-run-2026-09.md`](./rooms-device-run-2026-09.md) | rooms as a first-class unit — the whole feature, unexercised on hardware | **plan, not run.** Written before the run: does segmentation find this space's three rooms, does a name survive losing its room's floor, and how far apart are the rooms really — the measurement step 3 was deferred for |
| [`raised-floor.md`](./raised-floor.md) | a room's floor renders inches above the real one, on and off for days | cause established — the Quest anchors that room's stored entity high; three hypotheses **killed by measurement**, and a re-scan does not clear it. **Four correction attempts, all reverted** — the work sits unmerged on `feat/fix-floating-rooms` with a resume guide |
| [`surface-churn.md`](./surface-churn.md) | surfaces drop out and come back without their colour | first cause found and **fixed** (entering AR on a partial space → a frame ~17 cm out → originals pruned with their materials; the load gate). **Reopened 2026-09-09**: a second path reaches the same symptom — `/space/capture` accepts a destructive capture from a client that has not selected a space yet, so the gate never sees it. Cost a whole space's styling |

| [`facial-expression.md`](./facial-expression.md) | a figure who never blinks or changes expression, with no mechanism to make her | **closed 2026-09-16.** Driving a face shipped (§8d) and carrying one through a retarget shipped (§8e). The value is in the seven rejected approaches: two wrong frames for the gate (world reads 175° on rigs that agree; head-relative depends on both bone maps picking the same vertebra), a name match that is not evidence (Eve's lids are identically named and rest INVERTED), a composite held to a direction it cannot have, and a verdict of "marginal" reached by sampling a directional matrix along one axis |
| [`bride-eyelid.md`](./bride-eyelid.md) | a composed figure's left lower eyelid does not show while she animates | **opened 2026-09-15.** Cause NOT established. One fix — rebinding the donor's inverse bind matrices — was exact as arithmetic and **much worse on device**, because it put her body in a different frame from her clothes; reverted. The measurement that motivated it turns out not to explain one missing lid. Paid for a real bug on the way (`library.revive`) |
| [`figures-frame-rate.md`](./figures-frame-rate.md) | ~32 fps sustained with three figures in the room, on a 90 Hz display | **opened 2026-09-09**, one measurement. Distinct from `pops-and-jitters` (that one is *rare* drops on a healthy session; this is chronic, and *worse standing still than walking*). Cause unknown; the empty-room run splits the theory table |


## Related

- [`docs/specs/`](../specs/) — what the system does today.
- [`docs/backlogs/`](../backlogs/) — what it should do next. Open theories from an investigation are
  cross-linked from the relevant backlog.
- [`docs/decisions.md`](../decisions.md) — consequential forks and the reasoning behind them.
- [`docs/backlogs/`](../backlogs/) — currently-open problems, per subsystem, not campaigns.
