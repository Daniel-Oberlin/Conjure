# Space Model (Current Implementation)

Author: GitHub Copilot
Date: 2026-07-08
Source of truth: running code in client and server modules (not forward-looking design docs)

Before the details, a useful mental model is: there are two frames the code directly cares about, plus a likely deeper system frame it does not directly access. First, WebXR gives the session a raw `refSpace`, which is the current tracking frame for poses and detected planes and can shift when tracking resets or relocalizes. Second, Conjure derives its own stable room-aligned reference frame from room geometry and remembered surfaces, then keeps trying to recover the rigid transform from the current raw `refSpace` back into that stable frame. That transform is `Tmat`.

If you want to imagine a deeper Quest-side capture frame created during Space Setup, that is a reasonable intuition, but this code does not directly receive or manipulate that frame. What it actually sees is live detected geometry in the current WebXR frame. Before registration exists, the client bootstraps rendering from a WebXR anchor if available, otherwise identity. On first authoritative establishment for an owner with no prior reference surfaces, it promotes that bootstrap transform to `Tmat`; after the first capture, the durable frame becomes geometry-defined and later sessions recover against that geometry. At render time, `world-root` is moved by the inverse of `Tmat`, so content authored in Conjure's stable frame appears in the correct real-world place in the current session.



## 1. Purpose and terminology

This document replaces the conceptual role that room-model.md used to play, but in current terms and based on what the code does now.

Current system behavior is a space-first model with worlds attached to spaces, including cross-owner space references.

## 1.1 Quick glossary
| Term | Meaning in current code |
|---|---|
| space | Persistent physical-environment record: real surfaces, boundary, geolocation, visibility, and last-used world pointer. |
| world | Persistent authored scene: placed content, world-level prefs, visibility, and per-surface material overrides. |
| active world | The one world document currently loaded into the server's live store and broadcast to clients. |
| active space | The physical space currently composed into the active world. |
| refSpace | The raw WebXR reference space returned by the browser for current tracking poses. |
| raw detected plane | A current WebXR plane-detection observation, before Conjure assocaites it with a stable room surface. |
| reference constellation | The client-side set of remembered surfaces used as the stable geometric target for registration. |
| Tmat | The rigid transform from the current raw WebXR frame into the stable reference frame. |
| world-root | The A-Frame entity that parents world content and is moved so reference-frame content renders in the correct physical place. |
| frame lock | The state where the client trusts its current registration transform. |
| relocalization fallback | Safety mode where virtual content is hidden and passthrough stays visible until registration lock returns. |
| static surface | Room-shell architecture that should settle and then freeze: wall, door, window, wall art, floor, ceiling. |
| non-static surface | Any captured surface outside that static set; these remain live-updated and prunable. |

## 2. High-level flows


Before diving into data structures and algorithms, it helps to name the main end-to-end flows the code currently supports.

### 2.1 AR entry when no headset currently holds the space


This is the "I am entering AR while no headset currently holds the active physical space" flow.

High-level stages:
- The headset enters AR and reports coarse geolocation.
- The server returns nearby candidate spaces, if any.
- The headset captures raw detected planes and tries to geometrically match them against those candidates.
- If a candidate matches and the active space is currently unclaimed, the server joins that space's last-active world, or creates a default world in that space if none exists.
- In that matched case, the client then registers the new live capture into that space's reference frame and reorients world-root accordingly.
- If no candidate matches and the active space is currently unclaimed, the server creates a new space for the connecting user and creates a new world tied to it.

- The owner headset starts posting geometry to /room and becomes the room authority for that live session.


### 2.2 Guest visits a space that already has an active holder


This is the co-located multi-user AR flow.

High-level stages:

- A guest headset enters AR while another AR headset is already holding the active space.
- The guest still reports geolocation and still performs the same candidate-space geometric vote.
- If the guest matches the active space, the server admits the guest.

- The guest then becomes register-only: it aligns itself to the authoritative space geometry but does not author or upload room geometry.
- If the guest does not match the active space, the server refuses admission and the client blanks the world to passthrough-only.

### 2.3 Owner leaves and returns to the guardian boundary

This is not a new-space flow. It is a relocalization flow inside the same physical space.

Common real-world events where this flow can appear:
- User presses the recenter/reset-frame control.
- User puts the headset down and later picks it back up.
- User leaves the guardian boundary and re-enters.

These are possible triggers, not a guaranteed one-to-one mapping to any single detection signal.

High-level stages:
- There are two detection signals for a possible frame jump:
- Signal A (runtime event): WebXR emits a reference-space `reset` event.
- Signal B (geometry mismatch): registration against the persistent reference constellation fails, even if no `reset` event was observed.
- On either signal, the client immediately re-captures and tries to recover the rigid transform from the current raw capture back into the persistent reference constellation.
- If the mismatch path persists for about 3 seconds, lock is treated as lost and relocalization fallback is shown.
- If registration is confident, the world stays aligned.
- If registration is not confident, the client temporarily hides the virtual world and shows a relocalization fallback until the lock returns.

### 2.4 Void or outdoor world without a stored physical space

This is a different path from normal room-backed spaces.

High-level stages:
- The active world declares environment.space = <void>.
- No stored space geometry is loaded.
- The client derives a deterministic canonical frame from live walls when available.
- No room geometry is posted or authored into the server's persistent space store for that world.

These four flows are the important ones to understand before looking at individual functions.

## 3. Core concepts and definitions

### 3.1 What is Three.js?

Three.js is the low-level 3D math and rendering library underneath A-Frame in this project. The code uses its vectors, quaternions, matrices, Euler angles, and geometry helpers directly.

In practice here, Three.js is used for:
- coordinate and transform math
- plane normals and basis vectors
- registration transforms
- custom geometry such as holed walls

### 3.2 What is A-Frame?

