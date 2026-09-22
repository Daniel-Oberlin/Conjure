# Plan — a figure who looks at you and blinks

**Status:** all phases open · **Opened:** 2026-09-17 · **Blocked on:**
[`test-clips-and-faces.md`](./test-clips-and-faces.md) §2

**This file is temporary.** It holds one sequence across the bone vocabulary, the client runtime and
the face work, and is deleted when the last phase has settled — finished work to
[`specs/figures.md`](../specs/figures.md), deferred or abandoned work to
[`backlogs/figures.md`](../backlogs/figures.md), forks taken to [`decisions.md`](../decisions.md).

**Dissolves to:** `specs/figures.md` — a new **§8f (gaze)** and an addition to **§8d** for the blink
timer · `backlogs/figures.md` for whatever is deferred · `decisions.md` if the clamp or the
gaze-versus-clip precedence turns into a fork worth recording.

---

## 0. Why, and why in this order

Two figures can change expression. Twenty-two can blink from a clip. **None of them look at you**, and
none of them do anything at all with their face while standing still — which is most of the time a
figure is in a room.

Gaze first, because it is the larger presence win, it is unblocked, and Saka is a free first test.

**Both are blocked on phase 2 of the test plan.** A timer or a gaze loop over a driver that turns out
broken just automates the fault, and we have never seen the face work on a headset.

---

## 1. What this is built on

Measured 2026-09-17. Where one of these is wrong, the phase resting on it is wrong.

**Every rig in the corpus has eye bones.**

| figure | eye bones | in the humanoid map? | look morphs |
|---|---|---|---|
| Saka (VRM) | `J_Adj_L_FaceEye`, `J_Adj_R_FaceEye` | **yes — `leftEye`/`rightEye`, stated by the file** | 0 |
| Alice (CC) | `CC_Base_L_Eye`, `CC_Base_R_Eye` | no | **8** |
| Barbie, office-babe (Rigify) | `DEF-eye.L/R`, `DEF-eye_master.L/R` | no | 0 |
| Grace, Trish, Yuffie (Daz) | `eye.L/R` | no | 0 |

**Gaze is an `aim`, not a delta, and that is the whole reason this is tractable.** §8e carries a
rotation between two rigs and therefore depends on their rest frames agreeing — which is why it refuses
Eve Maccaro and cannot cross a naming family. Aiming a bone at a world-space point needs no agreement
at all: `figures.compose_frame` already resolves "point this bone there" from whatever rest a rig has,
and that is exactly `aim`'s existing job. Four naming families is then a discovery-layer-1 convention
table, which is the cheap kind.

**A clip drives eye bones too.** `1_idle_speaking` puts 42.7° of travel into `CC_Base_R_Eye`. So gaze
and clips contend for the same bones and the order of writes decides who wins.

**A blink has two mechanisms.** Saka and Alice blink by morph weight, which `figure-face` already
writes. The other twenty blink with `DEF-lid.*` bones and have no blink morph at all.

---

## 2. Phase 1 — eyes in the bone vocabulary

`leftEye` and `rightEye` added to the semantic bone set, with a convention entry per naming family.

- **Saka needs none of it.** VRM states the mapping, `vrm_humanoid` already reads it, and she is mapped
  today. Use her to prove the runtime before touching discovery.
- The other three families are a name table: `CC_Base_{L,R}_Eye`, `DEF-eye.{L,R}`, `eye.{L,R}`.
- Prefer `DEF-eye.L` over `DEF-eye_master.L` and `DEF-eye_iris.L` — the master is a control and the
  iris is a detail. `prefer_deform` already makes this kind of choice by vertex weight; check whether
  it lands correctly here rather than assuming.

**Verify:** `FRAME_REV` bumps, `refresh-models` re-derives, and `scripts/faces.py` (or a new column)
shows how many figures gained eyes. Expect all of them.

