"""Pure matching of one author/inventor mention against a researcher roster."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from unicodedata import normalize

ALGORITHM_VERSION = "weighted-v1"
BASELINE_VERSION = "name-v1"
Kind = Literal["paper", "patent"]
Mode = Literal["name", "weighted"]


def normalize_phrase(value: str) -> str:
    canonical = normalize("NFKC", value).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in canonical).split())


def normalize_name(value: str) -> str:
    # Do not infer transliteration, token reordering, or accent equivalence.
    return normalize_phrase(value).replace(" ", "")


@dataclass(frozen=True, order=True)
class TrustedIdentifier:
    """A verified assertion only, not a raw ID copied from an unverified source."""

    scheme: str
    value: str

    def __post_init__(self):
        object.__setattr__(self, "scheme", self.scheme.strip().casefold())
        object.__setattr__(self, "value", self.value.strip())
        if not self.scheme or not self.value:
            raise ValueError("A trusted identifier requires a nonempty scheme and value")


@dataclass(frozen=True)
class Affiliation:
    name: str
    starts_on: date
    ends_on: date | None = None
    aliases: tuple[str, ...] = ()

    def active_on(self, day: date) -> bool:
        return self.starts_on <= day and (self.ends_on is None or day < self.ends_on)


@dataclass(frozen=True)
class History:
    available_on: date
    collaborators: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    trusted_ids: tuple[TrustedIdentifier, ...] = ()
    affiliations: tuple[Affiliation, ...] = ()
    history: tuple[History, ...] = ()


@dataclass(frozen=True)
class Mention:
    id: str
    work_id: str
    kind: Kind
    position: int
    name: str
    title: str = ""
    event_date: date | None = None
    affiliation: str = ""
    co_contributors: tuple[str, ...] = ()
    trusted_ids: tuple[TrustedIdentifier, ...] = ()


@dataclass(frozen=True)
class Evidence:
    code: str
    points: int
    detail: str


@dataclass(frozen=True)
class Candidate:
    profile_id: str
    score: int
    evidence: tuple[Evidence, ...]
    blocked: bool = False


@dataclass(frozen=True)
class Policy:
    min_score: int
    min_margin: int

    def __post_init__(self):
        if self.min_margin < 1:
            raise ValueError("The margin must be positive; never recommend a tied first place")


NAME_POLICY = Policy(min_score=35, min_margin=1)
# Frozen deployment configuration; the evaluation records the validation grid and its winner.
WEIGHTED_POLICY = Policy(min_score=55, min_margin=20)


@dataclass(frozen=True)
class Decision:
    recommended_id: str | None
    reason: str
    margin: int | None


@dataclass(frozen=True)
class Ranking:
    candidates: tuple[Candidate, ...]
    trusted_conflict: bool = False

    @property
    def eligible(self) -> tuple[Candidate, ...]:
        return tuple(candidate for candidate in self.candidates if not candidate.blocked)

    def decide(self, policy: Policy) -> Decision:
        eligible = self.eligible
        margin = eligible[0].score - eligible[1].score if len(eligible) > 1 else None
        if self.trusted_conflict:
            return Decision(None, "trusted_conflict", margin)
        if not eligible:
            return Decision(None, "no_candidate", margin)
        if eligible[0].score < policy.min_score:
            return Decision(None, "below_score", margin)
        if margin is not None and margin < policy.min_margin:
            return Decision(None, "ambiguous", margin)
        return Decision(eligible[0].profile_id, "recommended", margin)


def _by_scheme(identifiers: tuple[TrustedIdentifier, ...]) -> dict[str, set[str]]:
    result = defaultdict(set)
    for identifier in identifiers:
        result[identifier.scheme].add(identifier.value)
    return dict(result)


def _weighted_evidence(
    mention: Mention, profile: Profile, name_keys: frozenset[str]
) -> tuple[list[Evidence], bool, bool]:
    evidence = []
    observed_ids = _by_scheme(mention.trusted_ids)
    known_ids = _by_scheme(profile.trusted_ids)
    common_schemes = sorted(observed_ids.keys() & known_ids.keys())
    matched = [scheme for scheme in common_schemes if observed_ids[scheme] & known_ids[scheme]]
    conflicts = [scheme for scheme in common_schemes if observed_ids[scheme] != known_ids[scheme]]
    if matched:
        evidence.append(Evidence("trusted_id_match", 120, "可信标识一致：" + "、".join(matched)))
    if conflicts:
        evidence.append(Evidence("trusted_id_conflict", 0, "可信标识冲突：" + "、".join(conflicts)))

    day = mention.event_date
    if day is None:
        evidence.append(Evidence("event_date_missing", 0, "缺少成果日期，不使用历史证据"))
    observed_affiliation = normalize_phrase(mention.affiliation)
    if not observed_affiliation:
        evidence.append(Evidence("affiliation_missing", 0, "来源未提供署名机构"))
    if day is not None and observed_affiliation:
        active = [period for period in profile.affiliations if period.active_on(day)]
        active_names = {
            frozenset(map(normalize_phrase, (period.name, *period.aliases))) - {""}
            for period in active
        } - {frozenset()}
        if not active_names:
            evidence.append(Evidence("affiliation_unknown", 0, "没有成果日期对应的院系记录"))
        elif len(active_names) > 1:
            evidence.append(Evidence("affiliation_overlap", 0, "历史主归属存在冲突，不计院系分"))
        elif any(observed_affiliation in names for names in active_names):
            evidence.append(Evidence("affiliation_match", 30, "成果日期的院系一致：" + mention.affiliation))
        else:
            evidence.append(Evidence("affiliation_mismatch", -12, "来源机构与当时的院系不一致"))

    prior = tuple(item for item in profile.history if day is not None and item.available_on < day)
    historical_names = {normalize_name(name) for item in prior for name in item.collaborators}
    current_names = {normalize_name(name) for name in mention.co_contributors}
    shared = sorted((historical_names & current_names) - name_keys - {""})
    if shared:
        evidence.append(Evidence("prior_collaboration", 12 * min(len(shared), 2),
                                 f"成果发布前的同名合作者线索：{len(shared)} 项"))
    title = f" {normalize_phrase(mention.title)} "
    topics = {normalize_phrase(topic) for item in prior for topic in item.topics} - {""}
    matching_topics = sorted(topic for topic in topics if f" {topic} " in title)
    if matching_topics:
        evidence.append(Evidence("prior_topic", 8 * min(len(matching_topics), 2),
                                 "事前研究主题与题名相符：" + "、".join(matching_topics)))
    return evidence, bool(matched), bool(conflicts)


class Matcher:
    """Index a roster once; retrieve by normalized name, explicit alias, or trusted ID."""

    def __init__(self, profiles: Iterable[Profile]):
        self._profiles = {}
        self._names = {}
        self._name_index = defaultdict(set)
        self._id_index = defaultdict(set)
        for profile in profiles:
            if not profile.id or profile.id in self._profiles:
                raise ValueError(f"Empty or duplicate profile ID: {profile.id!r}")
            self._profiles[profile.id] = profile
            keys = frozenset(map(normalize_name, (profile.name, *profile.aliases))) - {""}
            self._names[profile.id] = keys
            for key in keys:
                self._name_index[key].add(profile.id)
            for trusted_id in profile.trusted_ids:
                self._id_index[trusted_id].add(profile.id)

    def rank(self, mention: Mention, mode: Mode = "weighted") -> Ranking:
        if mode not in {"name", "weighted"}:
            raise ValueError(f"Unknown matching mode: {mode!r}")
        name_key = normalize_name(mention.name)
        ids = set(self._name_index.get(name_key, ()))
        for trusted_id in mention.trusted_ids:
            ids.update(self._id_index.get(trusted_id, ()))

        candidates = []
        trusted_links = []
        for profile_id in sorted(ids):
            profile = self._profiles[profile_id]
            name_keys = self._names[profile_id]
            if name_key and name_key == normalize_name(profile.name):
                evidence = [Evidence("name_exact", 40, "标准化姓名一致")]
            elif name_key in name_keys:
                evidence = [Evidence("name_alias", 35 if mode == "weighted" else 40, "姓名别名一致")]
            else:
                evidence = [Evidence("name_unmatched", 0, "无姓名或别名命中")]
            blocked = False
            if mode == "weighted":
                extra, id_matched, blocked = _weighted_evidence(mention, profile, name_keys)
                evidence.extend(extra)
                if id_matched:
                    trusted_links.append(blocked)
            candidates.append(Candidate(profile_id, sum(e.points for e in evidence),
                                        tuple(evidence), blocked))
        candidates.sort(key=lambda candidate: (candidate.blocked, -candidate.score, candidate.profile_id))
        incompatible_assertions = (
            mode == "weighted"
            and any(len(values) > 1 for values in _by_scheme(mention.trusted_ids).values())
        )
        # An incompatible assertion or non-unique trusted link cannot be rescued by soft evidence.
        trusted_conflict = (
            incompatible_assertions or len(trusted_links) > 1 or any(trusted_links)
            or (bool(candidates) and all(candidate.blocked for candidate in candidates))
        )
        return Ranking(tuple(candidates), trusted_conflict)
