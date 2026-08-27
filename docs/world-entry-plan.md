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

> **Second principle — every degradation is audible.**
>
> A fallback that fires silently is indistinguishable from a bug, and it is how all five of these went
> unnoticed for so long. Whenever the system gives you something other than what the pointer asked for,
> it says so, in one line, on the same channel that already carries the unasked-agent-change notice.

| Degradation | Told you today |
|---|---|
| new world got no space (someone else's private space) | no |
| boot couldn't restore what the pointer named | log only, not broadcast |
| the world/session you were in was deleted, so you got another | no |
| a space matched but you were deliberately left where you are (C2) | n/a — new |
| the constructor failed and the switch was abandoned | n/a — new |

The mechanism exists and there is precedent: an agent change nobody asked for is already announced
(decision #20 — "kept, made audible"). This extends the same courtesy to the other five.

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

#### C2b — a deliberately-void world is not relocated by a space match

Today, if you leave and restart while in an outdoor world and then put the headset on, **you are pulled
out of it into whichever agent last used the space you're standing in.** Traced: the client votes the
live capture against the geo candidates *even in a void world*, commits the match, and the server joins
that space's remembered world — in that space's remembered scope, which is usually the builder.

The client comment says exactly why it works that way:

> *Runs even when the active world is VOID … that's exactly when we must vote the live capture against
> candidates to find/mint the physical room — otherwise selection can never resolve (the old
> `!isVoidWorld` gate is what left an outdoor re-entry stuck on "finding").*

So the voting is right and must stay. What went wrong is that **two separate things were fused**:

1. **resolving which physical space you are in** — needed always, even in a void world: for the
   boundary, for occupancy, and so that a *later* switch knows where you are;
2. **acting on that by moving you to that space's last world** — which is exactly what you don't want
   when you deliberately chose to be nowhere.

Splitting them is the fix, and **C2 already supplies the discriminator**, which is why this belongs
here rather than in its own change:

| Live world's space | A headset establishes a space | Why |
|---|---|---|
| **unset** | claim it, and relocate — this *is* the provisional-boot case | nobody chose; spatial truth beats a temporal guess |
| **void** | claim it for boundary and occupancy, **do not relocate**, say so | the current world is a deliberate choice to be nowhere |
| a reference | unchanged (match → admitted, mismatch → refused) | already correct |

The existing rule — *"spatial truth supersedes the temporal guess"* — stays true and gets sharper: it
applies to a **guess**. A void world is not a guess.

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

### C6 — an agent declares whether its worlds want a space

**Found while answering §5.3, and it is a regression from the space-stamp fix.** `outdoor` is a
per-request parameter on world creation; an agent cannot declare that its worlds are room-less. The
outdoor agent's declared opening doesn't say so either — it only sets a sky. So now that every mint path
stamps the live space, switching to the outdoor agent for the first time while standing in a captured
space mints a world **with the whole room composed into it**. Measured on the real capture:

```
SWITCH TO OUTDOOR (first ever):  world: default   persisted space: daniel/space-1
                                 real surfaces composed: 59
```

The outdoor agent exists to put you *somewhere else*. Its own worlds on disk are all `<void>`, because
they were made by an explicit request that passed the flag; only the constructor-built first world is
wrong, and only when a space is live.

The fix is to let the agent say it: `world.outdoor: true` in the agent definition (default `false`),
consulted wherever a world is minted for that scope. That is a smaller and more honest change than
threading a flag through every arrival, and it makes §5.3 stop being about permissions:

- agent wants a space, and can have one → stamp it
- agent wants a space, **can't** have one (someone else's private space) → void, **and say so**
- agent doesn't want a space → void, silently, which is correct

## 5. Decisions

1. **Reset and assets — both, keep by default.** `reset agent <name>` leaves the catalog alone;
   `--assets` (or `reset agent <name> assets`) also purges rows in that scope. **Purely disk hygiene —
   it does not affect whether a first-run test is genuine.** (An earlier draft of this line claimed the
   constructor would otherwise reuse a cached skybox. It won't: procurement runs the generator
   unconditionally and only write-throughs the result afterwards — there is no catalog lookup on the
   generate path at all. Reuse is explicit-only, via the director calling `search_library`, which a
   constructor step never does. That is the library's founding premise: generation is non-deterministic,
   so the same prompt yields different bytes and a fresh call every time.)
2. **A failing constructor aborts.** See §5a below for the shape.
3. **Revised — notice, not refuse.** See §5b.
4. **`reset agent <name>` as a verb**, not extended deletion. A reset is several deletions plus pointer
   surgery; spelling it as `delete` would mean trusting that a delete is "really" doing a reset.

### 5a. What aborting means

The design constraint that makes abort cheap: **nothing is written until every fallible step has
succeeded.** Session creation already works this way — the generative steps run into patch ops *before*
any directory, world file or pointer is touched, so a failure has nothing to roll back. `enter` must
preserve that property rather than construct-then-fix-up.

Given that, "abort" is a **no-op**, and the answer to *where do you go* is **nowhere — you stay exactly
where you were**, with a notice saying why the switch didn't happen. Nothing was created, no pointer
moved, the live world is untouched. Not the shell: dropping someone into command mode is a bigger
disruption than the failure warrants, and in a headset the shell means nothing.

Two arrivals can't "stay where they were", because there is nowhere to stay:

| Arrival | On constructor failure |
|---|---|
| agent / session / world switch | **no-op + notice.** You stay put. |
| **boot** | can't refuse — the server must come up. Fall down the §2 chain; last resort is a plain world with no constructor, logged loudly. |
| **space established by a headset** | refuse the selection; the headset stays in passthrough with a message. The mechanism already exists — it's what a private-space refusal does. |

The rule underneath: **abort at the outermost point that still leaves a consistent state.**

One honest limit. The greeting is generated by the *agent server*, on its next reconcile, after the
world server has already committed — so a greeting failure cannot abort anything. Today it marks the
session greeted anyway so it never retries, which I think is right: a missing opening line is not an
inconsistent session. So "the constructor is atomic" is true of the world-server half (generative steps,
world build, state seed) and not of the greeting.

### 5b. What an "implicit arrival" into someone else's private space actually is

Concretely, one situation: **you are a co-located guest.** Another user established the space, it is
private, and the admission gate let you in because you are physically in it. You then switch agents (or
your session is restored) — and the world minted for you can't be built in their space, so it comes out
with no space at all.

I proposed refusing. **That's wrong**, because C6 shows void is sometimes the *correct* outcome — an
outdoor agent's world should have no space, and refusing to switch to it would be absurd.

So the distinction isn't refuse-vs-void, it's **silent-vs-explained**. Landing in a space-less world
without being told is the failure this plan is about; landing in one having been told *"you can't build
in Bob's private space, so this world has no room in it"* is fine, and leaves you free to carry on or
switch back. Combined with C6, the case stops needing a special rule at all.

## 6. Execution order

Each step is independently testable and leaves the tree green.

1. **C6** — `world.outdoor` in the agent definition. Goes first because it is a live regression: the
   outdoor agent currently inherits your room on a first switch. Small and self-contained.
2. **C3** — declarative session visibility. Same shape as C6, same place in the definition; unblocks the
   erotic agent immediately.
3. **C2** — unset vs. void. Pure data-model change with one new behaviour; needs a test that a world
   minted before space selection later adopts the space.
4. **C1** — the shared entry routine, carrying the abort rule from §5a. The refactor. No behaviour
   change intended beyond the opening now running on every arrival; the existing suite is the regression
   net.
5. **C4** — the fallback chain, inside the routine C1 just created.
6. **C5** — agent delete + `reset agent <name> [--assets]`, which is also how 1–5 get exercised.

C6 and C3 are both "a per-agent default that currently lives somewhere it shouldn't" — one in a
per-request flag, one in prompt prose. Doing them together keeps the agent-definition change to a single
pass over the loader and its validation.

## 7. Spec changes this implies

- **`specs/agents.md` §3** — the agent definition gains `session.public` and `world.outdoor`, and the
  table of validated fields grows two rows.
- **`specs/agents.md` §7.5** — the constructor runs on *every* session mint, not just the explicit one;
  `first_world` is honoured everywhere; the abort rule (§5a) and its one limit (the greeting is outside
  the atomic half) are stated.
- **`specs/agents.md` §9.1** — the pointer-restore rules become the fallback chain.
- **`specs/spaces.md` §4.2** — three space states; the boot opt-out becomes "unset", and space selection
  may claim an unset world.
- **`specs/agents.md` §6.3** — the new shell verb.
- **Backlogs** — the constructor item in `backlogs/agents.md` is resolved by C1; the two unrecorded
  items (wrong-agent recovery, space-less restore) never need writing up because they're fixed here.
