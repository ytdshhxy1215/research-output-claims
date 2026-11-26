"""Read-only Django adapter for authenticated, caller-scoped claim suggestions."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, cast

from .matcher import (
    ALGORITHM_VERSION,
    WEIGHTED_POLICY,
    Affiliation,
    Evidence,
    Kind,
    Matcher,
    Mention,
    Profile,
    normalize_name,
)

if TYPE_CHECKING:
    from apps.catalog.models import ContributionSlot, Work
    from apps.people.models import Researcher


@dataclass(frozen=True)
class Recommendation:
    work: "Work"
    slot: "ContributionSlot"
    score: int
    margin: int | None
    evidence: tuple[Evidence, ...]
    algorithm_version: str = ALGORITHM_VERSION

    @property
    def work_id(self) -> int:
        return self.slot.work_id

    @property
    def slot_id(self) -> int:
        return self.slot.pk

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(item.detail for item in self.evidence)


def _event_date(work: "Work") -> date | None:
    if work.kind == "paper":
        return work.published_on
    return min((day for day in (work.published_on, work.granted_on) if day is not None), default=None)


def for_researcher(researcher: "Researcher") -> list[Recommendation]:
    """Return accepted slots for this saved researcher only; caller must enforce access.

    Current catalog/people schemas supply name and affiliation only. Neither source IDs,
    catalogue identifiers nor other claims are treated as verified person evidence.
    """
    from apps.catalog.models import ContributionSlot
    from apps.people.models import AffiliationPeriod, Researcher

    if researcher.pk is None or researcher._state.adding:
        return []
    target_name = normalize_name(researcher.name)
    if not target_name:
        return []
    db = researcher._state.db
    # Compare with the whole normalized homonym block, not just the caller. Use
    # the same Python normalization on both sides regardless of database collation.
    homonyms = [item for item in Researcher.objects.using(db).only("name").iterator()
                if normalize_name(item.name) == target_name]
    if all(item.pk != researcher.pk for item in homonyms):
        return []
    histories = defaultdict(list)
    periods = (AffiliationPeriod.objects.using(db)
               .filter(researcher_id__in=[item.pk for item in homonyms], is_primary=True)
               .select_related("department").order_by("researcher_id", "starts_on", "pk"))
    for period in periods:
        histories[period.researcher_id].append(Affiliation(
            period.department.name, period.starts_on, period.ends_on, (period.department.code,),
        ))
    matcher = Matcher(Profile(str(item.pk), item.name, affiliations=tuple(histories[item.pk]))
                      for item in homonyms)
    target_id = str(researcher.pk)
    recommendations = []
    slots = (ContributionSlot.objects.using(db).filter(work__kind__in=("paper", "patent"))
             .select_related("work").order_by("pk").iterator())
    for slot in slots:
        if normalize_name(slot.name) != target_name:
            continue
        work = slot.work
        mention = Mention(str(slot.pk), str(work.pk), cast(Kind, work.kind), slot.position,
                          slot.name, work.title, _event_date(work), slot.affiliation or "")
        ranking = matcher.rank(mention)
        decision = ranking.decide(WEIGHTED_POLICY)
        if decision.recommended_id == target_id:
            winner = ranking.eligible[0]
            recommendations.append(Recommendation(work, slot, winner.score, decision.margin,
                                                  winner.evidence))
    return sorted(recommendations, key=lambda rec: (-rec.score, rec.work_id, rec.slot.position,
                                                   rec.slot_id))
