"""Conjure CLI — drive the world from the terminal for quick, quiet, discrete testing.

The world server must be running (`python -m conjure`). Two ways to drive it:

  • Direct tool commands (deterministic):
        conjure-cli asset "oak tree" --size 7
        conjure-cli image "an oil painting of a red dragon"
        conjure-cli skybox "a misty pine forest"
        conjure-cli add box --color red --pos 0 1 -3
        conjure-cli world

  • The director, by text — no voice, no audio noise:
        conjure-cli say "put an oak tree in front of me and hang a sunset painting on the wall"
        conjure-cli                # no args → interactive director REPL (type instructions)

Quiet by default. Pass -v/--verbose for tool calls + library logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid

import httpx

from .config import Settings, get_settings
from .director import Director


# --------------------------------------------------------------------------- helpers

def _server_ok(s: Settings) -> bool:
    try:
        httpx.get(f"{s.world_url}/world", timeout=3.0)
        return True
    except Exception:
        return False


def _post(s: Settings, path: str, body: dict) -> dict:
    r = httpx.post(f"{s.world_url}{path}", json=body, timeout=240.0)
    r.raise_for_status()
    return r.json()


def _get(s: Settings, path: str) -> dict:
    r = httpx.get(f"{s.world_url}{path}", timeout=30.0)
    r.raise_for_status()
    return r.json()


def _say(obj: dict, verbose: bool, fallback: str) -> None:
    if verbose:
        print(json.dumps(obj, indent=2))
    elif obj.get("ok") is False:
        print(f"error: {obj.get('error', 'unknown error')}")
    else:
        print(fallback)


def _working(msg: str) -> None:
    """A one-line status to stderr so a slow image generation doesn't look like a freeze."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- direct commands

def cmd_world(s: Settings, a) -> None:
    doc = _get(s, "/world")
    print(f"{doc.get('name', '')} (rev {doc['rev']}), {len(doc['entities'])} entities:")
    for e in doc["entities"]:
        c, m = e.get("components", {}), e.get("meta", {})
        if "gltf-model" in c:
            d = f"model {m.get('title', '?')!r}"
        elif c.get("material", {}).get("src"):
            d = f"image {(m.get('prompt') or m.get('title') or '?')!r}"
        elif "grid" in c:
            d = "grid"
        else:
            d = f"{c.get('geometry', {}).get('primitive', '?')} {c.get('material', {}).get('color', '')}".strip()
        print(f"  {e['id']}: {d} @ {e.get('transform', {}).get('position')}")


def cmd_add(s: Settings, a) -> None:
    eid = a.name or f"ent_{a.shape}_{uuid.uuid4().hex[:6]}"
    entity = {
        "id": eid,
        "transform": {"position": a.pos or [0.0, 1.0, -3.0]},
        "components": {"geometry": {"primitive": a.shape}, "material": {"color": a.color}},
    }
    r = _post(s, "/patch", {"origin": "cli", "ops": [{"op": "add", "entity": entity}]})
    print(f"added {eid} (rev {r['rev']})") if not a.verbose else _say(r, True, "")


def cmd_move(s: Settings, a) -> None:
    r = _post(s, "/patch", {"origin": "cli",
                            "ops": [{"op": "update", "id": a.id, "set": {"transform.position": a.pos}}]})
    print(f"moved {a.id} (rev {r['rev']})")


def cmd_remove(s: Settings, a) -> None:
    r = _post(s, "/patch", {"origin": "cli", "ops": [{"op": "remove", "id": a.id}]})
    print(f"removed {a.id} (rev {r['rev']})")


def cmd_env(s: Settings, a) -> None:
    sets: dict = {}
    if a.sky_color:
        sets["sky"] = {"color": a.sky_color}
    if a.fog_color or a.fog_density is not None:
        fog = {"type": "exponential"}
        if a.fog_color:
            fog["color"] = a.fog_color
        if a.fog_density is not None:
            fog["density"] = a.fog_density
        sets["fog"] = fog
    if not sets:
        print("nothing to set")
        return
    r = _post(s, "/patch", {"origin": "cli", "ops": [{"op": "env", "set": sets}]})
    print(f"environment updated (rev {r['rev']})")


