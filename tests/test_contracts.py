import pytest

from src.common.contracts import (
    canonical_url,
    chunk_id_for,
    doc_id_for,
    parse_chunk_id,
    sha256_hex,
    strategy_tag,
)

URL = "https://www.faa.gov/uas/recreational_flyers"


def test_doc_id_is_first_12_hex_of_sha256_of_canonical_url():
    assert doc_id_for(URL) == sha256_hex(URL)[:12]
    assert len(doc_id_for(URL)) == 12
    assert all(c in "0123456789abcdef" for c in doc_id_for(URL))


def test_doc_id_is_stable_across_trivial_url_differences():
    variants = [
        "https://www.faa.gov/uas/recreational_flyers/",
        "HTTPS://WWW.FAA.GOV/uas/recreational_flyers",
        "https://www.faa.gov:443/uas/recreational_flyers",
        "https://www.faa.gov/uas/recreational_flyers#section-2",
        "https://www.faa.gov/uas//recreational_flyers?utm_source=x",
    ]
    assert {doc_id_for(v) for v in variants} == {doc_id_for(URL)}


def test_different_pages_get_different_doc_ids():
    assert doc_id_for(URL) != doc_id_for("https://www.faa.gov/uas/commercial_operators")


def test_path_case_and_query_order_are_handled():
    # Paths are case-sensitive on the server, so they are not lowercased.
    assert canonical_url("https://faa.gov/UAS") != canonical_url("https://faa.gov/uas")
    assert canonical_url("https://faa.gov/a?b=2&a=1") == canonical_url("https://faa.gov/a?a=1&b=2")
    assert canonical_url("https://faa.gov") == "https://faa.gov/"


def test_canonical_url_rejects_relative_urls():
    with pytest.raises(ValueError):
        canonical_url("/uas/recreational_flyers")


def test_chunk_id_format_matches_proposal_example():
    assert chunk_id_for("a1b2c3d4e5f6", "struct", "v1", 7) == "a1b2c3d4e5f6::struct@v1::0007"
    assert chunk_id_for("a1b2c3d4e5f6", "fixed", "v2", 12345) == "a1b2c3d4e5f6::fixed@v2::12345"


def test_chunk_id_round_trips():
    cid = chunk_id_for(doc_id_for(URL), "fixed", "v1", 3)
    assert parse_chunk_id(cid) == (doc_id_for(URL), "fixed", "v1", 3)


@pytest.mark.parametrize(
    "args",
    [
        ("A1B2C3D4E5F6", "struct", "v1", 0),  # uppercase doc_id
        ("a1b2c3", "struct", "v1", 0),  # too short
        ("a1b2c3d4e5f6", "semantic", "v1", 0),  # unknown strategy
        ("a1b2c3d4e5f6", "struct", "1", 0),  # version without v
        ("a1b2c3d4e5f6", "struct", "v1", -1),  # negative ordinal
    ],
)
def test_chunk_id_for_rejects_bad_input(args):
    with pytest.raises(ValueError):
        chunk_id_for(*args)


@pytest.mark.parametrize("bad", ["", "a1b2c3d4e5f6::struct@v1::7", "a1b2c3d4e5f6::foo@v1::0007"])
def test_parse_chunk_id_rejects_bad_ids(bad):
    with pytest.raises(ValueError):
        parse_chunk_id(bad)


def test_strategy_tag():
    assert strategy_tag("struct", "v1") == "struct@v1"
