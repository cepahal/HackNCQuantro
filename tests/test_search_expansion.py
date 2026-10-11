from backend.services.external_apis.search_expansion import widen_queries
from backend.services.shared.types import Queries


def test_empty_and_unquoted_clues_do_not_invent_expansion():
    for clues in ([], ["we will not cancel this program"]):
        result = widen_queries(Queries(queries=clues, warnings=["existing"]))
        assert result.queries == []
        assert result.warnings == ["existing"]


def test_unused_original_clues_take_priority_and_output_is_bounded():
    original = Queries(queries=['"one phrase"', "second", "third", "unused fourth", "unused fifth", "unused sixth", "unused seventh"])
    assert widen_queries(original).queries == ["unused fourth", "unused fifth", "unused sixth"]
    assert original.queries[0] == '"one phrase"'


def test_relaxation_preserves_unicode_words_negation_and_provenance_warning():
    original = Queries(queries=['“Nous ne supprimerons pas l’éducation”', '"we will not cancel" program'], warnings=["existing"])
    result = widen_queries(original)
    assert result.queries == ["Nous ne supprimerons pas l’éducation", "we will not cancel program"]
    assert result.warnings[0] == "existing"
    assert "unverified" in result.warnings[-1]
    assert result.model_dump().keys() == {"queries", "warnings"}


def test_deduplication_and_overlength_are_skipped_without_truncation():
    result = widen_queries(Queries(queries=['"same words"', "same words", "third", "same words", "x" * 513, '"not false"']))
    assert result.queries == ['"not false"', "not false"]
    assert all(len(query) <= 512 for query in result.queries)


def test_unmatched_quote_does_not_change_query():
    assert widen_queries(Queries(queries=['"do not remove unmatched quote'])).queries == []
