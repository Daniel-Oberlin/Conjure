"""The STT evaluation corpus — recorded utterances plus the labels that make them measurable.

Turned on with `python -m conjure.voice --stt-corpus`; off by default. Everything lands under
`temp/stt-corpus/`, which is gitignored:

    clips/20260830-142305-118.wav   audio        — machine
    manifest.jsonl                  capture log  — machine, append-only, never hand-edited
    truth.tsv                       the labels   — HUMAN, hand-edited

**The split is the point** (docs/backlogs/voice.md). Keeping hand-written truth out of a
machine-written append-only file means a capture re-run, a new manifest field, or a bug in the writer
can never corrupt labels. Scoring results live in NEITHER: N models over the corpus is derived and
regenerable, and accumulating it here would mutate the thing that is meant to hold still.

JSONL where it is written (escapes commas and quotes for free, gains fields without rewriting old
rows); TSV where it is edited (speech contains no tabs, so it survives `vim` as well as a spreadsheet —
CSV does not, the first time a transcript contains `"Conjure, make a beagle."`).

`truth` is prefilled with the recognizer's hypothesis, so labelling is scan-and-fix rather than
transcription. `reviewed` is NOT optional bookkeeping: with only two columns, `truth == hypothesis`
means either *verified correct* or *never looked at*, and an unreviewed prefill scores its own source
model at 0% WER by construction — so an abandoned labelling pass would make the measurement invert
rather than degrade.
"""

from __future__ import annotations

import io
import json
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "temp" / "stt-corpus"      # temp/ is gitignored — see .gitignore

TRUTH_HEADER = ("id", "truth", "reviewed")
# What a human might type in the `reviewed` column. Spreadsheets and text editors invite different
# habits, so accept the obvious ones rather than making the purge depend on remembering one spelling.
_REVIEWED_TRUE = {"y", "yes", "1", "true", "t", "x", "✓"}


def clip_id(when: Optional[datetime] = None) -> str:
    """A filename-safe id that sorts lexicographically into chronological order.

    Not ISO-with-colons: legal on macOS, painful in shells and URLs."""
    t = when or datetime.now()
    return f"{t:%Y%m%d-%H%M%S}-{t.microsecond // 1000:03d}"


def _tsv_safe(text: str) -> str:
    """One TSV cell. Tabs and newlines are the only characters that could break the format, and speech
    contains neither — but a recognizer is not speech, so do not rely on that."""
    return " ".join(text.split())


def input_device_name() -> str:
    """The default input device, as PortAudio sees it — the one condition tag worth recording.

    Read ONCE per session, deliberately: an open PortAudio stream stays on the device it was opened
    with, so connecting earbuds mid-session changes neither the audio nor this label. Re-reading per
    clip would produce tags that disagree with the audio they describe."""
    try:
        import pyaudio
    except Exception:  # noqa: BLE001 — a missing optional dep must not cost the corpus its audio
        return "unknown"
    pa = None
    try:
        pa = pyaudio.PyAudio()
        return str(pa.get_default_input_device_info().get("name") or "unknown")
    except Exception:  # noqa: BLE001 — no input device, or PortAudio unhappy
        return "unknown"
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001
                pass


def _wav_bytes(audio: bytes, sample_rate: int) -> bytes:
    """Audio as a complete WAV file.

    PipeCat's `SegmentedSTTService` hands `run_stt` a whole WAV container (header included), so the
    common path is a straight pass-through. Wrap raw PCM if that ever changes rather than writing a
    file that only looks like audio."""
    if audio[:4] == b"RIFF":
        return audio
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setsampwidth(2)
        w.setnchannels(1)
        w.setframerate(sample_rate)
        w.writeframes(audio)
    return buf.getvalue()


def _duration_s(wav: bytes) -> Optional[float]:
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:  # noqa: BLE001 — a duration we cannot read is not worth losing the clip over
        return None


@dataclass
class PurgeReport:
    """What a purge did. `orphans` are records whose audio is already missing — a defect to report,
    not a record to preserve: a label without its clip cannot be measured against anything."""
    removed: int = 0
    kept: int = 0
    orphans: list[str] = field(default_factory=list)