A-Frame is the higher-level entity/component framework used to build the browser XR scene. It sits above Three.js and provides declarative scene entities such as a-entity, a-plane, a-sky, and custom components.

In practice here, A-Frame is used for:
- the scene graph the headset renders
- custom components like room-capture, surface-edges, billboard, fill-visible
- mapping world document entities into rendered XR objects

Relationship in the larger system:
- WebXR provides raw headset/session/device data.
- A-Frame owns the browser scene and XR session integration surface.
- Three.js provides the math and underlying render objects used by A-Frame and custom components.
- Conjure's client code bridges WebXR plane detection into A-Frame entities using Three.js math.

One terminology note: this project runs in the browser on top of WebXR, not as a native app directly against OpenXR. Some concepts overlap at a high level because both APIs describe tracked spaces, poses, and spatial data, but the code in this repository is reading WebXR browser objects such as reference spaces, poses, and detected planes. Terms like static surface and non-static surface are Conjure terms layered above that API surface, not standard platform terms from WebXR or OpenXR.

### 3.3 What is a space?

A space is the persistent physical-environment layer. It stores captured room-like geometry and boundary information independent of any one world.

In current code, a space contains:
- the set of real surfaces
- the boundary
- ownership and visibility metadata
- geolocation metadata
- a pointer to the last world used there

The important architectural point is that a world does not own the room shell. The space does.

### 3.4 What is a world?

A world is the authored scene layer: placed models, images, skybox choices, world visibility, and per-surface style overrides. A world references a space and is composed with it at runtime.

### 3.5 What does cross-owner space reference mean?

It means the active world can belong to one user while the physical space it is built in belongs to another user.

Example:
- Daniel owns space daniel/kitchen.
- Harold creates his own world in Daniel's kitchen.
- Harold's world stores environment.space = daniel/kitchen.
- Harold owns the world document.
- Daniel still owns the underlying physical-space record.

That is why the code tracks active_world owner and active_space owner separately.

### 3.6 What are active world and active space?

Active world:
- the one world document currently loaded into the server's live store and broadcast to clients.

Active space:
- the physical space record currently composed into that live store.

These are related but distinct:
- switching worlds may change the active space
- two different worlds may reference the same active space
- active world owner and active space owner may differ

### 3.7 What does compose/decompose mean?

Compose means:
- take a stored world document
- take a stored space document
- merge them into the live runtime document the client actually renders

Specifically, compose pulls:
- placed entities and environment prefs from the world
- real surfaces and boundary from the space
- material overrides from world.environment.room.surfaceStyles onto the real surfaces

Decompose means the reverse at save time:
- strip the live composed document back into two persistent layers
- geometry and boundary go to the space file
- placed content and style overrides go to the world file

Why this matters:
- many worlds can share one physical space
- room capture updates should update the space, not duplicate room shell geometry into every world
- per-world styling can still differ without copying the whole room shell

### 3.7.1 When does persistence happen?

In current server code, persistence is not only "on manual save"; it is a mix of periodic durability and explicit flush points.

The active world (and its paired active space split) is persisted when:
- autosave loop sees `rev` changed (about every 1 second check)
- switching worlds (outgoing active world is saved before switch)
- resetting the active world (`/reset` calls save immediately)
- server shutdown/lifespan teardown (final flush)
- changing public/private on the active world (save is called immediately)

Also, when a freshly created world is activated, it is written to disk during the switch path.

Important split detail:
- persistence writes world content to world storage
- and writes captured geometry/boundary to space storage
- in the same `_save_active()` split/decompose operation

### 3.8 What is the boundary?

Boundary is the server/client concept for the overall footprint and coarse extent of the physical space.

In current runtime it is primarily:
- floorPolygon: a polygon in floor coordinates
- height: coarse vertical extent, currently usually seeded as 2.6 from floor capture path

It is used as:
- a compact spatial summary of the physical space
- a placement/bounds reference for higher-level tools
- part of what persists with the space

It is not a full mesh. It is a simplified enclosure description.

More concretely:
- floorPolygon is the polygon of the currently chosen floor plane, expressed in the floor plane's own local coordinates as captured by the client.
- In the current client path, boundary is only emitted when a floor plane is present.
- height is currently a coarse scalar paired with that polygon, not a separately measured ceiling-fit model.

What this means operationally:
- boundary tells the system roughly where the floor footprint is and how tall the enclosing volume should be treated.
- boundary is much cheaper and more stable than carrying full wall polygons or a dense scan mesh for every high-level reasoning task.
- boundary is useful even when individual walls are noisy, absent, or still settling.

What boundary is not:
- it is not the same thing as the full set of room surfaces
- it is not a dense room mesh
- it is not an occupancy grid or collision mesh
- it does not encode every doorway, recess, or wall articulation

### 3.9 What is the WebXR reference space?

The WebXR reference space is the raw coordinate frame the browser gives the XR session for poses and detected geometry. Plane poses from frame.getPose(plane.planeSpace, refSpace) arrive in this frame.

Important properties in this system:
- it is the immediate source frame for detected planes
- it can shift relative to the real room when tracking relocalizes
- by itself it is not stable enough to serve as persistent room identity

### 3.10 What are raw detected planes?

Raw detected planes are WebXR plane-detection results exposed on frame.detectedPlanes.

Each detected plane effectively provides:
- a pose through plane.planeSpace
- a polygon outline
- an orientation or semantic label when available

The Conjure client converts each plane into a temporary capture record containing:
- position
- quaternion
- derived normal
- semantic label
- orientation class (vertical or horizontal)
- extent from polygon bounds

These raw records are not yet stable room surfaces. They are just the latest sensor observations in the current reference space.

### 3.11 What is the registered space/reference frame?

This is the stabilized frame Conjure derives from the room's own geometry.

The client keeps a persistent reference constellation of known surfaces. For each new capture it tries to solve a rigid transform from the current raw WebXR capture into that reference constellation. That transform is Tmat.

