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
    ap.add_argument("--force-geo", metavar="ZERO|/user/spaces/name", default=None,
                    help="TEST: override the reported geolocation — 'zero' pins you at (0,0), or address an "
                         "existing space by path (e.g. /daniel/spaces/space-0) to pin you at its location")
    ap.add_argument("--force-occupied", action="store_true",
                    help="TEST: treat the active space as already CLAIMED (a phantom AR holder), so the "
                         "admission gate engages for one headset — match the active space ⇒ admitted, else refused")

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

    args = ap.parse_args()
    if args.debug_registration:                      # picked up by get_settings() when the app imports below
        os.environ["CONJURE_DEBUG_REGISTRATION"] = "1"
    if args.force_geo:
        os.environ["CONJURE_FORCE_GEO"] = args.force_geo
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
    uvicorn.run("conjure.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
