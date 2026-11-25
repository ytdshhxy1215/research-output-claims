"""Run the frozen offline protocol: ``python -m apps.matching.evaluate``."""

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from .dataset import (
    DATASET_VERSION,
    SEED,
    Dataset,
    Label,
    assert_isolated,
    challenge_cases,
    synthetic_split,
)
from .matcher import (
    ALGORITHM_VERSION,
    BASELINE_VERSION,
    NAME_POLICY,
    WEIGHTED_POLICY,
    Decision,
    Matcher,
    Mention,
    Mode,
    Policy,
    Ranking,
)

PROTOCOL_VERSION = "offline-v1"
PRECISION_FLOOR = Fraction(9, 10)
SCORE_GRID = (40, 55, 60, 65, 70, 75, 80, 90, 100, 120, 150)
MARGIN_GRID = (1, 10, 15, 20, 30)
ABSTAIN_POLICY = Policy(1000, 1)  # Outside the v1 score range.
KINDS = ("all", "paper", "patent")


@dataclass(frozen=True)
class RankedRow:
    mention: Mention
    label: Label
    ranking: Ranking


def _rank(dataset: Dataset, mode: Mode) -> tuple[RankedRow, ...]:
    matcher = Matcher(dataset.profiles)
    labels = {label.mention_id: label for label in dataset.labels}
    return tuple(RankedRow(mention, labels[mention.id], matcher.rank(mention, mode))
                 for mention in dataset.mentions)


def _divide(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _measure(rows: tuple[RankedRow, ...], policy: Policy, k: int) -> dict:
    counts = Counter()
    reasons = Counter()
    for row in rows:
        decision = row.ranking.decide(policy)
        predicted = decision.recommended_id
        expected = row.label.profile_id
        reasons[decision.reason] += 1
        if predicted is not None:
            counts["accepted"] += 1
            counts["tp" if predicted == expected else "fp"] += 1
        if expected is not None:
            counts["positives"] += 1
            if expected != predicted:
                counts["fn"] += 1
            ranked_ids = tuple(candidate.profile_id for candidate in row.ranking.eligible[:k])
            counts["top1_hits"] += expected in ranked_ids[:1]
            counts["recall_at_k_hits"] += expected in ranked_ids
    tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
    return {
        "mentions": len(rows),
        "positives": counts["positives"],
        "accepted": counts["accepted"],
        "abstained": len(rows) - counts["accepted"],
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": _divide(tp, tp + fp),
        "recall": _divide(tp, tp + fn),
        "f1": _divide(2 * tp, 2 * tp + fp + fn),
        "top1_hits": counts["top1_hits"],
        "top1": _divide(counts["top1_hits"], counts["positives"]),
        "k": k,
        "recall_at_k_hits": counts["recall_at_k_hits"],
        "recall_at_k": _divide(counts["recall_at_k_hits"], counts["positives"]),
        "coverage": _divide(counts["accepted"], len(rows)),
        "abstention_rate": _divide(len(rows) - counts["accepted"], len(rows)),
        "decision_reasons": dict(sorted(reasons.items())),
    }


def _precision_key(metric: dict) -> Fraction:
    return Fraction(metric["tp"], metric["accepted"] or 1)


def _selection_key(policy: Policy, metric: dict) -> tuple[Fraction, Fraction, int, int]:
    f1_denominator = 2 * metric["tp"] + metric["fp"] + metric["fn"]
    f1 = Fraction(2 * metric["tp"], f1_denominator or 1)
    return f1, _precision_key(metric), policy.min_margin, policy.min_score


def select_threshold(validation: Dataset, k: int = 3) -> tuple[Policy, list[dict]]:
    """Select only on validation: highest F1 subject to precision >= 90%."""
    if validation.name != "validation":
        raise ValueError("Threshold selection requires the validation split")
    if k < 1:
        raise ValueError("k must be positive")
    rows = _rank(validation, "weighted")
    policies = (Policy(score, margin) for score in SCORE_GRID for margin in MARGIN_GRID)
    trials = [(policy, _measure(rows, policy, k)) for policy in policies]
    qualified = [(policy, metric) for policy, metric in trials
                 if metric["accepted"] and _precision_key(metric) >= PRECISION_FLOOR]
    if qualified:
        winner, _ = max(qualified, key=lambda item: _selection_key(*item))
    else:
        winner = ABSTAIN_POLICY
    return winner, [{"policy": asdict(policy), "metrics": metric} for policy, metric in trials]


def _error(row: RankedRow, decision: Decision) -> dict:
    expected, predicted = row.label.profile_id, decision.recommended_id
    error = ("missed_link" if predicted is None else
             "false_link" if expected is None else "wrong_identity")
    return {
        "error": error,
        "mention_id": row.mention.id,
        "work_id": row.mention.work_id,
        "kind": row.mention.kind,
        "position": row.mention.position,
        "name": row.mention.name,
        "title": row.mention.title,
        "scenario": row.label.scenario,
        "expected_id": expected,
        "decision": asdict(decision),
        "ranked_candidates": [asdict(candidate) for candidate in row.ranking.candidates],
    }


def evaluate_dataset(dataset: Dataset, mode: Mode, policy: Policy, k: int = 3) -> dict:
    if k < 1:
        raise ValueError("k must be positive")
    rows = _rank(dataset, mode)
    errors = []
    for row in rows:
        decision = row.ranking.decide(policy)
        if decision.recommended_id != row.label.profile_id:
            errors.append(_error(row, decision))
    return {
        "metrics": {
            kind: _measure(tuple(row for row in rows if kind == "all" or row.mention.kind == kind),
                           policy, k)
            for kind in KINDS
        },
        "errors": errors,
    }


def build_report(k: int = 3) -> dict:
    validation = synthetic_split("validation")
    # Fit before loading test/challenge labels. Only this frozen config is subsequently scored.
    chosen, trials = select_threshold(validation, k)
    if chosen != WEIGHTED_POLICY:
        raise ValueError(f"Frozen weighted-v1 policy {WEIGHTED_POLICY} differs from validation {chosen}")
    datasets = (validation, synthetic_split("test"), challenge_cases())
    assert_isolated(*datasets)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "dataset_version": DATASET_VERSION,
        "seed": SEED,
        "k": k,
        "models": {
            "name": {"version": BASELINE_VERSION, "policy": asdict(NAME_POLICY)},
            "weighted": {"version": ALGORITHM_VERSION, "policy": asdict(chosen)},
        },
        "selection": {
            "split": "validation",
            "objective": "max F1 with nonzero recommendations and precision >= 0.90; "
                         "ties: higher precision, margin, score; if none qualify, abstain",
            "trials": trials,
        },
        "splits": {
            dataset.name: {
                "sha256": dataset.digest(),
                "profiles": len(dataset.profiles),
                "name_groups": len({profile.name for profile in dataset.profiles}),
                "name": evaluate_dataset(dataset, "name", NAME_POLICY, k),
                "weighted": evaluate_dataset(dataset, "weighted", chosen, k),
            }
            for dataset in datasets
        },
    }


