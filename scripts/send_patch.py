#!/usr/bin/env python3
"""Send a hand-authored patch JSON file to a running Conjure server.

Usage:  python scripts/send_patch.py examples/patches/add_cube.json
        CONJURE_URL=http://localhost:8080 python scripts/send_patch.py <file>
"""

import os
import sys
import urllib.request

URL = os.environ.get("CONJURE_URL", "http://localhost:8080") + "/patch"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    body = open(sys.argv[1], "rb").read()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        print(resp.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