Meaning:
- WebXR reference space is the headset's current tracking frame.
- registered space/reference frame is Conjure's stable room-aligned frame.

Once registration is confident, this frame becomes the authoritative frame for the live space.

### 3.12 What is world-root?

world-root is the A-Frame entity that acts as the container for all world content. The client moves and rotates world-root so that content authored in the registered reference frame appears at the correct real-world place in the headset's current raw frame.

This is how Conjure avoids having to rewrite every entity transform when tracking origin changes.

### 3.13 What is a WebXR anchor, and what does bootstrap mean?

A WebXR anchor is a browser/XR object representing a tracked spatial point or frame. In current code it is used only as an early-session bootstrap mechanism.

Bootstrap here means:
- before geometry registration has enough information to lock the room frame,
- the client may create an anchor and use that temporary anchor-derived frame so the world can still render coherently.

After geometry registration succeeds, the geometry-derived transform is authoritative and the anchor is no longer the main source of truth for room identity.

### 3.14 What is a frame lock or geometry lock?

Frame lock means the client has high enough confidence in the transform from the current raw capture into the persistent reference constellation.

Geometry lock is the same idea in room terms: the room geometry has been confidently registered into the stable reference frame.

When lock exists:
- Tmat is trusted
- world-root is pinned from that transform
- surfaces can re-inherit stable IDs and updates can be posted safely

When lock does not exist:
- client holds the previous frame
- may enter relocalization fallback
- avoids posting unstable geometry into the server

### 3.15 What is relocalization fallback?

Relocalization fallback is the safety mode entered when the client believes tracking has lost a reliable geometric lock.

In practice it means:
- hide virtual world content
- hide sky and scaffold
- leave passthrough visible
- show a headset hint telling the user to relocalize
- automatically restore the world when registration lock returns

This is safer than rendering stale geometry in the wrong physical position.

### 3.16 What are static and non-static surfaces?

These are application-defined Conjure categories, not standard OpenXR or WebXR vocabulary.

Static surfaces are the room shell and mounted architectural features:
- wall
- wall art
- door
- window
- floor
- ceiling

These are treated as architecture that should settle and then freeze.

Non-static surfaces are everything else captured through the same ingest path. They remain live-updated and prunable.

How the code determines them is simple and important:
- static means the surface semantic is one of the explicit strings in the server's _STATIC_SEMANTICS set
- non-static means any captured surface whose semantic is not in that set

So non-static is currently defined by exclusion, not by a separate ontology or a second allow-list.

What the code does and does not tell us:
- it explicitly names wall, wall art, door, window, floor, and ceiling as static
- it does not define a formal catalog of non-static semantic labels
- a server comment gives furniture as the motivating example for dynamic behavior, but the actual rule is broader: if a semantic is not in _STATIC_SEMANTICS, it is treated as dynamic/non-static

In practice this distinction exists because room shell geometry should not jitter forever, while movable or less-trusted detected surfaces may continue changing.

### 3.17 Stability terms compared

Several related phrases appear in code comments and can be easy to blur together. They do not mean the same thing.

| Term | Scope | Decided by | What it means |
|---|---|---|---|
| frame lock | client registration | client registration vote | The client currently trusts Tmat enough to align world-root and re-inherit stable surface IDs. |
| geometry lock | client registration | same as frame lock | Informal room-centric phrasing for the same idea: the room has been confidently registered into the stable reference frame. |
| relocalization fallback | client rendering safety mode | client lost-lock timer and recovery path | Virtual content is temporarily hidden because frame lock is not trusted. |
| establishing window | server ingest lifecycle | server timer from first accepted /room post | Initial period during which static shell geometry may still be recomputed and recommitted as a coherent set. |
| settled static geometry | server ingest lifecycle | server static-freeze policy | Existing static surfaces are no longer continuously rewritten from minor capture jitter. |

The key distinction is this:
- frame lock is about whether the client's current coordinate transform is trustworthy right now
- settled static geometry is about whether the server will continue rewriting room-shell surfaces from later captures

You can have one without the other:
- a client can have frame lock during the early establishing window, while the server still accepts coherent static-shell updates
- a room can have settled static geometry on the server while a client temporarily loses frame lock after a guardian-boundary relocalization

### 3.18 Worked example: one raw wall plane becoming one stable real surface

This is a concrete mental model for the most important transformation in the system.

Step 1: WebXR reports a raw detected plane
- Suppose the headset sees one wall.
- WebXR exposes that wall as one member of frame.detectedPlanes.
- The client asks for its pose with frame.getPose(plane.planeSpace, refSpace).
- At this moment the wall exists only as a current sensor observation in the raw WebXR reference space.

What the client extracts from it:
- position in refSpace
- orientation quaternion in refSpace
- polygon outline from plane.polygon
- semantic label such as wall if available
- a derived extent by bounding the polygon in plane-local coordinates
- a derived normal by rotating the plane's local +Y axis into world space

Step 2: the client turns that raw observation into a temporary capture record
- The client builds a temporary object with fields like pos, quat, nrm, sem, orient, and ext.
- This object is still temporary. It does not yet have a persistent Conjure id like real_wall_7.

Step 3: the client tries to register the whole current capture into the persistent reference constellation
- The client compares the current capture set to the remembered surfaces for the space.
- If registration is confident, it gets Tmat, the transform from current refSpace into the stable space reference frame.
- If registration is not confident, it does not trust the wall yet and may hold the previous frame instead of committing unstable geometry.

Step 4: the wall is expressed in the stable reference frame
- The client multiplies the wall's pose by Tmat.
- Now the wall has a stable position and orientation in Conjure's room-aligned reference frame rather than just the transient current headset tracking frame.