def _print_summary(report: dict) -> None:
    print(f"{report['protocol_version']} / {report['dataset_version']} (seed {report['seed']})")
    print(f"Validation-selected weighted policy: {report['models']['weighted']['policy']}")
    print(f"{'split':10} {'model':8} {'kind':7} {'n':>4} {'pos':>4} {'P':>7} {'R':>7} "
          f"{'F1':>7} {'top-1':>7} {'R@' + str(report['k']):>7} {'cover':>7} {'errors':>6}")
    for split, data in report["splits"].items():
        for mode in ("name", "weighted"):
            error_counts = Counter(row["kind"] for row in data[mode]["errors"])
            for kind, metric in data[mode]["metrics"].items():
                errors = sum(error_counts.values()) if kind == "all" else error_counts[kind]
                values = " ".join(f"{metric[name]:7.3f}" for name in
                                  ("precision", "recall", "f1", "top1", "recall_at_k", "coverage"))
                print(f"{split:10} {mode:8} {kind:7} {metric['mentions']:4} "
                      f"{metric['positives']:4} {values} {errors:6}")
    print("Per-mention errors, evidence, hashes and the validation grid: --json or --output PATH.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=3, help="candidate recall cutoff (default 3)")
    parser.add_argument("--json", action="store_true", help="print the complete JSON report")
    parser.add_argument("--output", type=Path, help="write the complete UTF-8 JSON report to this path")
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be positive")
    report = build_report(args.k)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_summary(report)


if __name__ == "__main__":
    main()
