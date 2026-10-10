"""Answer checks: citation validity and the 25-word quote limit (ethics safeguard 2)."""

import pytest

from src.generate.checks import (
    check_sentences,
    citation_validity,
    longest_common_run,
    quote_limit,
    words,
)

A = "a1b2c3d4e5f6::struct@v1::0000"
B = "a1b2c3d4e5f6::struct@v1::0001"
NOT_RETRIEVED = "ffffffffffff::struct@v1::0009"

# 40 words of source text to copy from.
SOURCE = (
    "Recreational flyers must pass the free online safety test, carry proof of passing, "
    "fly only for fun, follow the safety guidelines of a community based organization, "
    "keep the drone within visual line of sight, and give way to crewed aircraft at all times."
)
CHUNKS = {A: SOURCE, B: "Remote ID lets a drone share its identification and location in flight."}


def copy_of(n_words, start=0):
    return " ".join(SOURCE.split()[start : start + n_words])


def s(text, *ids):
    return {"text": text, "chunk_ids": list(ids)}


# --- Citation validity --------------------------------------------------------------


def test_sentence_citing_retrieved_chunks_is_valid():
    (v,) = citation_validity([s("Fly for fun only.", A, B)], CHUNKS)
    assert v == {"ok": True, "reason": None}


def test_sentence_without_a_citation_fails():
    (v,) = citation_validity([s("Fly for fun only.")], CHUNKS)
    assert v == {"ok": False, "reason": "no citation"}


def test_citing_a_chunk_that_was_not_retrieved_fails():
    (v,) = citation_validity([s("Fly for fun only.", A, NOT_RETRIEVED)], CHUNKS)
    assert not v["ok"]
    assert NOT_RETRIEVED in v["reason"]


@pytest.mark.parametrize(
    "sentence",
    [{"text": "x"}, {"text": "x", "chunk_ids": A}, {"text": "x", "chunk_ids": [1]}, "x"],
)
def test_malformed_citations_fail_closed(sentence):
    (v,) = citation_validity([sentence], CHUNKS)
    assert v == {"ok": False, "reason": "malformed chunk_ids"}


# --- Quote limit ----------------------------------------------------------------------


def test_planted_26_word_copy_is_caught():
    sentence = s(copy_of(26) + ".", A)
    (v,) = quote_limit([sentence], CHUNKS)
    assert v["ok"] is False
    assert v["longest_run"] == 26
    assert v["chunk_id"] == A
    assert v["copied"] == " ".join(words(copy_of(26)))


def test_exactly_25_copied_words_are_allowed():
    (v,) = quote_limit([s(copy_of(25), A)], CHUNKS)
    assert v == {"ok": True, "longest_run": 25, "chunk_id": None, "copied": None}


def test_case_punctuation_curly_quotes_and_citation_marks_do_not_hide_a_copy():
    disguised = copy_of(30).upper().replace(",", ";").replace(" ", " [1] ", 1)
    disguised = "“" + disguised + "”"
    (v,) = quote_limit([s(disguised, A)], CHUNKS)
    assert not v["ok"] and v["longest_run"] == 30


def test_copy_from_a_retrieved_but_uncited_chunk_still_counts():
    (v,) = quote_limit([s(copy_of(27), B)], CHUNKS)
    assert not v["ok"] and v["chunk_id"] == A


def test_a_long_quote_split_across_two_sentences_is_caught():
    first, second = s(copy_of(15), A), s(copy_of(15, start=15), A)
    verdicts = quote_limit([first, second], CHUNKS)
    assert [v["ok"] for v in verdicts] == [False, False]
    assert verdicts[0]["across_sentences"] == [0, 1]
    assert verdicts[0]["chunk_id"] == A


def test_short_overlaps_across_sentences_pass():
    verdicts = quote_limit([s(copy_of(10), A), s(copy_of(10, start=10), A)], CHUNKS)
    assert all(v["ok"] for v in verdicts)


def test_a_paraphrase_passes():
    paraphrase = (
        "Hobby pilots need to pass a free online test, keep the drone in sight, "
        "and always yield to crewed aircraft."
    )
    (v,) = quote_limit([s(paraphrase, A)], CHUNKS)
    assert v["ok"] and v["longest_run"] < 10


def test_malformed_sentence_text_fails_closed():
    (v,) = quote_limit([{"chunk_ids": [A]}], CHUNKS)
    assert v["ok"] is False


def test_custom_limit():
    (v,) = quote_limit([s(copy_of(11), A)], CHUNKS, max_words=10)
    assert not v["ok"]


def test_longest_common_run():
    assert longest_common_run("a b c d".split(), "x b c y".split()) == (2, 3)
    assert longest_common_run([], ["a"]) == (0, 0)


# --- Whole answer -------------------------------------------------------------------------


def test_check_sentences_combines_both_checks_per_sentence():
    good = s("Hobby flyers take a free online test.", A)
    copied = s(copy_of(26), A)
    uncited = s("Remote ID shares location.")
    result = check_sentences([good, copied, uncited], CHUNKS)
    assert result["ok"] is False
    assert [v["ok"] for v in result["sentences"]] == [True, False, False]
    assert result["sentences"][1]["quote_limit"]["ok"] is False
    assert result["sentences"][1]["citation_validity"]["ok"] is True
    assert result["sentences"][2]["citation_validity"]["reason"] == "no citation"
    assert result["sentences"][0]["text"] == good["text"]
    assert result["sentences"][0]["chunk_ids"] == [A]


def test_a_clean_answer_passes():
    result = check_sentences([s("Hobby flyers take a free online test.", A)], CHUNKS)
    assert result["ok"] is True


def test_only_requested_checks_run():
    result = check_sentences([s(copy_of(26))], CHUNKS, checks=["quote_limit"])
    (v,) = result["sentences"]
    assert set(v) == {"text", "chunk_ids", "quote_limit", "ok"}


def test_unknown_check_is_rejected():
    with pytest.raises(ValueError):
        check_sentences([s("x", A)], CHUNKS, checks=["claim_support"])


def test_an_empty_answer_is_not_ok():
    assert check_sentences([], CHUNKS) == {"ok": False, "sentences": []}
