# World entry — a plan

> **Temporary.** Working document to iterate on, then execute. When it's done its content folds into
> [`specs/agents.md`](./specs/agents.md) §7.5/§9, [`specs/spaces.md`](./specs/spaces.md) §4.2 and the
> matching backlogs, and this file is deleted.

Five reported problems. They are **one problem**, and patching them separately would leave the shape
that produced them intact.

---

## 1. The diagnosis

There are six ways to arrive at a live world:

| # | Arrival | Trigger |
|---|---|---|
| 1 | boot restore | server start |
| 2 | agent switch | `agent <name>` |
| 3 | session switch | `session <name>` |
| 4 | session create | `session new` |
| 5 | space established / rejoined | a headset entering AR |
| 6 | world create | `new_world` |

All six converge on **one shared tail** — persist the outgoing world, point the store at the incoming
one, broadcast. That part is fine and is not what this plan touches.

**What they do not share is a head.** Each one independently answers the same three questions before
reaching the tail:

> *Which session? Does it exist — and if not, what do I create?*
> *Which world? Does it exist — and if not, what do I build?*
> *What space does the result belong to?*

Six independent answers to three questions is thirteen opportunities to forget something, and the
record shows they were duly forgotten:

| Obligation | Honoured by | Forgotten by | Symptom |
|---|---|---|---|
| tie the new world to the live space | world create | 1, 2, 3, 4 | switching agents dropped you out of your own room *(fixed)* |
| mark a new session un-constructed | session create | 1, 2, 3 | a switched-to agent never seeded state or greeted *(fixed)* |
| run the agent's declared opening | session create | 1, 2, 3, 5 | first switch to an agent gives a bare world, no sky |
| keep the agent you were using | 2, 3, 4, 6 | 5 | a deleted world sends you back as the builder |
| survive a missing pointer sensibly | — | 1, 5 | a deleted session strands you in a space-less world |

Two of these are fixed, by moving the obligation to a shared chokepoint. The remaining three can't be
fixed that way — the opening involves image generation, which is slow, asynchronous and has to announce
itself, while the chokepoint is synchronous.

**So the fix is to give the six arrivals a shared head, not to add a fourth patch.**

## 2. One behavioural principle worth adding

Three of the five problems are the same user-facing failure: *the thing you were pointed at is gone, so
the system gives you a global default instead of the next-best version of what you actually wanted.*

- Your world was deleted → you get **the builder's** area, whichever agent you were with.
- Your conversation was deleted → you get **an empty space-less world**, not that agent again.

The user's intent in both cases is the same and is still perfectly recoverable: *put me back with the
agent I was using, in the space I'm standing in.* The current fallbacks throw away both facts.

> **Principle — degrade to the next-broadest thing that is still true, never to a global default.**
>
> - world missing → another world in that session → else that agent's freshly-constructed opening world
> - session missing → that agent's most recent other session → else a new, properly-constructed one
> - agent unknowable → **only then** the default agent

This is a simpler rule than what exists, and it is the whole fix for problems 3, 4 and 5.

## 3. The changes

### C1 — one entry routine (the shared head)

A single `enter(scope, session=None, world=None)` that guarantees the invariants and then calls the
existing tail. Every one of the six arrivals becomes a thin call into it:

```
enter(scope, session?, world?):
    session ← the named one, else the scope's last-used, else CREATE (marked un-constructed)
    if the session was created → run the full constructor (below)
    world   ← the named one, else the session's last-used, else CREATE
    if the world was created  → world-level setup ⊕ the agent's opening ⊕ adopt the live space
    → tail (persist outgoing, install incoming, broadcast)
```

It is `async`, because every caller already awaits — which is precisely what makes the generative
opening reachable from an agent switch, not just from `session new`.

**What this deletes:** the bare-`default` mint in boot, the bare-`default` mint in agent switch, the
bare-`home` mint in session switch, and the separate world-building limb inside session create. Four
near-duplicate blocks become one. It also ends the accident that a world minted by one route is called
`default` and by another `home`, for no reason a user could explain.

**Constraint to respect:** the tail persists the *outgoing* world before switching, and the session
pointer must flip in the right order or the outgoing world is written into the incoming session. That
ordering is subtle, already correct, and must not be disturbed — `enter` prepares, the tail switches.

### C2 — three space states, not two

Today a world's space reference is either a real space or the void sentinel, and **absent collapses to
void**. That is why a world minted at boot — before any headset has said which space it is in — is
*permanently* space-less rather than merely space-less *for now*.

Split the two meanings that are currently one:

| State | Means | Renders as | Adopts a space later? |
|---|---|---|---|
| **unset** | no space known *yet* | nothing real (same as void) | **yes**, on the next space selection |
| **void** | deliberately outdoor | nothing real | never |
| a reference | that space | its geometry | n/a |

