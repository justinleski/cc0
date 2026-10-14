"""Quoted phrases are required. A stray quote is stripped, not an error."""

from uuid import UUID

import pytest

from app.search.cursor import InvalidCursor, decode_cursor, encode_cursor, parse_rank_cursor
from app.search.parse import parse_search_text
from app.search.rank import fuse

A = UUID("00000000-0000-0000-0000-00000000000a")
B = UUID("00000000-0000-0000-0000-00000000000b")
C = UUID("00000000-0000-0000-0000-00000000000c")


def test_balanced_quotes_are_a_required_phrase_and_the_rest_stays_unquoted():
    parsed = parse_search_text('  "da vinci"   coastal ')
    assert parsed.phrases == ("da vinci",)
    assert parsed.unquoted == "coastal"
    assert parsed.applied_query == '"da vinci" coastal'
    assert not parsed.quotes_only


def test_a_quotes_only_query_does_not_keep_embedding_text():
    parsed = parse_search_text('"Harbor at Dawn"')
    assert parsed.quotes_only
    assert parsed.unquoted == ""
    assert parsed.applied_query == '"Harbor at Dawn"'


def test_an_unpaired_quote_is_removed_and_the_search_text_remains():
    parsed = parse_search_text('"DaVinci')
    assert parsed.phrases == ()
    assert parsed.unquoted == "DaVinci"
    assert parsed.applied_query == "DaVinci"

    mixed = parse_search_text('say "hello')
    assert mixed.phrases == ()
    assert mixed.applied_query == "say hello"
    assert mixed.unquoted == "say hello"


def test_a_rank_offset_without_a_snapshot_is_rejected():
    raw = encode_cursor({"t": "rank", "offset": 20})
    with pytest.raises(InvalidCursor):
        parse_rank_cursor(decode_cursor(raw))


def test_fusion_promotes_an_asset_that_both_lists_rank():
    # B is first on one list only. A is second and third, so both lists vote for it.
    fused = fuse([[B, A], [C, A]])
    assert fused[0] == A
    assert set(fused) == {A, B, C}