Step 5: the wall either reuses an existing stable id or gets a new one
- The client compares the transformed wall against remembered reference surfaces of the same semantic.
- If it matches an existing wall closely enough, it re-inherits that wall's stable id, for example real_wall_3.
- If it does not match anything, the client mints a new id, for example real_wall_12.

This is the point where the system changes from "sensor observation" to "persistent room surface".

Step 6: room-shape cleanup runs before upload
- The client may square wall facings onto the dominant orthogonal grid.
- It may join short wall-corner gaps.
- It may snap doors, windows, and wall art onto parent walls and add hole metadata.

Step 7: the client POSTs the resulting surface set to /room
- The upload now contains Conjure surface ids, stable-space positions, rotations, extents, semantics, and optional holes.
- The server receives these as RoomSurface entries.

Step 8: the server turns the wall into a real entity in the live world doc
- The server creates or updates an entity like this in the live composed document:
  - id = real_wall_3
  - meta.real = true
  - meta.semantic = wall
  - transform.position and transform.rotation from the stable reference-frame pose
  - components.surface.extent from the client result
  - components.material seeded from the default surface material rules unless already preserved

Step 9: the renderer shows that entity as a room surface
- The client snapshot/patch renderer maps the entity to an A-Frame plane or holed-wall geometry.
- From that point on, the wall is part of the persistent space model, not just a single frame's raw sensor reading.

The important conceptual jump is this:
- raw detected plane = one current observation in the headset's current tracking frame
- real surface entity = a stabilized, named room feature in Conjure's persistent space model

### 3.19 Worked example: hanging an image on a real wall surface

This second example shows why outward normals and room-facing reorientation matter.

Step 1: assume a stable wall surface already exists
- Suppose the system has a stable surface with id real_wall_3.
- That wall stores a rotation whose forward direction represents the wall's outward normal.

Step 2: a user places an image on that surface
- The placement path resolves the requested target to real_wall_3.
- The server does not simply copy the wall's raw rotation onto the image.

Step 3: the server computes a room-facing orientation
- It calls _face_room(surface_rotation).
- That function takes the wall's outward normal, negates it, and uses that as the image's forward direction so the image faces into the room.
- It then chooses an upright direction relative to gravity when possible.

Step 4: the server offsets the image slightly off the wall
- The image is placed a small distance in front of the wall toward the room interior.
- This avoids z-fighting and makes the image visually sit on the wall rather than inside it.

Step 5: the image records which surface it belongs to
- The entity stores meta.on_surface = real_wall_3.
- Later, if the wall moves slightly during a re-registration or capture refresh, the server can re-anchor the image from the wall's updated pose instead of leaving it behind.

This is why the document keeps emphasizing three different ideas:
- the wall surface stores an outward-facing room normal
- placed content usually needs to face inward toward the room, not outward
- stable surface ids are what let attached content survive room re-registration

### 3.20 Detailed registration process and debug HUD decoding

This section describes the actual registration algorithm path used by the client when mapping the current capture into the stable reference frame.

Registration goal:
- solve one rigid transform from current refSpace to stable reference frame
- constrained to yaw plus x/z translation
- return either a confident transform (lock) or null (hold and retry)

Why only yaw plus x/z:
- upstream trust gate requires level horizontal planes before registration is accepted
- that makes gravity a trusted up-axis, so pitch and roll are not solved in registration

Detailed algorithm path:

Step 1: Build current capture set
- The client reads detected planes in the current refSpace and converts each to compact records (semantic, orientation class, extent, position, normal-yaw).

Step 2: Precondition checks
- If the stable reference set has fewer than 3 surfaces, registration returns null with stat ref<3.
- If there are too few vertical-pair yaw deltas, registration returns null with stat dlt=n.

Step 3: Generate yaw hypotheses from semantic/size-compatible vertical pairs
- For each compatible vertical current/reference pair, compute yaw delta between their normals.
- Bin deltas in a histogram (about 6 degree bins).
- Take top histogram peaks as candidate yaw values.

Size-compatibility detail used in this step:
- Compatibility is asymmetric, with tolerance about 0.5 m per extent axis.
- A current surface is accepted when:
- current.ext[0] <= reference.ext[0] + 0.5
- current.ext[1] <= reference.ext[1] + 0.5
- This allows partial/smaller current views to match a larger stored reference while rejecting notably larger current surfaces as unlikely correspondences.

Vertical-orientation matching detail used in this step:
- Pairing for yaw votes only considers vertical-class surfaces on both sides.
- Beyond class, vertical faces are required to be same-facing under the candidate yaw during scoring/translation voting.
- The same-facing gate rejects a vertical pair when cos((current.nyaw + theta) - reference.nyaw) < 0.5 (roughly more than 60 degrees apart).
- This prevents opposite faces of shared walls from being treated as the same surface while still tolerating moderate normal noise.

Step 4: For each candidate yaw, solve translation by dense grid vote
- Rotate each current surface by candidate yaw.
- For compatible pairs, compute translation needed to align centers (tx, tz).
- Vote translation cells on a coarse grid (0.25 m).
- Pick densest translation cell as that yaw candidate's translation.

Resolution and refinement note:
- The vote search is coarse (about 6 degree yaw bins and 0.25 m translation cells), but the chosen parameters are sub-bin inside the winning bucket/cell:
- yaw is the circular mean of deltas in the selected yaw bucket (not snapped to the bucket center)
- translation is the average tx,tz of votes in the winning translation cell (not snapped to the cell corner)
- There is no separate post-vote continuous optimizer (for example ICP or least-squares refinement pass) after this step.

Step 5: Score candidate transform by distinct reference coverage
- Compose candidate transform from solved yaw plus translation.
- Transform each current surface center and find closest same-semantic, same-facing reference within threshold.
- Count distinct claimed reference surfaces as coverage score cov.
- Track inlier count inl as raw matched current surfaces.

