import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from apps.matching.dataset import Dataset, Label, assert_isolated, challenge_cases, synthetic_split
from apps.matching.evaluate import (
    MARGIN_GRID,
    SCORE_GRID,
    build_report,
    evaluate_dataset,
    select_threshold,
)
from apps.matching.matcher import NAME_POLICY, WEIGHTED_POLICY, Matcher, Mention, Profile


def test_frozen_splits_have_disjoint_profiles_names_and_works():
    validation, test, challenge = synthetic_split("validation"), synthetic_split("test"), challenge_cases()
    assert_isolated(validation, test, challenge)
    assert len(validation.mentions) == 128
    assert len(test.mentions) == 256
    assert [sum(m.kind == kind for m in test.mentions) for kind in ("paper", "patent")] == [128, 128]
    assert len(challenge.mentions) == 12
    assert any(label.profile_id is None for label in test.labels)
    assert len({m.work_id for m in challenge.mentions}) < len(challenge.mentions)
    assert [split.digest() for split in (validation, test, challenge)] == [
        "66eb0180e5e07f6c3fd208a85569243915c46a5eaa819b2a6f00e302256b8966",
        "2b14295c96037d1db687a4348b4960c73eb0145da8ab98dd50d3ae8d4e306f48",
        "f496e889b5133b32ebd228f9fa70fa293a7aaa2613e6a414d3ef7400d737e03e",
    ]


def test_split_audit_rejects_normalized_alias_and_work_leakage():
    val = Dataset("validation", (Profile("p1", "First", (" X-Y ",)),),
                  (Mention("m1", "w", "paper", 1, "First"),), (Label("m1", "p1", "fixture"),))
    test = Dataset("test", (Profile("p2", "Second"),),
                   (Mention("m2", "w2", "patent", 1, "ＸＹ"),), (Label("m2", "p2", "fixture"),))
    with pytest.raises(ValueError, match="normalized name/alias"):
        assert_isolated(val, test)
    with pytest.raises(ValueError, match="Shared work"):
        assert_isolated(val, replace(test, mentions=(replace(test.mentions[0], name="Second",
                                                             work_id="w"),)))


def test_threshold_selection_refuses_test_and_challenge_and_freezes_the_validation_winner():
    validation = synthetic_split("validation")
    winner, trials = select_threshold(validation)
    assert len(trials) == len(SCORE_GRID) * len(MARGIN_GRID) == 55
    assert winner == WEIGHTED_POLICY
    assert select_threshold(validation, k=1)[0] == winner
    for dataset in (synthetic_split("test"), challenge_cases()):
        with pytest.raises(ValueError, match="validation split"):
            select_threshold(dataset)


def test_metrics_count_wrong_identity_as_both_fp_and_fn_and_abstention_as_fn():
    profiles = (Profile("p1", "Liu"), Profile("p2", "Tan"))
    mentions = (
        Mention("m1", "w1", "paper", 1, "Liu"),        # TP
        Mention("m2", "w2", "paper", 1, "Tan"),        # Wrong; one FP and one FN
        Mention("m3", "w3", "paper", 1, "No match"),   # FN
        Mention("m4", "w4", "patent", 1, "Liu"),       # Outside label; FP
        Mention("m5", "w5", "patent", 1, "No match"),  # Correct abstention
    )
    labels = tuple(Label(m.id, p, "fixture") for m, p in zip(mentions, ("p1", "p1", "p2", None, None)))
    report = evaluate_dataset(Dataset("test", profiles, mentions, labels), "name", NAME_POLICY, k=1)
    metric = report["metrics"]["all"]
    assert (metric["tp"], metric["fp"], metric["fn"]) == (1, 2, 2)
    assert (metric["precision"], metric["recall"], metric["f1"]) == (1 / 3, 1 / 3, 1 / 3)
    assert metric["top1"] == metric["recall_at_k"] == 1 / 3
    assert metric["coverage"] == 3 / 5
    assert report["metrics"]["patent"]["top1"] == 0
    assert [row["error"] for row in report["errors"]] == ["wrong_identity", "missed_link", "false_link"]
    assert report["errors"][0]["ranked_candidates"][0]["profile_id"] == "p2"


def test_independent_challenges_assert_decisions_and_expose_intentional_failure():
    challenge = challenge_cases()
    ranker = Matcher(challenge.profiles)
    decisions = {m.id: ranker.rank(m).decide(WEIGHTED_POLICY) for m in challenge.mentions}
    assert decisions["c01"].recommended_id == decisions["c03"].recommended_id == "ch-celadon"
    assert decisions["c02"].reason == "ambiguous"
    assert decisions["c04"].recommended_id == decisions["c05"].recommended_id == "ch-jade"
    assert decisions["c06"].reason == "no_candidate"
    assert decisions["c07"].recommended_id == "ch-jade"
    assert decisions["c08"].reason == decisions["c09"].reason == "trusted_conflict"
    assert decisions["c10"].reason == decisions["c11"].reason == "below_score"
    errors = {row["mention_id"]: row for row in build_report()["splits"]["challenge"]["weighted"]["errors"]}
    assert errors["c12"]["error"] == "false_link"


def test_cli_report_is_byte_identical_across_hash_seeds(tmp_path):
    outputs = [tmp_path / f"{seed}.json" for seed in (1, 2)]
    for seed, path in enumerate(outputs, 1):
        result = subprocess.run([sys.executable, "-m", "apps.matching.evaluate", "--output", str(path)],
                                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                                capture_output=True, text=True, check=True)
        assert "Validation-selected weighted policy" in result.stdout
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    report = json.loads(outputs[0].read_bytes())
    assert report["selection"]["split"] == "validation"
    assert set(report["splits"]["test"]["weighted"]["metrics"]) == {"all", "paper", "patent"}
    assert report["splits"]["test"]["weighted"]["metrics"]["all"]["mentions"] == 256