class Corpus:
    """Append-only writer for one capture session. Never raises into the voice loop: a diagnostic
    feature that can take down the thing it is diagnosing is worse than no diagnostic."""

    def __init__(self, root: Path | str = CORPUS_DIR, *, device: Optional[str] = None) -> None:
        self.root = Path(root)
        self.clips = self.root / "clips"
        self.manifest = self.root / "manifest.jsonl"
        self.truth = self.root / "truth.tsv"
        self.device = device if device is not None else input_device_name()
        self.count = 0

    def _unique_id(self, when: Optional[datetime] = None) -> str:
        base = clip_id(when)
        cid, n = base, 1
        while (self.clips / f"{cid}.wav").exists():   # same-millisecond collision: vanishingly rare, cheap to rule out
            cid = f"{base}-{n}"
            n += 1
        return cid

    def record(self, audio: bytes, *, hypothesis: str, model: str, decode_s: Optional[float] = None,
               sample_rate: int = 16000, when: Optional[datetime] = None) -> Optional[str]:
        """Write one clip: the WAV, a manifest line, and a truth row prefilled with `hypothesis`.

        Returns the clip id, or None if anything failed (already reported to the caller's log)."""
        stamp = when or datetime.now()
        wav = _wav_bytes(audio, sample_rate)
        cid = self._unique_id(stamp)

        self.clips.mkdir(parents=True, exist_ok=True)
        (self.clips / f"{cid}.wav").write_bytes(wav)

        row = {"id": cid,
               # The timestamp is a FIELD, not only the filename: otherwise every consumer parses
               # filenames and that parsing becomes load-bearing the day we want a second id scheme.
               "ts": stamp.isoformat(timespec="milliseconds"),
               "device": self.device,
               "model": model,
               "hypothesis": hypothesis,
               "decode_s": decode_s,
               "duration_s": _duration_s(wav),
               "sample_rate": sample_rate}
        with self.manifest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        new_truth = not self.truth.exists()
        with self.truth.open("a", encoding="utf-8") as fh:
            if new_truth:
                fh.write("\t".join(TRUTH_HEADER) + "\n")
            fh.write(f"{cid}\t{_tsv_safe(hypothesis)}\tn\n")

        self.count += 1
        return cid


def read_truth(root: Path | str = CORPUS_DIR) -> dict[str, tuple[str, bool]]:
    """`{id: (truth, reviewed)}` from `truth.tsv`. Unknown `reviewed` spellings count as NOT reviewed —
    the conservative direction, since an unreviewed row silently flatters the model that prefilled it."""
    path = Path(root) / "truth.tsv"
    out: dict[str, tuple[str, bool]] = {}
    if not path.exists():
        return out
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        parts = line.split("\t")
        if i == 0 and tuple(p.strip() for p in parts[:3]) == TRUTH_HEADER:
            continue
        cid = parts[0].strip()
        text = parts[1] if len(parts) > 1 else ""
        reviewed = (parts[2].strip().lower() if len(parts) > 2 else "") in _REVIEWED_TRUE
        if cid:
            out[cid] = (text, reviewed)
    return out


def purge(root: Path | str = CORPUS_DIR, *, include_reviewed: bool = False) -> PurgeReport:
    """Drop clips AND their labels together.

    A clip and its label are ONE unit: a transcript with no audio cannot be run against a model, so
    keeping labels for deleted clips would leave a directory of sentences that measures nothing.

    The asymmetry that matters is labelled-vs-unlabelled, not audio-vs-labels. Unreviewed clips are
    cheap, plentiful, and what actually fills the disk — capture outruns labelling by a wide margin —
    so they are the default. Reviewed pairs are the expensive artifact and need `include_reviewed`.
    """
    root = Path(root)
    rep = PurgeReport()
    labels = read_truth(root)
    clips_dir = root / "clips"
    manifest = root / "manifest.jsonl"
    truth = root / "truth.tsv"

    ids: set[str] = {p.stem for p in clips_dir.glob("*.wav")} if clips_dir.exists() else set()
    rep.orphans = sorted(set(labels) - ids)

    doomed = {cid for cid in ids if include_reviewed or not labels.get(cid, ("", False))[1]}
    for cid in doomed:
        (clips_dir / f"{cid}.wav").unlink(missing_ok=True)
    rep.removed = len(doomed)
    rep.kept = len(ids) - len(doomed)

    # Rewrite both records against the survivors. Orphaned rows go too — see the docstring.
    survivors = ids - doomed
    if manifest.exists():
        lines = [ln for ln in manifest.read_text(encoding="utf-8").splitlines() if ln.strip()]
        keep = [ln for ln in lines if _line_id(ln) in survivors]
        manifest.write_text("".join(f"{ln}\n" for ln in keep), encoding="utf-8")
    if truth.exists():
        rows = [f"{cid}\t{_tsv_safe(text)}\t{'y' if rev else 'n'}"
                for cid, (text, rev) in sorted(labels.items()) if cid in survivors]
        truth.write_text("\n".join(["\t".join(TRUTH_HEADER), *rows]) + "\n", encoding="utf-8")
    return rep


def _line_id(line: str) -> str:
    try:
        return str(json.loads(line).get("id", ""))
    except Exception:  # noqa: BLE001 — a corrupt line has no id and is dropped with the rest
        return ""


def summary(root: Path | str = CORPUS_DIR) -> dict:
    """Counts for the CLI: how much audio, how much of it labelled."""
    root = Path(root)
    ids = {p.stem for p in (root / "clips").glob("*.wav")} if (root / "clips").exists() else set()
    labels = read_truth(root)
    return {"clips": len(ids),
            "reviewed": sum(1 for cid in ids if labels.get(cid, ("", False))[1]),
            "orphans": len(set(labels) - ids),
            "root": str(root)}


def iter_manifest(root: Path | str = CORPUS_DIR) -> Iterable[dict]:
    """Manifest rows, oldest first — the input to an offline scoring run."""
    path = Path(root) / "manifest.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    return out
