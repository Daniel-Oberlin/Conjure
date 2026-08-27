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
