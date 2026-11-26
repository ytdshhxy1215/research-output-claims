from dataclasses import replace
from datetime import date

import pytest

from apps.matching.matcher import (
    NAME_POLICY,
    WEIGHTED_POLICY,
    Affiliation,
    History,
    Matcher,
    Mention,
    Policy,
    Profile,
    TrustedIdentifier,
    normalize_name,
)

DAY = date(2025, 1, 9)
START = date(2020, 1, 1)


def mention(**kwargs):
    return replace(Mention("slot-1", "work-1", "paper", 1, "林岚", event_date=DAY), **kwargs)


def by_code(candidate):
    return {e.code: e.points for e in candidate.evidence}


def test_normalization_requires_an_explicit_alias_for_reordered_or_transliterated_names():
    assert normalize_name(" Ｊｉｎ・ＬＩ ") == normalize_name("Jin Li")
    assert normalize_name("林－岚") == "林岚"
    assert normalize_name("Lü") != normalize_name("Lu")
    assert normalize_name("Jin Li") != normalize_name("Li Jin")
    profile = Profile("p", "林岚", aliases=("Lin Lan",))
    model = Matcher([profile])
    match = model.rank(mention(name="ＬＩＮ・ＬＡＮ"), "name")
    assert match.decide(NAME_POLICY).recommended_id == "p"
    assert by_code(match.eligible[0]) == {"name_alias": 40}
    assert model.rank(mention(name="Lan Lin")).decide(WEIGHTED_POLICY).reason == "no_candidate"


def test_tied_name_baseline_abstains_and_ranks_by_stable_id():
    first = Profile("z", "林岚")
    second = Profile("a", "Other", aliases=("林岚",))
    for order in [(first, second), (second, first)]:
        ranking = Matcher(order).rank(mention(), "name")
        assert [c.profile_id for c in ranking.eligible] == ["a", "z"]
        assert ranking.decide(NAME_POLICY).reason == "ambiguous"
        assert ranking.decide(NAME_POLICY).margin == 0


def test_affiliation_uses_half_open_period_and_recognizes_a_known_code():
    profile = Profile("p", "林岚", affiliations=(
        Affiliation("甲院", START, DAY, ("alpha",)), Affiliation("乙院", DAY, aliases=("beta",)),
    ))
    model = Matcher([profile])
    past = model.rank(mention(event_date=date(2025, 1, 8), affiliation="ＡＬＰＨＡ"))
    assert past.eligible[0].score == 70
    assert past.decide(WEIGHTED_POLICY).recommended_id == "p"
    boundary = model.rank(mention(affiliation="甲院"))
    assert by_code(boundary.eligible[0])["affiliation_mismatch"] == -12
    assert boundary.decide(WEIGHTED_POLICY).reason == "below_score"
    assert model.rank(mention(affiliation="乙院")).decide(WEIGHTED_POLICY).recommended_id == "p"


def test_overlap_missing_and_unknown_affiliations_are_neutral_not_mismatches():
    profiles = (Profile("p", "林岚", affiliations=(Affiliation("甲院", START),
                                                      Affiliation("乙院", START))),)
    model = Matcher(profiles)
    assert by_code(model.rank(mention(affiliation="甲院")).eligible[0])["affiliation_overlap"] == 0
    assert by_code(model.rank(mention()).eligible[0])["affiliation_missing"] == 0
    unknown = model.rank(mention(affiliation="甲院", event_date=date(2019, 1, 1)))
    assert by_code(unknown.eligible[0])["affiliation_unknown"] == 0