Rendering is unchanged, so no client work. The only new behaviour is that space selection may claim an
*unset* world. This does **not** resurrect the anonymous-default fallback that was deliberately
removed — nothing is guessed; the decision is deferred until a headset actually knows the answer.

### C3 — the agent declares its session defaults

Add `session.public` to the agent definition, default `true`. The constructor writes it; the
session-ensure path reads it too, so implicit mints honour it as well.

Then the erotic agent declares `"public": false` and **the instruction telling it to make itself
private is deleted from its prompt.** Privacy stops depending on a language model remembering to act.

This is the smallest change here and the one with the clearest argument: a per-agent default belongs in
the agent's declaration, not in its prose.

### C4 — the fallback chain

Implement §2 inside `enter`, so all six arrivals inherit it. Concretely, this removes the hard-coded
builder scope from the space-selection path: when a space's remembered world is gone, the replacement is
built **in the scope that space was last used from**, and only falls back to the default agent when that
scope no longer exists at all.

### C5 — resetting an agent

Two gaps today. An agent path is listable and inspectable but **cannot be deleted** — the deletion
surface handles users, sessions, worlds, spaces and assets, and falls through on an agent. And every
deletion refuses to touch whatever is currently live ("switch away first"), which is exactly what you
must do to test a first run.

Add both halves:

- **Agent-level delete** — purge every session (transcripts, state, worlds) in that scope. Assets are a
  separate question (§5).
- **`reset agent <name>`** — a shell verb that encapsulates the dance: switch away if it's live, purge,
  clear any pointer naming it, and (optionally) re-enter it so you land on a genuine first run. Without
  this, testing a first run means three manual steps and hand-editing a pointer file — which is what we
  actually did, twice.

`reset` and C4 are the same work seen from two sides: a reset deliberately creates the dangling pointers
that C4 makes survivable. Building either without the other leaves a trap.

## 4. What the user sees afterwards

| Situation | Today | After |
|---|---|---|
| switch to an agent for the first time | bare world, no opening | that agent's world, its name, its sky, its greeting |
| switch back to an agent | where you left off | unchanged |
| headset on, in your space | the world you left | unchanged |
| …but that world was deleted | a new world, as the **builder** | a new world, **with the same agent**, in your space |
| …your conversation was deleted | empty, space-less world | a fresh conversation with the same agent, adopting your space when the headset locks on |
| starting a private-by-nature agent | public until it thinks to say otherwise | private from the first line |
| testing a first run | edit files by hand | `reset agent <name>` |

## 5. Open questions — please decide

1. **Does a reset delete the agent's assets too?** Generated skyboxes and models in that scope are
   expensive to recreate and arguably not part of "the conversation". My inclination: **no** by default,
   with `reset agent <name> --assets` to include them. But a first-run test of an agent whose opening
   *generates* a skybox will silently reuse the cached one, which is not a true first run.
2. **When the constructor's opening fails** (image generation errors) on an *implicit* arrival — should
   the switch fail, or land you in a plain world with a notice? Session create currently fails hard,
   which is right for an explicit request. For an agent switch I lean to **notice and continue**: not
   being able to switch agents because an image API is down is worse than a missing sky.
3. **The private-space asymmetry** (introduced by me): an explicit `new_world` in someone else's private
   space *refuses*, while an implicit arrival degrades to a space-less world. Under §2's principle,
   should the implicit arrival instead refuse to enter and leave you where you are? I now think **yes** —
   silently landing somewhere with no space is the exact failure this plan is trying to end.
4. **Naming.** `reset agent <name>` vs. extending `delete` to agent paths and leaving the dance manual.
   I prefer the verb, because the dance is the part that's easy to get wrong.

## 6. Execution order

Each step is independently testable and leaves the tree green.

1. **C3** — declarative session visibility. Smallest, no interaction with the rest; unblocks the erotic
   agent immediately.
2. **C2** — unset vs. void. Pure data-model change with one new behaviour; needs a test that a world
   minted before space selection later adopts the space.
3. **C1** — the shared entry routine. The refactor. No behaviour change intended beyond the opening now
   running on every arrival; the existing suite is the regression net.
4. **C4** — the fallback chain, inside the routine C1 just created.
5. **C5** — agent delete + `reset`, which is also how 1–4 get properly exercised.

## 7. Spec changes this implies

- **`specs/agents.md` §7.5** — the constructor runs on *every* session mint, not just the explicit one;
  the agent definition gains `session.public`; `first_world` is honoured everywhere.
- **`specs/agents.md` §9.1** — the pointer-restore rules become the fallback chain.
- **`specs/spaces.md` §4.2** — three space states; the boot opt-out becomes "unset", and space selection
  may claim an unset world.
- **`specs/agents.md` §6.3** — the new shell verb.
- **Backlogs** — the constructor item in `backlogs/agents.md` is resolved by C1; the two unrecorded
  items (wrong-agent recovery, space-less restore) never need writing up because they're fixed here.