Step 6: Accept or reject
- Build status metrics: cov, inl, dlt.
- Accept only if coverage passes absolute and fractional thresholds.
- On accept, append solved yaw and translation to stat and return Tmat.
- On reject, return null and keep previous frame.

Step 7: Caller behavior after register
- If register returns transform, client marks lock and updates Tmat.
- If register returns null and client is not in first-establish owner path, client enters hold path and retries quickly.
- Prolonged mismatch transitions to relocalization fallback.

Important robustness rules used by the matcher:
- semantic compatibility gate: only compare same semantic classes
- asymmetric size compatibility: allows partial current views against larger references
- same-facing gate on vertical surfaces: avoids pairing opposite wall faces across shared partitions
- acceptance based on distinct reference coverage: resists extra clutter planes and fragmentation

How this ties to frame behavior:
- Lock means current capture explained enough of the known room under one consistent transform.
- Hold means current capture did not produce a confident transform this pass.
- Repeated hold with weak overlap usually indicates limited viewpoint overlap or transient tracking instability.

Debug-registration HUD output, field by field:

Line shape:
- ROLE ref=R cur=C  STAT  LOCK_OR_HOLD  DELTA

Fields:
- ROLE: OWNER or GUEST.
- ref=R: number of surfaces currently in the reference constellation.
- cur=C: number of current detected surfaces in this capture pass.
- STAT: short registration status string from the current pass.
- LOCK_OR_HOLD: LOCK when this pass returned a confident transform, hold otherwise.
- DELTA: change from previous capture in solved frame, shown as Δpos in meters and Δyaw in degrees.

Common STAT values and meanings:
- ref<3: too few reference surfaces to solve registration.
- dlt=n: too few vertical-pair yaw deltas for robust yaw voting.
- settling ny=x.xx: trust gate says horizontal normal is not level enough yet.
- cov=a/b inl=c/d dlt=e: candidate statistics before accept/reject.
- cov=... inl=... dlt=... yaw=Y° t=(tx,tz): accepted solve with final yaw and translation.
- walls=n (void path): insufficient walls for canonical frame in void world mode.
- walls=n grid=G° theta=T° (void path): canonical frame solve stats in void world mode.

How to read DELTA quickly:
- Δpos approximately 0 and Δyaw approximately 0 across captures means a stable frame.
- Monotonic drift in either suggests walking or unstable registration.
- Large jump once followed by stability often indicates a relocalization correction.

## 4. Data model as implemented

### 4.1 Space storage

Spaces are stored in .cache/spaces/<user>/<space>.json (SpaceStore).

A space JSON includes at least:
- owner
- name
- public
- geolocation (optional)
- surfaces (real geometry + default material)
- boundary
- optional last_scope and last_world for return selection

### 4.2 World storage and composition

Worlds are stored in .cache/worlds/<scope>/<name>.json (WorldRepository), where scope is <user>/agents/<agent>.

A world references a space by:
- <space_owner>/<space_name> (current preferred form)
- <void> sentinel for no physical room
- legacy bare <space_name> still resolves (back-compat)

On activation, server composes:
- world placed entities and prefs
- plus real surfaces and boundary from the referenced space
- plus per-world surfaceStyles material overrides

On save, server decompose/splits back:
- geometry and boundary to space file (space owner scope)
- placed entities and room surface style overrides to world file

Important: active world owner and active space owner can differ.

## 5. Coordinate systems and frames

## 5.1 Base coordinate conventions

The runtime follows the Three.js/A-Frame convention used in code:
- right-handed
- +X right
- +Y up
- camera forward is -Z

Evidence in code:
- presence and forward vectors use head -Z for look direction
- guest spawn offsets to owner right by rotating [1,0,0]

## 5.2 Frames used at runtime

1) WebXR reference space frame (refSpace)
- Raw detected plane poses arrive here.

2) Registered space/reference frame
- Defined by transform Tmat (refSpace -> reference frame).
- This is the authoritative room alignment frame once lock is available.

3) World-root render frame
- world-root is set to inverse(Tmat), so all content authored in reference frame stays fixed in physical room.

4) Bootstrap anchor frame
- A WebXR anchor can bootstrap before a geometry lock exists.
- After registration lock, geometry-derived Tmat is authoritative.

## 5.3 Surface local frames and normal definitions

WebXR plane conventions used by capture code:
- Plane lies in local X-Z plane.
- Plane normal is local +Y.

A-Frame render surface conventions:
- a-plane lies in local X-Y plane.
- a-plane normal is local +Z.

Conversion:
- capture code applies an extra -90 deg X rotation and emits Euler in YXZ order.
- YXZ is required because A-Frame stores/applies these rotations in YXZ semantics in this pipeline.

Normal meaning in system semantics:
- Surface normals are treated as outward-from-room for captured surfaces.
- Interior-facing direction is computed as -normal.

Implication for image placement:
- on_surface image placement uses _face_room(surface_rotation), which orients image toward room interior and keeps it upright relative to gravity where possible.
- this prevents backward-facing and upside-down placements caused by relying on raw surface roll.

## 6. Lifecycle: establishing, refining, settling, and freezing

## 6.1 Capture cadence and gating

Client room-capture component:
- runs in immersive session with detected planes
- throttles posts to about every 2s
- skips updates when refused by admission gate
- applies trust gate on horizontal surfaces: if floor/ceiling normal is not sufficiently level, capture is held (no post), registration marked unsettled, and relocalization fallback may engage

## 6.2 Establishing window and static freeze

Server ingest_room defines static semantics:
- wall, wall art, door, window, floor, ceiling

Behavior:
- first ~20s from first accepted room capture is establishing window
- during establishing, if any static surface changed, server re-commits whole posted static set atomically
- after establishing window closes, existing static surfaces are frozen (jitter ignored)
- genuinely new static IDs may still be added later

