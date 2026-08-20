"""Run the Conjure world server: `python -m conjure` (or the `conjure` console script)."""

from __future__ import annotations

import os


def main() -> None:
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser(prog="conjure", description="Run the Conjure world server.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("CONJURE_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CONJURE_PORT", "8080")))
    ap.add_argument("--debug-registration", action="store_true",
                    help="show the co-location registration HUD + per-capture log in the headset (off by default)")
    ap.add_argument("--debug-jitter", action="store_true",
                    help="enable ONLY the frame-pacing/jitter probes without the heavy registration "
                         "diagnostics — for a clean per-frame cost measurement (off by default). Emits: RATE "
                         "(actual display Hz + budget, once), PACE (rolling ~2 s window: mean/jitter-sd/p95/max "
                         "dt + late/drop counts + peak slew + mesh-rebuild count + max camera JERK mm + JS heap "
                         "MB), LATE (per-late-frame forensics: dt(prev-cap):wall-move/obj-move mm/heap-delta "
                         "KB/tick-self ms — flat move ⇒ compositor reprojection; small tick-self ⇒ stall "
                         "outside our JS), JERK (per-frame camera 2nd-difference events: jerk-mm(on|late/dt) — "
                         "'on'-time jerks ⇒ a view/tracking stutter, not a dropped frame), COST (per-capture "
                         "sub-phase "
                         "breakdown), and SPIKE (one-shot dump on a frame past 1.5x the refresh interval; "
                         "CONJURE_JITTER_DT_MS overrides)")
    ap.add_argument("--force-geo", metavar="ZERO|/user/spaces/name", default=None,
                    help="TEST: override the reported geolocation — 'zero' pins you at (0,0), or address an "
                         "existing space by path (e.g. /daniel/spaces/space-0) to pin you at its location")
    ap.add_argument("--force-occupied", action="store_true",
                    help="TEST: treat the active space as already CLAIMED (a phantom AR holder), so the "
                         "admission gate engages for one headset — match the active space ⇒ admitted, else refused")
    ap.add_argument("--drop-surface", metavar="SEMANTIC|ID[,…]", default=None,
                    help="TEST: the client pretends it didn't capture surfaces matching any of these "
                         "semantics/id-substrings (comma-separated, e.g. 'door,window,wall art') — kept in "
                         "the seed but omitted from the local render — so missing-surface RECOVERY (§5.2) "
                         "can be exercised with one headset")

    # --- Co-location robustness: how tolerantly a GUEST headset locks onto the shared space ------------
    reg = ap.add_argument_group(
        "co-location robustness (two-headset guest tuning)",
        "Tune how a GUEST headset joins a space another headset already established. Terminology:\n"
        "  authority     the FIRST headset in a space; the ONLY one that authors the room's geometry.\n"
        "  guest         a second, co-located headset. It does NOT author geometry — instead it\n"
        "                REGISTERS its own live Quest capture against the authority's, so both render\n"
        "                content in the same real-world spot. These knobs govern that match.\n"
        "  reference     the authority's set of surfaces (the 'reference constellation') the guest\n"
        "                matches against.\n"
        "  registration  solving the single rotation + horizontal shift that maps the guest's detected\n"
        "                planes onto the reference. It gates BOTH admission (a guest that can't register\n"
        "                is refused entry) AND staying locked on every recapture.\n"
        "  coverage      how many DISTINCT reference surfaces the guest explains under one transform —\n"
        "                the accept score (extra/duplicate planes can't inflate it).\n"
        "  inlier        a detected plane that lands close enough to a same-kind reference surface to\n"
        "                count toward coverage.\n"
        "LOOSEN these (lower coverage mins, larger tolerances) when the two headsets capture the SAME\n"
        "room differently (different plane sizes/positions, missing surfaces) so the guest still gets in\n"
        "and holds its lock; TIGHTEN them to demand a stronger, less ambiguous match.")
    reg.add_argument("--reg-min-cov", type=int, default=4, metavar="N",
                     help="min reference surfaces the guest must COVER to accept a lock (default: 4). "
                          "Lower ⇒ locks on less overlap (a guest viewing from a different spot); higher ⇒ stricter.")
    reg.add_argument("--reg-min-cov-frac", type=float, default=0.3, metavar="FRAC",
                     help="min FRACTION (0..1) of the authority's surfaces the guest must cover (default: 0.3). "
                          "Lower ⇒ tolerate a partial-vantage guest; higher ⇒ require most of the room to match.")
    reg.add_argument("--reg-size-tol", type=float, default=0.5, metavar="METERS",
                     help="how much LARGER (m) a guest's plane may be than a reference surface and still match "
                          "(default: 0.5). Raise when the two headsets split walls into different-sized planes.")
    reg.add_argument("--reg-inlier-m", type=float, default=0.4, metavar="METERS",
                     help="max distance (m), after aligning, a guest plane may sit from a same-kind reference and "
                          "still count as an inlier/coverage (default: 0.4). Raise when the captures disagree on "
                          "where a wall is.")
    reg.add_argument("--reg-yaw-peaks", type=int, default=5, metavar="N",
                     help="how many candidate room rotations to try when solving the guest's orientation "
                          "(default: 5). More ⇒ more robust to an ambiguous/noisy heading, slightly slower.")
    reg.add_argument("--capture-interval", type=float, default=2.0, metavar="SECONDS",
                     help="how often a headset recaptures + re-registers the room, in seconds (default: 2.0). "
                          "Lower ⇒ the guest re-locks faster but churns more; higher ⇒ calmer, slower to recover.")

    # --- Render apply-gate: how much a locally-rendered surface must change before it's redrawn -----------
    gate = ap.add_argument_group(
        "render apply-gate (local-first surface rendering)",
        "Each headset renders its OWN captured surfaces and re-lays a surface only when it moves past one of\n"
        "these tolerances, so sub-tolerance re-derivation doesn't rebuild the mesh (the 'pop'). Bigger =\n"
        "calmer (fewer redraws, more lag before a real change shows); smaller = snappier (tracks tighter,\n"
        "redraws more). Applies to every client's local render (docs/local-first-geometry.md §4-6).")
    gate.add_argument("--apply-tol-pos", type=float, default=0.02, metavar="METERS",
                      help="how far (m) a surface must move before it's redrawn (default: 0.02 = 2 cm).")
    gate.add_argument("--apply-tol-rot", type=float, default=1.0, metavar="DEGREES",
                      help="how far (°) a surface must rotate before it's redrawn (default: 1.0).")
    gate.add_argument("--apply-tol-ext", type=float, default=0.02, metavar="METERS",
                      help="how much (m) a surface's size or an opening must change before it's redrawn "
                           "(default: 0.02 = 2 cm).")
    gate.add_argument("--inset-standoff", type=float, default=0.02, metavar="METERS",
                      help="how far (m) a door/window/wall-art SURFACE sits in front of its wall (default: "
                           "0.02 = 2 cm). Bigger = the surface reads as visibly separated from the wall.")
    gate.add_argument("--geo-slice-ms", type=float, default=3.0, metavar="MS",
                      help="per-frame budget (ms) for the time-sliced surface mesh-rebuild pump so a "
                           "whole-room re-triangulation spreads across frames instead of dropping one "
                           "(default: 3). 0 or less disables slicing (rebuild all inline each frame).")
    gate.add_argument("--on-surface-standoff", type=float, default=0.02, metavar="METERS",
                      help="how far (m) an on-surface IMAGE sits in front of its host surface (default: 0.02 "
                           "= 2 cm). Applies to newly placed / re-anchored images.")
    gate.add_argument("--foveation", type=float, default=0.0, metavar="LEVEL",
                      help="fixed-foveated-rendering level 0..1, applied at runtime over index.html's "
                           "foveationLevel. Higher = periphery rendered at lower resolution = less GPU (fewer "
                           "dropped frames / the walking micro-stutter) at the cost of peripheral sharpness + "
                           "moire on fine edges; 0 (default) = full-res everywhere. Try ~0.3 if frames drop; "
                           "A/B against 0 with --debug-jitter (watch the PACE 'drop' count).")
    gate.add_argument("--occlusion", choices=["off", "hands", "hands-solid", "full"], default="off",
                      help="real-world depth occlusion (docs/dynamic-content-plan.md): hide virtual content where a "
                           "nearer real surface is, so your hand covers a virtual wall instead of vice-versa. "
                           "off (default) = virtual always over passthrough (today); hands = tracked-hand occluders "
                           "only (sharp, cheap; your real hand shows through); hands-solid = same hand mesh drawn as "
                           "opaque white polygons (a white-glove avatar that also occludes); full = environment depth "
                           "(walls/furniture/people, coarse edges, needs Quest depth-sensing). Per-client override: "
                           "?occlusion=off|hands|hands-solid|full in the URL.")
    gate.add_argument("--pose-tau", type=float, default=0.0, metavar="SECONDS",
                      help="pose-smoothing time constant (s): ease each surface (and its content) toward its "
                           "newly-captured pose over ~3x this instead of snapping, so a drift correction reads "
                           "as a short settle not a ~2 s step (docs/pose-smoothing-plan.md). 0 (default) "
                           "disables = snap as before; A/B like --geo-slice-ms. ~0.1 is a good starting value; "
                           "too large trails the real wall in passthrough (§10).")
    gate.add_argument("--surface-weld", type=float, default=0.002, metavar="METERS",
                      help="inflate each surface's FILL by this much (split per side, default: 0.002 = 2 mm) so "
                           "abutting fills overlap instead of leaving a float-rounding crack the passthrough "
                           "flickers through. The wireframe outline stays true size. 0 disables.")
    gate.add_argument("--wall-seal-tol", type=float, default=0.15, metavar="METERS",
                      help="seal a wall's top to the ceiling and bottom to the floor when the edge is already "
                           "within this distance (m) of the plane (default: 0.15). Closes the open slit at the "
                           "wall/ceiling line the Quest leaves by fitting walls a few cm short (docs §9.1); "
                           "vertical-only, so plane/width and registration are untouched. 0 disables.")
    gate.add_argument("--wall-perp-tol", type=float, default=0.15, metavar="METERS",
                      help="how far apart (m) two captures' wall planes may sit and still be called the SAME "
                           "wall (default: 0.15). §5.3 wall identity by plane — bigger tolerates a guest's "
                           "differently-placed scan; smaller demands a tighter plane match.")
    gate.add_argument("--wall-yaw-tol", type=float, default=30.0, metavar="DEGREES",
                      help="max normal-yaw difference (°) for a wall match (default: 30). Keeps the two "
                           "anti-parallel faces of a partition distinct.")
    gate.add_argument("--wall-overlap-slop", type=float, default=0.3, metavar="METERS",
                      help="max along-wall gap (m) between two captures' spans and still ONE wall (default: "
                           "0.3). Guards against merging two distinct collinear walls (a segment past a door).")
    gate.add_argument("--group-surface-relay", choices=["on", "off"], default="on",
                      help="when ANY real surface crosses the apply-tolerance, re-lay ALL of them together so "
                           "wall-floor/ceiling junctions and door/window cutouts share one render epoch and "
                           "don't drift apart over a session (default: on). 'off' = each surface re-lays "
                           "independently (the A/B baseline — reproduces the slowly-opening junction seams).")

    args = ap.parse_args()
    if args.debug_registration:                      # picked up by get_settings() when the app imports below
        os.environ["CONJURE_DEBUG_REGISTRATION"] = "1"
    if args.debug_jitter:
        os.environ["CONJURE_DEBUG_JITTER"] = "1"
    if args.force_geo:
        os.environ["CONJURE_FORCE_GEO"] = args.force_geo
    if args.drop_surface:
        os.environ["CONJURE_DROP_SURFACE"] = args.drop_surface
    if args.force_occupied:
        os.environ["CONJURE_FORCE_OCCUPIED"] = "1"
    os.environ["CONJURE_REG_MIN_COV"] = str(args.reg_min_cov)
    os.environ["CONJURE_REG_MIN_COV_FRAC"] = str(args.reg_min_cov_frac)
    os.environ["CONJURE_REG_SIZE_TOL"] = str(args.reg_size_tol)
    os.environ["CONJURE_REG_INLIER_M"] = str(args.reg_inlier_m)
    os.environ["CONJURE_REG_YAW_PEAKS"] = str(args.reg_yaw_peaks)
    os.environ["CONJURE_CAPTURE_INTERVAL"] = str(args.capture_interval)
    os.environ["CONJURE_APPLY_TOL_POS"] = str(args.apply_tol_pos)
    os.environ["CONJURE_APPLY_TOL_ROT_DEG"] = str(args.apply_tol_rot)
    os.environ["CONJURE_APPLY_TOL_EXT"] = str(args.apply_tol_ext)
    os.environ["CONJURE_INSET_STANDOFF"] = str(args.inset_standoff)
    os.environ["CONJURE_GEO_SLICE_MS"] = str(args.geo_slice_ms)
    os.environ["CONJURE_FOVEATION"] = str(args.foveation)
    os.environ["CONJURE_OCCLUSION"] = str(args.occlusion)
    os.environ["CONJURE_POSE_TAU"] = str(args.pose_tau)
    os.environ["CONJURE_ON_SURFACE_STANDOFF"] = str(args.on_surface_standoff)
    os.environ["CONJURE_SURFACE_WELD"] = str(args.surface_weld)
    os.environ["CONJURE_WALL_SEAL_TOL"] = str(args.wall_seal_tol)
    os.environ["CONJURE_WALL_PERP_TOL"] = str(args.wall_perp_tol)
    os.environ["CONJURE_WALL_YAW_TOL_DEG"] = str(args.wall_yaw_tol)
    os.environ["CONJURE_WALL_OVERLAP_SLOP"] = str(args.wall_overlap_slop)
    os.environ["CONJURE_GROUP_SURFACE_RELAY"] = "1" if args.group_surface_relay == "on" else "0"
    uvicorn.run("conjure.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