def test_only_prior_deduplicated_history_scores_and_missing_dates_disable_it():
    profile = Profile("p", "林岚", history=(
        History(START, ("王雁", "陈臻", "孔帆", "林岚"), ("laser",)),
        History(DAY, ("沈之",), ("resonance",)),
        History(date(2026, 1, 1), ("吕莺",), ("future",)),
    ))
    model = Matcher([profile])
    slot = mention(co_contributors=("王雁", "王·雁", "陈臻", "孔帆", "林岚", "沈之", "吕莺"),
                   title="Laser resonance and future")
    evidence = by_code(model.rank(slot).eligible[0])
    assert evidence["prior_collaboration"] == 24
    assert evidence["prior_topic"] == 8
    missing = by_code(model.rank(replace(slot, event_date=None)).eligible[0])
    assert missing == {"name_exact": 40, "event_date_missing": 0, "affiliation_missing": 0}
    same_day_only = model.rank(replace(slot, co_contributors=("林岚", "沈之"), title="Resonance lasers"))
    assert "prior_topic" not in by_code(same_day_only.eligible[0])
    assert "prior_collaboration" not in by_code(same_day_only.eligible[0])


def test_verified_conflict_hard_blocks_even_with_a_higher_raw_score():
    wrong = Profile("wrong", "林岚", trusted_ids=(TrustedIdentifier(" Registry ", "wrong"),),
                    affiliations=(Affiliation("甲院", START),))
    slot = mention(affiliation="甲院", trusted_ids=(TrustedIdentifier("registry", "other"),))
    ranking = Matcher([wrong]).rank(slot)
    assert ranking.candidates[0].score == 70
    assert by_code(ranking.candidates[0])["trusted_id_conflict"] == 0
    assert ranking.eligible == ()
    assert ranking.decide(Policy(-100, 1)).reason == "trusted_conflict"


def test_verified_link_recalls_a_name_mismatch_and_vetoes_the_conflicted_homonym():
    trusted = TrustedIdentifier("registry", "verified")
    first = Profile("first", "Correct", trusted_ids=(trusted,))
    second = Profile("second", "林岚", trusted_ids=(TrustedIdentifier("registry", "second"),),
                     affiliations=(Affiliation("甲院", START),))
    ranker = Matcher([first, second])
    slot = mention(trusted_ids=(trusted,), affiliation="甲院")
    ranking = ranker.rank(slot)
    assert [c.profile_id for c in ranking.candidates] == ["first", "second"]
    assert ranking.candidates[-1].blocked
    assert ranking.decide(WEIGHTED_POLICY).recommended_id == "first"
    assert ranker.rank(slot, "name").decide(NAME_POLICY).recommended_id == "second"


def test_incompatible_or_nonunique_trusted_assertions_force_global_abstention():
    first = TrustedIdentifier("one", "first")
    second = TrustedIdentifier("two", "second")
    past = Affiliation("甲院", START)
    model = Matcher([Profile("p1", "林岚", trusted_ids=(first,), affiliations=(past,)),
                     Profile("p2", "Other", trusted_ids=(second,))])
    assert model.rank(mention(trusted_ids=(first, second), affiliation="甲院")).decide(
        WEIGHTED_POLICY).reason == "trusted_conflict"
    duplicate = Matcher([Profile("p1", "林岚", trusted_ids=(first,), affiliations=(past,)),
                         Profile("p2", "Other", trusted_ids=(first,))])
    assert duplicate.rank(mention(trusted_ids=(first,))).decide(WEIGHTED_POLICY).reason == (
        "trusted_conflict"
    )
    inconsistent = mention(trusted_ids=(first, TrustedIdentifier("one", "other")))
    assert model.rank(inconsistent).decide(WEIGHTED_POLICY).reason == "trusted_conflict"


def test_single_candidate_can_still_abstain_and_score_margin_are_inclusive():
    first = Profile("p1", "林岚", affiliations=(Affiliation("甲院", START),))
    rival = Profile("p2", "林岚")
    assert Matcher([first]).rank(mention()).decide(WEIGHTED_POLICY).reason == "below_score"
    candidates = Matcher([first, rival]).rank(mention(affiliation="甲院"))
    assert candidates.decide(Policy(70, 30)).recommended_id == "p1"
    assert candidates.decide(Policy(71, 30)).reason == "below_score"
    assert candidates.decide(Policy(70, 31)).reason == "ambiguous"
    with pytest.raises(ValueError, match="margin"):
        Policy(40, 0)