Reason:
- keeps corner-joined shell coherent and avoids visual popping from re-derived static geometry noise

## 4.3 Dynamic updates and pruning

Non-static surfaces are dynamic:
- updated only when changed above thresholds
- thresholds include position, rotation, and extent tolerances

Absence handling:
- if replace=true and a surface disappears, server does not remove immediately
- requires consecutive absence count before prune
- anchored protection: surfaces referenced by on-surface content are protected from pruning to avoid orphaning attached images

## 4.4 What "locked/settled" means in current code

There are two independent notions of stability:

1) Frame lock (client-side registration confidence)
- if registration fails, client holds previous frame and can show passthrough relocalization hint.

2) Geometry settle/freeze (server-side static policy)
- after establishing window, static shell is effectively settled and no longer continuously rewritten from minor capture noise.

There is no separate mesh-refinement state machine in current code. Refinement is plane-based with selective updates and static freeze.

## 7. Space establishment and selection flow

Current flow is two-stage.

## 5.1 Stage 1: geolocation candidate discovery

POST /geolocation:
- returns geo-near candidate spaces across all users
- each candidate includes reduced surface constellation needed for registration vote
- idempotent per client-id once selected in current claim epoch

## 5.2 Stage 2: client-side geometric vote and commit

Client runs RoomSnap.selectSpace over candidates:
- tries registration against each candidate
- selects best candidate with confident lock
- commits verdict via POST /space/select with matched or no-match and client geo

Server /space/select behavior:
- if space is occupied (claimed): admission gate
  - matched active space -> admitted
  - no match or wrong match -> refused
- if unclaimed:
  - matched candidate -> join that space's last world if available, else establish world in that space if allowed
  - no match -> mint new space (space-N) for connecting user and establish world

## 5.3 Occupancy and claim lifecycle

- AR client sends ws message hold after successful select/admission.
- AR client sends release on exit-vr.
- websocket disconnect also releases hold.
- when last holder leaves, space becomes unclaimed and per-client selection commit guard resets.

This is intentionally per-AR-holder occupancy, not tied to voice/CLI/desktop participants.

## 8. Re-orientation and relocalization behaviors

## 6.1 Leaving and returning to guardian boundary

Observed Quest behavior in code comments: tracking frame can jump by a large yaw and translation after relocalization.

Current mitigation:
- per-capture geometric registration recomputes/validates frame alignment against persistent surface constellation
- if lock is lost for a period, client enters relocalization mode:
  - hides virtual world and sky
  - shows passthrough + headset hint
  - restores world automatically when lock returns
- refSpace reset event forces immediate recapture attempt

## 6.2 Returning visitor orientation into known space

Return path:
- geolocation returns nearby candidates with stored surface constellations
- client vote picks correct candidate space by geometry, not just distance
- server switches to candidate's last scope/world when available
- client registers capture into space reference frame and aligns world-root via inverse(Tmat)

## 6.3 Guest (non-owner) orientation

Guest behavior in room-capture component:
- guest is register-only for geometry
- guest reseeds reference from authoritative broadcast each capture
- guest solves Tmat and pins world-root
- guest never mints IDs, never posts /room geometry

This prevents guest-side drift feedback into canonical geometry.

## 9. Plane registration algorithm and guest-difference accommodation

RoomSnap.register (current algorithm):
- solves yaw + x/z translation only
- candidate yaw from histogram of normal yaw deltas over semantic/size-compatible vertical pairs
- tests top peaks
- translation by densest grid cell vote of ref.pos - R*cur.pos
- score by distinct reference surfaces covered under distance and facing constraints
- acceptance uses minimum covered references and coverage fraction of reference set

Robustness for guest partial/extra perception:
- asymmetric size compatibility allows partial smaller observations, rejects larger-than-reference mismatch
- coverage uses distinct reference coverage, not raw detected count (reduces clutter inflation and fragmentation double-count)
- same-facing gate for vertical surfaces reduces wrong-face matches in partition-wall situations

Known weak point documented in code comments and tests:
- symmetric environments can admit 180-degree ambiguity from some vantage points

## 10. Surfaces, doors/windows/wall-art, corners, and holes

## 8.1 Wall squaring

squareWalls:
- estimates dominant orthogonal grid from wall normals (weighted)
- snaps wall, door, window, wall art facings to nearest 90-degree grid direction
- only small nudges are applied; larger deviations are preserved as potentially intentional geometry

## 8.2 Corner joining

joinCorners:
- considers pairs of near-perpendicular walls at similar height bands
- computes intersection in plan view and snaps nearest eligible ends if both within gap threshold
- updates wall centers and extents to close short gaps
- avoids forcing joins for non-corner cases (e.g. collinear doorway gaps, distant intersections)

## 8.3 Inset snapping (doors/windows/wall art)

snapInsets:
- identifies nearest parallel wall for each inset semantic
- nudges inset to interior side with small offset while keeping depth relation meaningful
- forces inset orientation to host wall orientation for consistency
- records snap debug metadata

Door/window special handling:
- each door/window adds hole metadata to host wall (x,y,w,h in wall-local frame)
- wall art does not cut holes

## 8.4 Hole rendering path

Client render uses custom holed-wall geometry:
- converts hole list into ShapeGeometry with holes
- clamps hole bounds slightly inward to avoid triangulation failure at border-touching cases (e.g. door to floor edge)
- maps UVs so texture behavior is consistent with plain plane expectations

## 8.5 Adjacent rooms, parallel walls, and inward-facing doors

Current code and tests explicitly handle adjacency patterns:
- parallel opposite-facing wall surfaces representing opposite sides of a partition are treated as distinct via facing checks
- same-facing and near-coincident anti-parallel fallback logic avoids both ID swaps and false dedupe in common partition configurations
- door snapping uses interior direction inferred from normal sign convention, so each door is pushed into its own room interior at junctions

