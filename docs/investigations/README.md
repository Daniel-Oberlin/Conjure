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

## Related

- [`docs/specs/`](../specs/) — what the system does today.
- [`docs/backlogs/`](../backlogs/) — what it should do next. Open theories from an investigation are
  cross-linked from the relevant backlog.
- [`docs/decisions.md`](../decisions.md) — consequential forks and the reasoning behind them.
- [`docs/known-issues.md`](../known-issues.md) — currently-open user-visible issues, not campaigns.
