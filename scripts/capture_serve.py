#!/usr/bin/env python
"""Serve a captured PlayCanvas build so it runs locally, without writing to the capture.

    python scripts/capture_serve.py temp/vrh/barbie
    python scripts/capture_serve.py temp/vrh/barbie --port 8088 --no-stub

A capture is very nearly a runnable app — `specs/captures.md` §2, and `plans/run-a-capture-locally.md`
for the measurement. Every script the build's own `index.html` asks for is on disk, including the
engine, the four `__*.js` bootstrap files and the CDN copies of axios, mobx and pocketbase. Three things
stand between that and a page that loads, and this handles all three:

**The shell is on disk under the wrong name.** No capture has an `index.html`, because the page's own
document is not an entry in `config.json` and the grabber is registry-driven. But the site is an SPA and
answers a 404 with the shell body at `200`, so the grabber saved it wherever a texture failed — 143
copies across the corpus, all identical, all the real thing. This finds one and serves it at `/`.

**It phones home.** `new PocketBase("https://api." + detectPortal())`, where `detectPortal()` matches
`location.hostname` against a portal list and FALLS THROUGH to the live site, so a local build calls the
real service unless something stops it. `--stub` (the default) injects a shim ahead of the app that
points `fetch` and `XMLHttpRequest` at this server instead, which then replays the API responses the
capture already holds. Nothing else is patched: the shim is 30 lines and the app is untouched.

**It calls out for analytics.** The Cloudflare beacon and Plausible are stripped from the shell — they
are not part of the app and a blocked request throws inside a `<script type=module>`.

The capture directory is opened read-only and never written to. Everything this generates lives in
memory.
"""
from __future__ import annotations

import argparse
import http.server
import json
import re
import socketserver
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

#: Where a recovered shell has to point once it is served from here rather than from the site.
_BEACON = re.compile(r"<script[^>]*cloudflareinsights[^>]*>\s*</script>", re.I | re.S)
_PLAUSIBLE = re.compile(r"<script[^>]*plausible[^>]*>\s*</script>", re.I | re.S)

#: Injected ahead of everything else. `detectPortal()` reads `location.hostname`, so from localhost the
#: app resolves to the live API and there is no setting that changes it — the origin is built in the
#: bundle. Redirecting the two transports it uses is less invasive than patching the bundle, and it is
#: visible in one place instead of spread through a 4 MB minified file.
_SHIM = """<script>
(function () {
  var LIVE = /^https:\\/\\/api\\.[a-z0-9.-]+\\//i;
  var here = function (url) { return url.replace(LIVE, location.origin + "/__api/"); };
  var f = window.fetch;
  window.fetch = function (input, init) {
    if (typeof input === "string" && LIVE.test(input)) input = here(input);
    else if (input && input.url && LIVE.test(input.url)) input = new Request(here(input.url), input);
    return f.call(this, input, init);
  };
  var open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    if (typeof url === "string" && LIVE.test(url)) url = here(url);
    return open.apply(this, [method, url].concat([].slice.call(arguments, 2)));
  };
  console.log("[capture_serve] API calls redirected to " + location.origin + "/__api/");
})();
</script>
"""


def find_shell(root: Path) -> tuple[bytes, str]:
    """The build's `index.html`, and where it was found. Raises if the capture has no shell."""
    direct = root / "index.html"
    if direct.exists():
        return direct.read_bytes(), str(direct)
    # The strays. Identified by CONTENT, not by name — a texture that came back as HTML is the app
    # shell only if it loads the app.
    for path in sorted(root.rglob("*.htm*")):
        body = path.read_bytes()
        if b"__start__.js" in body and b"playcanvas" in body.lower():
            return body, str(path)
    raise SystemExit(
        f"{root}: no app shell. Every script may be here, but the page that loads them is not — and "
        f"this capture has no stray copy either. It is one of the assets-only captures; see "
        f"docs/plans/run-a-capture-locally.md §2.")


#: An absolute third-party URL in the shell — the CDN copies of axios and friends.
_ABSOLUTE = re.compile(r"https://([a-z0-9.-]+)/([^\"\'\s>]+)", re.I)


def _localise(text: str, root: Path) -> tuple[str, list[str]]:
    """Point every absolute URL at the capture's own copy, where there is one.

    The grabber preserves the URL path — `cdnjs.cloudflare.com/ajax/libs/axios/1.7.3/axios.min.js` is on
    disk at exactly that path — so this is a rewrite and not a fetch. What it is FOR is the word
    standalone: a build that still pulls axios off a CDN runs on someone else's uptime, and the point of
    a capture is that it does not.
    """
    kept: list[str] = []

    def swap(m: re.Match) -> str:
        rel = f"{m.group(1)}/{m.group(2)}"
        if (root / rel).is_file():
            kept.append(rel)
            return "/" + rel
        return m.group(0)

    return _ABSOLUTE.sub(swap, text), kept


def shell_for(root: Path, stub: bool) -> bytes:
    body, where = find_shell(root)
    print(f"  shell     {where}")
    text = body.decode("utf-8", "replace")
    text = _BEACON.sub("<!-- cloudflare beacon stripped by capture_serve -->", text)
    text = _PLAUSIBLE.sub("<!-- plausible stripped by capture_serve -->", text)
    text, local = _localise(text, root)
    for rel in local:
        print(f"  local     {rel}")
    if stub:
        text = text.replace("<head>", "<head>\n" + _SHIM, 1)
    return text.encode()


def api_file(root: Path, rel: str) -> Path | None:
    """A captured API response for `rel`, if one was recorded.

    The grabber preserves the URL path, so `api/collections/module/records` is a FILE of that name with
    no extension. Chrome numbers repeats — `records (1)` — and the bare name is the first one recorded,
    which is the one to serve.
    """
    for host in ("api.vrholes.com", "api.d3flirt.com", "api.vrsodoma.com"):
        p = root / host / rel
        if p.is_file():
            return p
        for n in (1, 2):
            alt = p.with_name(f"{p.name} ({n})")
            if alt.is_file():
                return alt
    return None


def handler_for(root: Path, stub: bool):
    index = shell_for(root, stub)
    misses: set[str] = set()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, fmt, *args):     # quiet; the misses are what matter
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_POST(self):                     # the API is mostly POST; replay is replay
            self.do_GET()

        def do_OPTIONS(self):
            self._send(b"", "text/plain")

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path in ("/", "/index.html"):
                return self._send(index, "text/html; charset=utf-8")
            if path.startswith("/__api/"):
                hit = api_file(root, path[len("/__api/"):])
                if hit is None:
                    if path not in misses:
                        misses.add(path)
                        print(f"  API MISS  {path}")
                    return self._send(b"{}", "application/json", 404)
                return self._send(hit.read_bytes(), "application/json")
            target = (root / path.lstrip("/"))
            if not target.exists() and path not in misses:
                misses.add(path)
                print(f"  MISS      {path}")
            return super().do_GET()

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("capture", type=Path)
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--no-stub", dest="stub", action="store_false",
                    help="do NOT redirect the API — the build will call the LIVE service")
    args = ap.parse_args()
    root = args.capture.resolve()
    if not root.is_dir():
        raise SystemExit(f"{root}: not a directory")

    print(f"serving {root}")
    handler = handler_for(root, args.stub)
    print(f"  api       {'replayed from the capture' if args.stub else 'LIVE — calls leave this machine'}")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"\n  http://127.0.0.1:{args.port}/\n")
        print("misses are printed once each; ^C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
