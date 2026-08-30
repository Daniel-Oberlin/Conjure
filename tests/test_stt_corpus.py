"""The STT evaluation corpus (conjure.stt_corpus).

Pure file-level behaviour — no pipecat, no microphone. The properties worth pinning are the ones that
make the corpus *measurable*: a clip and its label live and die together, an unreviewed prefill is
distinguishable from a verified one, and ids sort into chronological order."""

from __future__ import annotations

import json
from datetime import datetime

from conjure.stt_corpus import Corpus, clip_id, purge, read_truth, summary


def _corpus(tmp_path, **kw):
    return Corpus(tmp_path / "stt-corpus", device="TestMic", **kw)


def _wav(n_samples: int = 1600) -> bytes:
    """Raw PCM, not a WAV container — exercises the wrap path."""
    return b"\x00\x01" * n_samples


def test_clip_ids_are_filename_safe_and_sort_chronologically():
    early = clip_id(datetime(2026, 8, 30, 14, 23, 5, 118_000))
    late = clip_id(datetime(2026, 8, 30, 14, 23, 5, 903_000))
    assert early == "20260830-142305-118"
    assert early < late                                   # lexicographic == chronological
    assert not (set(early) & set(':/ '))                  # safe in shells, URLs and filenames


def test_record_writes_clip_manifest_and_a_prefilled_unreviewed_label(tmp_path):
    c = _corpus(tmp_path)
    cid = c.record(_wav(), hypothesis="make a beagle", model="small.en", decode_s=1.4)

    assert (c.clips / f"{cid}.wav").read_bytes()[:4] == b"RIFF"     # raw PCM got wrapped

    row = json.loads(c.manifest.read_text().splitlines()[0])
    assert row["id"] == cid and row["model"] == "small.en" and row["device"] == "TestMic"
    assert row["hypothesis"] == "make a beagle" and row["decode_s"] == 1.4
    assert row["ts"].startswith("20")            # a FIELD, so consumers never parse filenames
    assert row["duration_s"] == 0.1              # 1600 samples @ 16 kHz

    # Truth is prefilled with the hypothesis so labelling is scan-and-fix, and marked NOT reviewed.
    assert read_truth(c.root) == {cid: ("make a beagle", False)}


def test_a_wav_container_is_written_through_unchanged(tmp_path):
    """PipeCat hands `run_stt` a whole WAV; re-wrapping it would corrupt the audio."""
    c = _corpus(tmp_path)
    first = c.record(_wav(), hypothesis="one", model="m")
    already_wav = (c.clips / f"{first}.wav").read_bytes()

    second = c.record(already_wav, hypothesis="two", model="m")
    assert (c.clips / f"{second}.wav").read_bytes() == already_wav


def test_tabs_and_newlines_never_break_the_tsv(tmp_path):
    c = _corpus(tmp_path)
    cid = c.record(_wav(), hypothesis="make\ta beagle\nnext to the table", model="m")
    assert len(c.truth.read_text().splitlines()) == 2                  # header + exactly one row
    assert read_truth(c.root)[cid][0] == "make a beagle next to the table"


def test_same_millisecond_clips_do_not_overwrite_each_other(tmp_path):
    c = _corpus(tmp_path)
    when = datetime(2026, 8, 30, 14, 23, 5, 118_000)
    a = c.record(_wav(), hypothesis="one", model="m", when=when)
    b = c.record(_wav(), hypothesis="two", model="m", when=when)
    assert a != b and (c.clips / f"{a}.wav").exists() and (c.clips / f"{b}.wav").exists()


def _mark_reviewed(c, cid: str, truth: str) -> None:
    """Stand in for the human editing truth.tsv in a spreadsheet."""
    lines = c.truth.read_text().splitlines()
    out = [ln if not ln.startswith(f"{cid}\t") else f"{cid}\t{truth}\ty" for ln in lines]
    c.truth.write_text("\n".join(out) + "\n")


def test_purge_drops_unreviewed_pairs_and_keeps_reviewed_ones(tmp_path):
    c = _corpus(tmp_path)
    keep = c.record(_wav(), hypothesis="make a bagel", model="m")
    drop = c.record(_wav(), hypothesis="never labelled", model="m")
    _mark_reviewed(c, keep, "make a beagle")

    rep = purge(c.root)
    assert (rep.removed, rep.kept) == (1, 1)
    assert (c.clips / f"{keep}.wav").exists() and not (c.clips / f"{drop}.wav").exists()

    # Both records follow the audio — the label AND the manifest line for the dropped clip are gone.
    assert read_truth(c.root) == {keep: ("make a beagle", True)}
    assert [json.loads(ln)["id"] for ln in c.manifest.read_text().splitlines()] == [keep]


def test_purge_all_needs_the_flag_and_then_takes_everything(tmp_path):
    c = _corpus(tmp_path)
    cid = c.record(_wav(), hypothesis="h", model="m")
    _mark_reviewed(c, cid, "the truth")

    assert purge(c.root).removed == 0                    # reviewed: untouched by the default purge
    assert purge(c.root, include_reviewed=True).removed == 1
    assert read_truth(c.root) == {} and c.manifest.read_text() == ""


def test_a_label_whose_audio_is_gone_is_reported_and_dropped(tmp_path):
    """A transcript with no clip cannot be run against a model, so it is a defect, not a record."""
    c = _corpus(tmp_path)
    cid = c.record(_wav(), hypothesis="h", model="m")
    _mark_reviewed(c, cid, "the truth")
    (c.clips / f"{cid}.wav").unlink()                    # audio lost outside our control

    rep = purge(c.root)
    assert rep.orphans == [cid] and read_truth(c.root) == {}


def test_unreviewed_is_the_conservative_reading_of_an_unknown_marker(tmp_path):
    """An unreviewed prefill scores its own source model at 0% WER, so anything ambiguous must count
    as NOT reviewed — otherwise an abandoned labelling pass flatters whichever model wrote it."""
    c = _corpus(tmp_path)
    cid = c.record(_wav(), hypothesis="h", model="m")
    c.truth.write_text(f"id\ttruth\treviewed\n{cid}\th\tmaybe\n")

    assert read_truth(c.root)[cid][1] is False
    assert purge(c.root).removed == 1

    # …while the spellings a human actually types all count as reviewed.
    for marker in ("y", "Y", "yes", "1", "true", "x"):
        c2 = _corpus(tmp_path / marker)
        cid2 = c2.record(_wav(), hypothesis="h", model="m")
        c2.truth.write_text(f"id\ttruth\treviewed\n{cid2}\th\t{marker}\n")
        assert read_truth(c2.root)[cid2][1] is True, marker


def test_summary_counts_what_is_there(tmp_path):
    c = _corpus(tmp_path)
    a = c.record(_wav(), hypothesis="one", model="m")
    c.record(_wav(), hypothesis="two", model="m")
    _mark_reviewed(c, a, "one")

    s = summary(c.root)
    assert (s["clips"], s["reviewed"], s["orphans"]) == (2, 1, 0)


def test_recording_survives_an_empty_corpus_directory(tmp_path):
    """First run: nothing exists yet, including the header."""
    c = _corpus(tmp_path)
    assert not c.root.exists()
    c.record(_wav(), hypothesis="first ever", model="m")
    assert c.truth.read_text().splitlines()[0] == "id\ttruth\treviewed"
