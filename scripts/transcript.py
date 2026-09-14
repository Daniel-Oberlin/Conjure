#!/usr/bin/env python3
"""Render a Claude Code session log to readable Markdown.

    python scripts/transcript.py --list                       # what sessions exist
    python scripts/transcript.py b9914db3                     # one session -> temp/transcripts/
    python scripts/transcript.py --all                        # every session for this project
    python scripts/transcript.py b9914db3 --tools             # ...with the tool CALLS too
    python scripts/transcript.py b9914db3 --full              # ...and their results (large)

The logs are JSONL under `~/.claude/projects/<slug>/`, one JSON object per line, and **the line number
is the record number** — so `sed -n '6749p' <file>` pulls one turn's raw JSON straight out.

Worth knowing before you choose a mode: in a 50 MB session the CONVERSATION is 0.7 MB. The rest is
2.6 MB of tool inputs, 15 MB of tool results and a great deal of per-record metadata. Prose-only is
not a lossy convenience — it is the part a person reads, at 1.4% of the size.

Written after a compaction, when the question was "can I get a copy of the transcript". The answer is
yes and it is nearly free; what was missing was a command that says so.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
SKIP_ROLES = {"last-prompt", "custom-title", "agent-name", "mode", "permission-mode",
              "atis-latch", "attachment", "summary"}


def slug_for(cwd: Path) -> str:
    """`~/dev/Conjure` → `-Users-you-dev-Conjure`, the directory Claude Code files a project under."""
    return str(cwd).replace("/", "-")


def sessions(root: Path) -> list[Path]:
    return sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def render(path: Path, *, tools: bool = False, results: bool = False, cap: int = 2000) -> str:
    out: list[str] = [f"# {path.stem}\n",
                      f"Source: `{path}`  \n"
                      f"Line number in that file == record number below.\n"]
    for n, line in enumerate(path.open(), 1):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") or {}
        role = msg.get("role") or rec.get("type") or ""
        if role in SKIP_ROLES:
            continue
        stamp = (rec.get("timestamp") or "")[:16].replace("T", " ")
        body = msg.get("content")
        chunks: list[str] = []
        if isinstance(body, str):
            chunks = [body]
        elif isinstance(body, list):
            for block in body:
                kind = block.get("type")
                if kind == "text":
                    chunks.append(block["text"])
                elif kind == "thinking":
                    chunks.append("> _thinking_\n>\n> " + block.get("thinking", "").replace("\n", "\n> "))
                elif kind == "tool_use" and tools:
                    args = json.dumps(block.get("input") or {})[:cap]
                    chunks.append(f"``` tool: {block.get('name')}\n{args}\n```")
                elif kind == "tool_result" and results:
                    got = block.get("content")
                    text = got if isinstance(got, str) else json.dumps(got)
                    chunks.append(f"``` result\n{text[:cap]}\n```")
        text = "\n\n".join(c for c in chunks if c and c.strip())
        if not text.strip():
            continue
        out.append(f"\n---\n\n### line {n} · {role}{' · ' + stamp if stamp else ''}\n\n{text}\n")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session", nargs="?", default="", help="a session id, or any unique prefix of one")
    ap.add_argument("--all", action="store_true", help="render every session for this project")
    ap.add_argument("--list", action="store_true", help="list the sessions and their sizes")
    ap.add_argument("--tools", action="store_true", help="include tool CALLS")
    ap.add_argument("--full", action="store_true", help="include tool calls AND their results")
    ap.add_argument("--cap", type=int, default=2000, help="truncate each tool blob (default 2000 chars)")
    ap.add_argument("--project", default="", help="project directory (default: the current one)")
    ap.add_argument("--out", default="temp/transcripts", help="where to write")
    args = ap.parse_args()

    root = PROJECTS / slug_for(Path(args.project or os.getcwd()).resolve())
    if not root.is_dir():
        print(f"no logs for {root.name} — is this the project directory?")
        return 2
    found = sessions(root)
    if args.list or not (args.session or args.all):
        total = 0
        for p in found:
            size = p.stat().st_size
            total += size
            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  {p.stem}  {size / 1e6:7.1f} MB  {when}")
        print(f"  {'TOTAL':36} {total / 1e6:7.1f} MB   ({len(found)} sessions)")
        return 0

    picked = found if args.all else [p for p in found if p.stem.startswith(args.session)]
    if not picked:
        print(f"no session starting with {args.session!r}")
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in picked:
        text = render(p, tools=args.tools or args.full, results=args.full, cap=args.cap)
        dest = out_dir / f"{p.stem}.md"
        dest.write_text(text)
        print(f"  {p.stem}  {p.stat().st_size / 1e6:6.1f} MB -> {dest}  "
              f"({dest.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