## 11. How surfaces appear/disappear and robustness strategies

What is known from code behavior:
- headset plane sets are noisy and can fragment (multiple planes per physical feature)
- sparse captures occur after relocalization or re-entry events
- planes can disappear transiently

Current robustness mechanisms:
- no immediate prune on first absence
- static shell freeze after establishing window
- change thresholds to suppress jitter updates
- registration confidence gate before committing frame
- relocalization fallback mode when lock confidence drops
- anchored-surface protection for attached content

## 12. Special cases, hacks, and brittle points

These are implementation realities worth calling out.

1) Threshold-heavy behavior
- many constants drive success/failure and visual stability (distance, angle, grid cell, coverage, absence counts, establish duration).
- environment-dependent tuning risk remains.

2) Reference-seeding dependence
- correctness depends on receiving and using authoritative real surfaces promptly; stale or missing snapshots can delay good lock.

3) Security boundary mismatch risk around /room ownership
- /room is owner-gated by active world owner middleware, not active space owner.
- when world owner differs from space owner, geometry writes are still permitted via world ownership path, then persisted into space owner's space reference path.
- this is intentional in current code comments for world ownership flow, but it is a nuanced trust boundary that can surprise.

4) Occupancy signaling trust
- claim/occupy uses client ws hold/release semantics; malicious or buggy clients could hold longer than intended until disconnect/release logic resolves.

5) Legacy/back-compat branching
- bare space refs are still accepted for compatibility, increasing branching complexity.

6) No dense mesh ingestion pipeline in active path
- despite prior design intent, current runtime is plane/surface based; any docs assuming active mesh segment progression are ahead of implementation.

## 13. Documentation mismatches found (code vs existing docs)

This section lists concrete mismatches discovered while writing this file.

1) docs/spaces-and-users-plan.md
- Claim: ingest_room checks connection user equals space owner.
- Code: /room is in owner-only middleware keyed to active world owner.

2) docs/room-model.md
- Contains forward-looking schema fields and flows not implemented in active runtime path (for example explicit room origin anchor object in environment and mesh refinement state fields as part of live room update loop).
- Current code uses plane capture, registration Tmat, and static/dynamic update policy; no active dense mesh refinement loop is wired through /room ingest in the current runtime path.

3) docs/room-model.md registration acceptance narrative
- A narrative section describes acceptance in terms of inlier fraction of detected planes.
- Current RoomSnap.register acceptance is based on distinct reference coverage with cov >= minimum and cov >= fraction_of_reference (not fraction of detected planes).

4) docs/room-model.md transport description
- It describes an upstream WS room message option in the model narrative.
- Current client path posts geometry via HTTP POST /room; ws channel is used for snapshot/patch/presence/hold-release signaling.

5) Terminology drift across docs
- Some docs still center the term room where current persistence and selection architecture centers space as shared physical layer with worlds composed over it.

## 14. Practical stage summary

End-to-end in current code:

1) AR enter -> geolocation report -> candidate list.
2) Client capture begins and geometric vote runs.
3) Client commits select verdict.
4) If admitted/established, client holds occupancy.
5) Owner capture posts /room every cycle (throttled), with registration-based frame alignment.
6) Server ingests with static/dynamic policy, updates boundary/authority, and broadcasts patches.
7) Guests register-only align to authoritative surfaces each capture and never author geometry.
8) On relocalization disturbance, lock can drop; passthrough fallback engages until lock recovers.
9) On AR exit/disconnect, hold releases; last holder leaving unclaims space for next establishment.

This is the current implemented space model.

## 15. Code traceability index

This index maps each major section in this document to implementation lines.

### 13.1 Space/world persistence and composition