**Trap:** adding bones to `CORE_BONES` changes `rig_signature`, which invalidates every cached clip
pairing. Check whether eyes belong in the signature at all — a rig that differs only in eye naming is
the same rig for clip purposes.

**Dissolves to:** `specs/figures.md` §2 (the vocabulary) and §3 (discovery).

---

## 3. Phase 2 — the gaze component

A client component that aims both eyes at the active camera each frame.

| decision | the answer, and why |
|---|---|
| when it runs | **after** the mixer. A clip writes eye bones and gaze should win while it is on. |
| clamp | **~30–35° off centre**, and this is the phase's whole risk. Human eyes do not exceed it; past that a head turn does the rest. Unclamped gaze at a wearer walking behind the figure is the classic uncanny failure and **worse than no gaze at all**. |
| beyond the clamp | eyes hold at the limit. Whether the head follows is phase 2b, and should be slow. |
| who to look at | the active camera. Multi-user is out of scope and should be said rather than assumed. |
| off switch | a tool call, and a default. A figure that never looks away is also wrong. |

**Verify:** on a headset, and Saka first. Walk around her. The failure to watch for is not "she does
not look" but "she looks wrongly" — eyes crossed, eyes rolled past the socket, or gaze that snaps
rather than moves.

**Cross-check available for free:** Alice has eight look morphs *and* eye bones. Driving her both ways
should agree about where she is looking. If they disagree, one of them is wrong and the morphs are the
one with a geometric oracle behind them.

**Dissolves to:** `specs/figures.md` **§8f**.

---

## 4. Phase 3 — auto-blink, morph path

Saka and Alice. `figure-face` already writes the weight, so this is the timer and nothing else.

- **Scope by "is anything driving this face", not by figure.** A capture figure blinks while a clip
  plays and stops the moment it stops, so the rule is per-moment. That also settles composition: the
  timer yields to a clip rather than fighting it.
- **Interval 3–5 s with wide spread.** A fixed period is the thing people notice. Occasional doubles
  are a nice-to-have, not the point.
- **Closure 100–150 ms**, not a frame. A single-frame blink is invisible at 72 Hz and reads as a glitch.

**Verify:** watch for thirty seconds. If you can predict the next blink, the jitter is wrong.

**Dissolves to:** `specs/figures.md` §8d.

---

## 5. Phase 4 — auto-blink, bone path

The other twenty figures, which have no blink morph.

**Derive the closed pose from the recorded performance rather than authoring one.** `pc_blink`'s
most-closed frame is a measured 26.4° delta per lid bone, on a rig sixteen figures share. Extracting a
blink pose per rig from a clip we already have beats authoring one against rigs we cannot see — and it
is the same move §8e makes, minus the cross-rig risk, because here the clip and the figure are the same
rig.

**Open, and to be answered by measurement not preference:** whether this is a stored named facial pose,
or a short clip played on the eyelid bones only. The second composes with a body clip if the track sets
are disjoint, which they are — but that is a claim to test, not to assume.

**Dissolves to:** `specs/figures.md` §8d, or `backlogs/figures.md` if the derivation does not hold up.

---

## 6. Not in scope

- **Gaze aversion, saccades, blink-on-speech.** Real gaze breaks and re-establishes, and blinks
  correlate with speech and saccades. All of it is better than a constant stare; none of it is worth
  building before the constant stare works.
- **Multi-user.** One camera.
- **Pupil dilation, eyelid follow.** An eyelid tracks the eye in a real face. Noted, not planned.

---

## 7. Where each result goes

| result | destination |
|---|---|
| a phase works | the spec section named in that phase, with the measurement |
| a phase is deferred | `backlogs/figures.md`, with what would unblock it |
| the clamp or the gaze/clip precedence becomes a judgement call | `decisions.md` |
| it looks wrong in a way that resists diagnosis | `investigations/facial-expression.md` |

Delete this file when phases 1–4 have settled.
