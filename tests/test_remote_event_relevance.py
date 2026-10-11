"""Named events must outrank unrelated videos about the same everyday topic."""

import pytest

import backend.services.reporting.run_analysis as analysis


@pytest.mark.parametrize(('submitted', 'quote', 'related', 'distractor'), [
    ('Former U.S. president Donald Trump orders 30 milkshakes at Atlanta Chick-fil-A',
     '"So can I have 30 milkshakes"',
     'President Trump Stops By Chick-fil-A in Vine City, Atlanta',
     'Homemade Milkshake vs Chick-Fil-A'),
    ('Joe Biden buys ice cream in Cleveland', '"Can I have two scoops of ice cream"',
     'Biden stops for ice cream in Cleveland', 'Full ice cream recipe and tasting'),
    ('Alexandria Ocasio-Cortez asks about housing in Queens', '"We need more affordable housing"',
     'Ocasio-Cortez Queens town hall on affordable housing', 'Full affordable housing tour in Tokyo'),
    ("Obama: We're building Iron Man", '"we are building Iron Man"',
     'Obama manufacturing innovation remarks and Iron Man joke', 'Iron Man full movie commentary'),
])
def test_named_event_beats_shared_topic_even_when_distractor_claims_full_length(submitted, quote, related, distractor):
    event = {'title': related}
    unrelated = {'title': distractor, 'snippet': 'Duration: 1:30:00. Full video.'}
    ranked = sorted([unrelated, event], key=lambda row: analysis._remote_relevance_priority(
        row, submitted, None, quote))
    assert ranked[0] is event


@pytest.mark.parametrize('title', [
    'Homemade Milkshake vs Chick-Fil-A',
    '1.25 GALLON Vanilla Milkshake (6,090 Calories)',
    'Which Drive-Thru Has the THICKEST Milkshake?',
    'I Tried Every Drive Thru Milkshake',
])
def test_recipe_results_do_not_spend_native_event_verification_budget(title):
    assert not analysis._remote_candidate_relevant(
        {'title': title},
        'Former U.S. president Donald Trump orders 30 milkshakes at Atlanta Chick-fil-A',
        None, '"So can I have 30 milkshakes"',
    )


def test_missing_candidate_metadata_is_unknown_rather_than_a_contradiction():
    assert analysis._remote_candidate_relevant(
        {'title': None, 'snippet': None}, 'Donald Trump visits Atlanta', None,
        '"We should not cancel the program"',
    )


def test_distinctive_speech_can_rescue_a_candidate_when_post_identity_clue_is_wrong():
    assert analysis._remote_candidate_relevant(
        {'title': 'We should not cancel the program if the figures were accurate'},
        'An unverified title naming Alice Example', None,
        '"We should not cancel the program if the figures were accurate"',
    )


def test_unknown_post_metadata_does_not_require_an_invented_person_or_event():
    assert analysis._remote_candidate_relevant(
        {'title': 'A full recording of a local meeting'}, None, None,
        '"We should not cancel the program if the figures were accurate"',
    )