- World and space repositories: [conjure/world.py](conjure/world.py#L156), [conjure/world.py](conjure/world.py#L264)
- Active space owner decoupled from active world owner: [conjure/server.py](conjure/server.py#L184)
- Compose/decompose world with space geometry: [conjure/server.py](conjure/server.py#L1461), [conjure/server.py](conjure/server.py#L1487)
- Space reference parsing/formatting: [conjure/server.py](conjure/server.py#L1529), [conjure/server.py](conjure/server.py#L1543)
- Activation flow and <void> handling: [conjure/server.py](conjure/server.py#L1562)

### 13.2 Coordinates, transforms, and frame pinning

- Frame pinning and world-root update: [client/conjure-client.js](client/conjure-client.js#L670), [client/conjure-client.js](client/conjure-client.js#L813)
- Registration transform ownership (_Tmat): [client/conjure-client.js](client/conjure-client.js#L648), [client/conjure-client.js](client/conjure-client.js#L973)
- Euler conversion and YXZ order: [client/room-snap.js](client/room-snap.js#L19), [client/room-snap.js](client/room-snap.js#L141)
- Surface constellation conversion: [client/room-snap.js](client/room-snap.js#L138)

### 13.3 Registration, candidate voting, and space selection

- Registration algorithm: [client/room-snap.js](client/room-snap.js#L49)
- Coverage thresholds (distinct reference coverage): [client/room-snap.js](client/room-snap.js#L61), [client/room-snap.js](client/room-snap.js#L124)
- Candidate selection vote: [client/room-snap.js](client/room-snap.js#L160)
- Client pending candidate flow: [client/conjure-client.js](client/conjure-client.js#L366), [client/conjure-client.js](client/conjure-client.js#L945)
- Geolocation discovery endpoint: [conjure/server.py](conjure/server.py#L937)
- Candidate set assembly: [conjure/server.py](conjure/server.py#L871)
- Selection/admission endpoint: [conjure/server.py](conjure/server.py#L956)

### 13.4 Occupancy claim lifecycle and gate mechanics

- Occupied/unclaim state: [conjure/server.py](conjure/server.py#L457), [conjure/server.py](conjure/server.py#L464)
- WebSocket hold/release handling: [conjure/server.py](conjure/server.py#L2718), [conjure/server.py](conjure/server.py#L2741), [conjure/server.py](conjure/server.py#L2745)
- Client hold/release sends: [client/conjure-client.js](client/conjure-client.js#L1149), [client/conjure-client.js](client/conjure-client.js#L1150)

### 13.5 Ingest pipeline: static freeze, dynamic updates, pruning

- /room ingest handler: [conjure/server.py](conjure/server.py#L1700)
- Static semantics set: [conjure/server.py](conjure/server.py#L1645)
- Establishing window: [conjure/server.py](conjure/server.py#L1646), [conjure/server.py](conjure/server.py#L1724)
- Dynamic change gate: [conjure/server.py](conjure/server.py#L1675)
- Absence debounce constant: [conjure/server.py](conjure/server.py#L1628)
- On-surface reanchor ops in ingest: [conjure/server.py](conjure/server.py#L2429)

### 13.6 Guest orientation and register-only behavior

- Guest reseed and no-post behavior: [client/conjure-client.js](client/conjure-client.js#L910), [client/conjure-client.js](client/conjure-client.js#L981), [client/conjure-client.js](client/conjure-client.js#L1047)
- Refusal passthrough blanking path: [client/conjure-client.js](client/conjure-client.js#L539), [client/conjure-client.js](client/conjure-client.js#L1123)

### 13.7 Relocalization and return behavior

- Lost-lock marking and relocalize mode: [client/conjure-client.js](client/conjure-client.js#L717), [client/conjure-client.js](client/conjure-client.js#L721)
- Trust gate and hold-while-unsettled behavior: [client/conjure-client.js](client/conjure-client.js#L934), [client/conjure-client.js](client/conjure-client.js#L975)
- Reference-space reset trigger: [client/conjure-client.js](client/conjure-client.js#L813)

### 13.8 Surface geometry processing: squaring, corners, insets, holes

- Wall squaring: [client/room-snap.js](client/room-snap.js#L240)
- Corner joining: [client/room-snap.js](client/room-snap.js#L267)
- Inset snapping and hole recording: [client/room-snap.js](client/room-snap.js#L318), [client/room-snap.js](client/room-snap.js#L351)
- Holed wall render geometry: [client/conjure-client.js](client/conjure-client.js#L80), [client/conjure-client.js](client/conjure-client.js#L266)

### 13.9 Surface normals and image orientation implications

- Surface-facing and room-facing content orientation: [conjure/server.py](conjure/server.py#L2375)
- On-surface image placement uses room-facing orientation: [conjure/server.py](conjure/server.py#L2399), [conjure/server.py](conjure/server.py#L2575)
- Reanchor on-surface images as surfaces move: [conjure/server.py](conjure/server.py#L2412), [conjure/server.py](conjure/server.py#L2429)

### 13.10 Verified behavior in tests

- Insets and own-room door direction: [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L54), [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L67)
- Hole creation correctness: [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L80)
- Squaring/corner joins: [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L141), [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L166)
- Registration robustness (partial/extra/symmetric limits): [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L244), [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L255), [tests/js/room-snap.test.js](tests/js/room-snap.test.js#L319)

### 13.11 Mismatch evidence lines

- /room ownership gate is active-world-owner middleware, not space-owner check: [conjure/server.py](conjure/server.py#L391), [conjure/server.py](conjure/server.py#L392), [conjure/server.py](conjure/server.py#L399), [conjure/server.py](conjure/server.py#L401)
- /room ingest route itself: [conjure/server.py](conjure/server.py#L1699)
- Client transport for geometry is POST /room: [client/conjure-client.js](client/conjure-client.js#L1047)



how register solves the guest's rotation. Here's the full picture, grounded in the actual algorithm.

What's being solved

When a guest joins, its live capture is the same room as the authority's reference, but rotated and shifted by an unknown amount. Because the trust gate guarantees a level floor, gravity pins pitch and roll — so the only rotational unknown is yaw (heading: rotation about the vertical axis). register finds that yaw first, then the horizontal shift.

How yaw is found — a voting histogram

It can't pair walls up front (it doesn't yet know which guest wall is which reference wall), so it votes:

1. Take every pairing of (a guest wall, a reference wall) that's plausibly the same surface — same semantic, similar size, both vertical.
2. For each such pair, compute the angle that would rotate the guest wall's facing onto the reference wall's facing: delta = wrap(ref.nyaw − cur.nyaw).
3. Here's the trick: if the guest really is the same room rotated by some true angle θ, then every correct pair produces delta ≈ θ. Incorrect pairs (guest wall i matched against an unrelated reference wall j) produce scattered, random deltas.
4. Drop all those deltas into a histogram with 6° bins (Math.PI/30). The correct θ piles up into one tall bin; the wrong pairings smear thinly across all the others.

So what is a "peak"?

A peak is one of the tallest histogram bins — a candidate yaw angle that many wall-pairs independently agreed on. The bins are sorted by how many deltas landed in them, and the top N (--reg-yaw-peaks) are taken as candidate rotations. Each peak's actual angle is the circular mean of the deltas in its bin (atan2(Σsin, Σcos)), so it's a refined value, not just the bin's center. (It's essentially a 1-D Hough transform over rotation angle.)

Why try more than the single tallest peak?

Because the true θ isn't always the most-populated bin. Clutter/furniture, a guest viewing the room from a very different spot, a nearly-symmetric room (a rectangle has a plausible 180° "mirror" rotation), or capture noise can make a wrong angle tie with or slightly outrank the correct one. So register doesn't commit to bin #1 — it tries the top N peaks, and for each candidate yaw it runs the rest of the solve (grid-vote the translation, then count how many reference surfaces that full transform actually covers) and keeps whichever candidate explains the room best. Even if the true rotation was the 3rd-tallest bin, trying 5 peaks still finds it.