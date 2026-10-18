"""The voice text stage — what an LLM writes vs. what a TTS engine should say."""

from conjure.speech import drop_asterisks, for_speech, name_emoji


# ---- asterisks ---------------------------------------------------------------------------------

def test_asterisks_are_removed_in_every_form_an_llm_emits():
    assert drop_asterisks("**Done** — there's your dragon.") == "Done — there's your dragon."
    assert drop_asterisks("that's *really* nice") == "that's really nice"
    assert drop_asterisks("*shrugs*") == "shrugs"
    assert drop_asterisks("***") == ""
    assert drop_asterisks("2 * 3") == "2 3"          # arithmetic is collateral; speech is the priority


def test_removing_a_bullet_does_not_leave_a_stutter():
    """`* one` would otherwise become ` one` — a leading gap the engine pauses on. Newlines stay: they
    pace the speech, and collapsing them would run the list into one breathless sentence."""
    assert drop_asterisks("* one\n* two") == "one\ntwo"
    assert drop_asterisks("a  **b**  c") == "a b c"
    assert "\n" in drop_asterisks("one\n\ntwo")


def test_text_with_nothing_to_fix_is_returned_unchanged():
    plain = "Sure — one moment while I fetch the driftwood."
    assert for_speech(plain) == plain
    assert for_speech("") == ""
    assert for_speech(None) is None


# ---- emoji -------------------------------------------------------------------------------------

def test_an_emoji_is_named_and_labelled():
    """Otherwise the engine drops it silently and the sentence loses whatever it meant."""
    assert name_emoji("Mmm 🍦 nice") == "Mmm soft ice cream emoji nice"
    assert name_emoji("🎉") == "party popper emoji"


def test_one_cluster_is_one_name():
    """A joined family or a skin-toned thumb is ONE picture to a reader, so it gets one name. Without an
    emoji database the best available is the first component's — accurate about what is there."""
    assert name_emoji("family 👨‍👩‍👧 photo") == "family man emoji photo"
    assert name_emoji("nice 👍🏽 work") == "nice thumbs up sign emoji work"
    assert name_emoji("❤️") == "heavy black heart emoji"          # variation selector is not spoken


def test_a_run_names_every_emoji_and_pluralises_once():
    """Naming only the first would silently discard the rest, and a listener can't tell something went.
    Runs are rare; slightly awkward to hear beats losing content."""
    assert name_emoji("👍🎉🔥") == "thumbs up sign party popper fire emojis"
    assert name_emoji("nice 👍🎉") == "nice thumbs up sign party popper emojis"


def test_emoji_separated_by_text_are_named_separately():
    """A space breaks the run — these are two decorations, not one string."""
    assert name_emoji("👍 🎉") == "thumbs up sign emoji party popper emoji"


def test_symbols_that_are_not_emoji_are_left_alone():
    """`unicodedata.category == "So"` would have caught these too — "copyright sign emoji" is worse
    than saying nothing, so the check is explicit pictographic ranges instead."""
    for s in ("© 2026", "Ryman®", "a™b", "5 °C", "naïve café"):
        assert name_emoji(s) == s


# ---- the stage ---------------------------------------------------------------------------------

def test_the_stage_composes_both_in_order():
    assert for_speech("**Yes!** 🎉") == "Yes! party popper emoji"


def test_the_stage_is_data_so_a_case_can_be_added():
    """`_STAGES` is the extension point — the module's whole reason for existing separately."""
    from conjure import speech
    assert [name for name, _ in speech._STAGES] == ["asterisks", "emoji"]
    assert all(callable(fn) for _, fn in speech._STAGES)


# --------------------------------------------------------------------------- speech tags

from conjure.speech import strip_tags  # noqa: E402

TAGS = ["laugh", "pause", "long-pause", "whisper", "slow", "emphasis"]


def test_an_inline_tag_goes_and_the_sentence_closes_up():
    assert strip_tags("Really? [laugh] That's incredible!", TAGS) == "Really? That's incredible!"
    assert strip_tags("[laugh] Sure thing.", TAGS) == "Sure thing."
    assert strip_tags("Done [laugh].", TAGS) == "Done."
    assert strip_tags("Yes[laugh]no", TAGS) == "Yes no"


def test_a_wrapping_tag_goes_and_its_span_stays():
    assert strip_tags("<whisper>It is a secret.</whisper> Pretty cool, right?", TAGS) == \
        "It is a secret. Pretty cool, right?"
    assert strip_tags("Goodnight, <slow>sleep well</slow>.", TAGS) == "Goodnight, sleep well."


def test_only_named_tags_are_touched():
    """`[asset 3f2a]` is how the builder prompt names a library asset; a stripper that guessed at shapes
    would eat it. Names only."""
    assert strip_tags("the [asset 3f2a] one, see <id>", TAGS) == "the [asset 3f2a] one, see <id>"
    assert strip_tags("a [giggle] b", TAGS) == "a [giggle] b"           # not in THIS vocabulary


def test_tags_are_matched_whole_and_case_insensitively():
    assert strip_tags("[Laugh]Hi", TAGS) == "Hi"
    assert strip_tags("a [long-pause] b [pause] c", TAGS) == "a b c"


def test_keep_lets_the_rendering_engine_hear_its_own_tags():
    out = strip_tags("Really? [laugh] <whisper>hi</whisper> [giggle]", TAGS + ["giggle"], keep=["whisper", "laugh"])
    assert out == "Really? [laugh] <whisper>hi</whisper>"


def test_lines_keep_their_breaks_and_lose_the_gap():
    assert strip_tags("line one [laugh]\n[pause] line two", TAGS) == "line one\nline two"


def test_empty_vocabulary_and_empty_text_are_untouched():
    assert strip_tags("Done [laugh].", []) == "Done [laugh]."
    assert strip_tags("", TAGS) == ""
    assert strip_tags("plain", TAGS) == "plain"


def test_the_stage_and_the_stripper_compose_for_a_tagless_voice_client():
    assert strip_tags(for_speech("**Done!** [laugh] 🎉"), TAGS) == "Done! party popper emoji"