def cmd_asset(s: Settings, a) -> None:
    body = {"query": a.query, "size_m": a.size}
    if a.pos:
        body["position"] = a.pos
    _say(_post(s, "/place_asset", body), a.verbose, f"placed asset for {a.query!r}")


def cmd_image(s: Settings, a) -> None:
    # Procurement is decoupled from placement; the CLI runs both steps for convenience.
    gen_body = {"prompt": a.prompt}
    if a.transparent:
        gen_body["transparent"] = True
    if a.generator:
        gen_body["generator"] = a.generator
    _working("generating image…")
    procured = _post(s, "/images/generate", gen_body)
    if procured.get("ok") is False:
        _say(procured, a.verbose, "")
        return
    body = {"image_id": procured["image_id"]}
    if a.pos:
        body["position"] = a.pos
    if a.size is not None:
        body["size_m"] = a.size
    _say(_post(s, "/place_image", body), a.verbose, f"placed image ({procured.get('provider', '?')})")


def cmd_skybox(s: Settings, a) -> None:
    gen_body = {"prompt": a.prompt}
    if a.generator:
        gen_body["generator"] = a.generator
    _working("generating skybox (high-res — this can take a minute)…")
    procured = _post(s, "/images/skybox", gen_body)
    if procured.get("ok") is False:
        _say(procured, a.verbose, "")
        return
    _say(_post(s, "/set_skybox", {"image_id": procured["image_id"]}), a.verbose, "set skybox")


def cmd_generators(s: Settings, a) -> None:
    out = _get(s, "/images/generators")
    if a.verbose:
        print(json.dumps(out, indent=2))
        return
    for g in out.get("generators", []):
        c = g["capabilities"]
        vendor = f" ({g['vendor']})" if g.get("vendor") else ""
        print(f"{g['name']}{vendor}: ops={','.join(c['operations'])}, edit={c['edit_mode']}, "
              f"max={c['max_resolution']}px, aspect={c['aspect']}, transparency={c['transparency']}")
    print(f"defaults: {out.get('defaults', {})}")


def cmd_edit(s: Settings, a) -> None:
    _working("editing image…")
    _say(_post(s, "/edit_image", {"id": a.id, "prompt": a.prompt}), a.verbose, f"edited {a.id}")


def cmd_outpaint(s: Settings, a) -> None:
    body = {"id": a.id}
    if a.aspect:
        body["aspect"] = a.aspect
    _working("outpainting image…")
    _say(_post(s, "/outpaint_image", body), a.verbose, f"outpainted {a.id}")


def cmd_skybox_from(s: Settings, a) -> None:
    _working("building skybox from image (high-res — this can take a minute)…")
    _say(_post(s, "/skybox_from_image", {"id": a.id}), a.verbose, f"skybox from {a.id}")


# --------------------------------------------------------------------------- director (text)

async def _director(s: Settings, instructions, verbose: bool) -> None:
    """Run text instructions through the shared director (conjure.director). `instructions` is an
    async iterator of strings (one-shot or interactive). The director owns the LLM roster, the MCP
    tools, and mid-conversation LLM switching; the CLI only decides how to print."""
    errlog = sys.stderr if verbose else None
    async with Director.connect(s, errlog=errlog) as director:
        multi = len(director.roster) > 1  # show who's speaking once there's more than one LLM

        async def on_text(text: str, *, final: bool, speaker: str) -> None:
            # Quiet by default: print only the closing reply; show running narration with -v.
            if verbose or final:
                print(f"{speaker}: {text}" if multi else text)

        async def on_tool(name: str, args: dict) -> None:
            if verbose:
                print(f"  · {name}({json.dumps(args)})")

        async for text in instructions:
            try:
                await director.handle(text, on_text=on_text, on_tool=on_tool)
            except Exception as exc:  # one bad turn (API error, quota, …) shouldn't kill the REPL
                print(f"error: {exc}")


