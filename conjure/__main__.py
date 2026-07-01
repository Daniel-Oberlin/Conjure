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
    args = ap.parse_args()
    if args.debug_registration:                      # picked up by get_settings() when the app imports below
        os.environ["CONJURE_DEBUG_REGISTRATION"] = "1"
    uvicorn.run("conjure.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
