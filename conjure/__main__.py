"""Run the Conjure world server: `python -m conjure` (or the `conjure` console script)."""

from __future__ import annotations

import os


def main() -> None:
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser(prog="conjure", description="Run the Conjure world server.")
    ap.add_argument("--host", default=os.environ.get("CONJURE_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CONJURE_PORT", "8080")))
    ap.add_argument("--debug-registration", action="store_true",
                    help="show the co-location registration HUD + per-capture log in the headset (off by default)")
    ap.add_argument("--establishment-period", type=float, default=20.0,
                    help="seconds a NEW room captures before its static set freezes (default: 20.0)")
    ap.add_argument("--force-geo", metavar="ZERO|/user/spaces/name", default=None,
                    help="TEST: override the reported geolocation — 'zero' pins you at (0,0), or address an "
                         "existing space by path (e.g. /daniel/spaces/space-0) to pin you at its location")
    ap.add_argument("--force-occupied", action="store_true",
                    help="TEST: treat the active space as already CLAIMED (a phantom AR holder), so the "
                         "admission gate engages for one headset — match the active space ⇒ admitted, else refused")
    args = ap.parse_args()
    if args.debug_registration:                      # picked up by get_settings() when the app imports below
        os.environ["CONJURE_DEBUG_REGISTRATION"] = "1"
    if args.force_geo:
        os.environ["CONJURE_FORCE_GEO"] = args.force_geo
    if args.force_occupied:
        os.environ["CONJURE_FORCE_OCCUPIED"] = "1"
    os.environ["CONJURE_ESTABLISHMENT_PERIOD"] = str(args.establishment_period)
    uvicorn.run("conjure.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