def cmd_say(s: Settings, a) -> None:
    async def once():
        yield " ".join(a.text)
    asyncio.run(_director(s, once(), a.verbose))


def cmd_repl(s: Settings, a) -> None:
    print("Conjure director REPL — type an instruction (':q' to quit).\n"
          "With more than one LLM configured you can switch ('let me talk to Gemini') or address "
          "one for a single turn ('Claude, make a picture of a cat').")

    async def lines():
        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, lambda: input("conjure> "))
            except (EOFError, KeyboardInterrupt):
                print()
                return
            line = line.strip()
            if line in (":q", ":quit", "exit"):
                return
            if line:
                yield line

    asyncio.run(_director(s, lines(), a.verbose))


# --------------------------------------------------------------------------- argparse

def _pos(p):
    p.add_argument("--pos", nargs=3, type=float, metavar=("X", "Y", "Z"), help="position in meters")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="conjure-cli", description="Drive the Conjure world from the terminal.")
    p.add_argument("-v", "--verbose", action="store_true", help="show tool calls and library logs")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("world", help="print the current world").set_defaults(fn=cmd_world)

    a = sub.add_parser("add", help="add a primitive shape"); a.set_defaults(fn=cmd_add)
    a.add_argument("shape"); a.add_argument("--color", default="white"); a.add_argument("--name"); _pos(a)

    a = sub.add_parser("asset", help="place a real 3D model (Poly Pizza)"); a.set_defaults(fn=cmd_asset)
    a.add_argument("query"); a.add_argument("--size", type=float, default=1.0, help="real-world size, meters"); _pos(a)

    a = sub.add_parser("image", help="generate + hang an image"); a.set_defaults(fn=cmd_image)
    a.add_argument("prompt"); a.add_argument("--size", type=float)
    a.add_argument("--transparent", action="store_true", help="cut-out with a transparent background")
    a.add_argument("--generator", help="force an image generator (else best default)"); _pos(a)

    a = sub.add_parser("skybox", help="generate a 360 skybox"); a.set_defaults(fn=cmd_skybox)
    a.add_argument("prompt"); a.add_argument("--generator", help="force an image generator")

    sub.add_parser("generators", help="list image generators + capabilities").set_defaults(fn=cmd_generators)

    a = sub.add_parser("edit", help="edit an in-world image"); a.set_defaults(fn=cmd_edit)
    a.add_argument("id"); a.add_argument("prompt")

    a = sub.add_parser("outpaint", help="extend an in-world image wider"); a.set_defaults(fn=cmd_outpaint)
    a.add_argument("id"); a.add_argument("--aspect")

    a = sub.add_parser("skybox-from", help="turn an in-world image into the sky"); a.set_defaults(fn=cmd_skybox_from)
    a.add_argument("id")

    a = sub.add_parser("move", help="move an entity"); a.set_defaults(fn=cmd_move)
    a.add_argument("id"); a.add_argument("pos", nargs=3, type=float, metavar=("X", "Y", "Z"))

    a = sub.add_parser("remove", help="remove an entity"); a.set_defaults(fn=cmd_remove)
    a.add_argument("id")

    a = sub.add_parser("env", help="set sky/fog"); a.set_defaults(fn=cmd_env)
    a.add_argument("--sky-color"); a.add_argument("--fog-color"); a.add_argument("--fog-density", type=float)

    a = sub.add_parser("say", help="run a text instruction through the director"); a.set_defaults(fn=cmd_say)
    a.add_argument("text", nargs="+")

    sub.add_parser("repl", help="interactive director REPL").set_defaults(fn=cmd_repl)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    for name in ("httpx", "anthropic", "mcp", "google_genai"):
        logging.getLogger(name).setLevel(logging.INFO if args.verbose else logging.WARNING)

    settings = get_settings()
    if not _server_ok(settings):
        print(f"World server not reachable at {settings.world_url}. Start it: python -m conjure")
        return 1

    fn = getattr(args, "fn", None) or cmd_repl  # no subcommand → director REPL
    try:
        fn(settings, args)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:  # e.g. no LLM keys for the director (say/repl)
        print(exc)
        return 1
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
