"""Run the Conjure world server: `python -m conjure` (or the `conjure` console script)."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("CONJURE_HOST", "0.0.0.0")
    port = int(os.environ.get("CONJURE_PORT", "8080"))
    uvicorn.run("conjure.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
