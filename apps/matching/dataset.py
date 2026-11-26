"""Frozen, entirely fictional offline data. Truth is not passed to the matcher."""

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
from itertools import combinations, product
from json import dumps
from random import Random
from typing import Literal

from .matcher import (
    Affiliation,
    History,
    Kind,
    Mention,
    Profile,
    TrustedIdentifier,
    normalize_name,
    normalize_phrase,
)

DATASET_VERSION = "synthetic-v1"
SEED = 240711
SyntheticSplit = Literal["validation", "test"]
GROUP_COUNTS = {"validation": 16, "test": 32}
MENTIONS_PER_GROUP = 8
MOVE_DAY = date(2023, 7, 1)
DEPARTMENTS = ("材料系", "力学系", "化工系", "生医系", "自动化系", "声学系")
COLLABORATORS = ("姚墨", "薛月", "周玄", "燕雨", "童鹭", "雍莺", "闻松", "纪荷", "严冰", "宋雁")
TOPICS = ("porous media", "signal fusion", "optical films", "soft robotics", "flow control",
          "sensor calibration", "surface coating", "thermal imaging", "model reduction")
SURNAMES = (("周", "Zhou"), ("朱", "Zhu"), ("陆", "Lu"), ("林", "Lin"), ("何", "He"),
            ("许", "Xu"), ("唐", "Tang"), ("孙", "Sun"), ("马", "Ma"))
GIVEN_NAMES = (("冉", "Ran"), ("晨", "Chen"), ("逸", "Yi"), ("岚", "Lan"), ("玥", "Yue"),
               ("晗", "Han"), ("霖", "Lin"), ("瑛", "Ying"), ("涛", "Tao"))


@dataclass(frozen=True)
class Label:
    mention_id: str
    profile_id: str | None  # None = no in-roster identity, even if evidence is misleading.
    scenario: str


@dataclass(frozen=True)
class Dataset:
    name: str
    profiles: tuple[Profile, ...]
    mentions: tuple[Mention, ...]
    labels: tuple[Label, ...]

    def __post_init__(self):
        profile_ids = {profile.id for profile in self.profiles}
        mention_ids = {mention.id for mention in self.mentions}
        slots = {(mention.work_id, mention.position) for mention in self.mentions}
        if (len(profile_ids) != len(self.profiles) or len(mention_ids) != len(self.mentions)
                or len(slots) != len(self.mentions) or len(self.labels) != len(self.mentions)
                or {label.mention_id for label in self.labels} != mention_ids
                or any(label.profile_id is not None and label.profile_id not in profile_ids
                       for label in self.labels)):
            raise ValueError(f"Invalid profile, slot or label keys in {self.name}")

    def digest(self) -> str:
        frozen_json = dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                            default=date.isoformat)
        return sha256(frozen_json.encode("utf-8")).hexdigest()


def _key(rng: Random) -> str:
    return f"{rng.getrandbits(96):024x}"


def _profile(rng: Random, ids: Random, name: str, roman_name: str, index: int) -> Profile:
    past, present = rng.sample(DEPARTMENTS, 2)
    start = date(2018, 1, 1)
    periods = ((Affiliation(past, start, MOVE_DAY), Affiliation(present, MOVE_DAY))
               if rng.random() < 0.52 else (Affiliation(past, start),))
    history = (
        History(date(2020, 6, 1), tuple(rng.sample(COLLABORATORS, 3)), tuple(rng.sample(TOPICS, 2))),
        History(date(2024, 5, 2), tuple(rng.sample(COLLABORATORS, 2)), (rng.choice(TOPICS),)),
    )
    trusted = (TrustedIdentifier("synthetic", _key(ids)),) if rng.random() < 0.85 else ()
    return Profile(_key(ids), name, (roman_name, f"{('E', 'Q')[index]}. {roman_name}"),
                   trusted, periods, history)


def _observed_mention(
    rng: Random, ids: Random, profile: Profile, is_in_roster: bool, kind: Kind,
    outside_case: str, siblings: tuple[Profile, ...],
) -> tuple[Mention, str]:
    day = date(rng.randrange(2021, 2026), rng.randrange(1, 13), rng.randrange(1, 29))
    draw = rng.random()
    if draw < 0.10:
        name, variant = profile.name + "·" + rng.choice("紊勘巽"), "typo"
    elif draw < 0.31:
        name, variant = rng.choice(profile.aliases), "alias"
    elif draw < 0.45:
        name, variant = "　".join(profile.name), "format"
    else:
        name, variant = profile.name, "canonical"
    other_departments = (*DEPARTMENTS, "外地研究所", "联合实验室")
    affiliation_draw = rng.random()
    if affiliation_draw < 0.32:
        affiliation = ""
    elif affiliation_draw < 0.76:
        affiliation = next(period.name for period in profile.affiliations if period.active_on(day))
        if not is_in_roster and rng.random() < 0.60:
            affiliation = "外地研究所"
    else:
        affiliation = rng.choice(other_departments)

    # Independent observation noise, including (rare) verified but wrong assertions.
    id_draw = rng.random()
    observed_id = ()
    if id_draw < 0.07:
        alternatives = tuple(identifier for sibling in siblings if sibling.id != profile.id
                             for identifier in sibling.trusted_ids)
        observed_id = ((rng.choice(alternatives),) if alternatives and rng.random() < 0.40
                       else (TrustedIdentifier("synthetic", _key(ids)),))
    elif id_draw < 0.27:
        observed_id = profile.trusted_ids

    prior = tuple(item for item in profile.history if item.available_on < day)
    coworkers = rng.sample(COLLABORATORS, rng.randrange(0, 3))
    if rng.random() < 0.46:
        coworkers += [rng.choice(rng.choice(prior).collaborators)]
    topics = rng.sample(TOPICS, rng.choice((1, 1, 2)))
    if rng.random() < 0.44:
        topics[0] = rng.choice(rng.choice(prior).topics)
    facet = rng.choice(("dispersion", "response", "stability", "drift", "resonance", "transfer"))
    setting = rng.choice(("humid", "cyclic", "acoustic", "shear", "low light", "vacuum", "marine"))
    variant_number = rng.randrange(100_000)
    title = f"{facet} under {setting} for {' and '.join(dict.fromkeys(topics))} ({variant_number:05d})"
    date_seen = day if rng.random() > 0.10 else None
    # The opaque keys and the title number are not derived from an identity or label.
    mention = Mention(_key(ids), _key(ids), kind, rng.randint(1, 4), name, title,
                      date_seen, affiliation, tuple(dict.fromkeys(coworkers)), observed_id)
    return mention, ("roster" if is_in_roster else f"outside_{outside_case}") + ":" + variant


def synthetic_split(name: SyntheticSplit) -> Dataset:
    if name not in GROUP_COUNTS:
        raise ValueError(f"Not a synthetic split: {name!r}")
    universe = list(product(SURNAMES, GIVEN_NAMES))
    Random(SEED).shuffle(universe)
    start = 0 if name == "validation" else GROUP_COUNTS["validation"]
    salt = 100 * (1 if name == "validation" else 2)
    roster_rng, truth_rng, observe_rng, ids = (Random(SEED + salt + n) for n in range(4))
    profiles, mentions, labels = [], [], []
    for (surname, latin_surname), (given, latin_given) in universe[start:start + GROUP_COUNTS[name]]:
        canonical, roman = surname + given, f"{latin_surname} {latin_given}"
        group_size = 2 if roster_rng.random() < 0.72 else 1
        group = tuple(_profile(roster_rng, ids, canonical, roman, i) for i in range(group_size))
        profiles.extend(group)
        for index in range(MENTIONS_PER_GROUP):
            # Hidden identity is chosen before observing metadata, with a genuine outside population.
            in_roster = truth_rng.random() < 0.76
            if in_roster:
                subject, outside_case = truth_rng.choice(group), ""
            else:
                outside_case = "homonym" if truth_rng.random() < 0.57 else "distinct"
                external_name = canonical if outside_case == "homonym" else "邬" + canonical
                external_roman = roman if outside_case == "homonym" else "Wu " + roman
                subject = _profile(roster_rng, ids, external_name, external_roman, 0)
            kind: Kind = "paper" if index % 2 == 0 else "patent"
            mention, scenario = _observed_mention(observe_rng, ids, subject, in_roster, kind,
                                                  outside_case, group)
            mentions.append(mention)
            labels.append(Label(mention.id, subject.id if in_roster else None, scenario))
    return Dataset(name, tuple(profiles), tuple(mentions), tuple(labels))


def challenge_cases() -> Dataset:
    """Independent hand-authored cases; no sampling from the synthetic generator."""
    moved = Profile("ch-celadon", "顾青", ("Gu Qing",), (TrustedIdentifier("lab", "celadon"),), (
        Affiliation("旧院", date(2019, 1, 1), date(2023, 7, 1)),
        Affiliation("新院", date(2023, 7, 1)),
    ), (History(date(2020, 2, 1), ("顾青", "苏可", "艾雯"), ("microfluidics",)),))
    homonym = Profile("ch-shale", "顾青", ("Q. Gu",), (TrustedIdentifier("lab", "shale"),), (
        Affiliation("新院", date(2019, 1, 1)),
    ), (History(date(2023, 7, 1), ("白砚", "薛未"), ("spectroscopy", "brine")),))
    alias = Profile("ch-jade", "陶白", ("Bai Tao",), (TrustedIdentifier("lab", "jade"),), (
        Affiliation("陶瓷所", date(2019, 1, 1)),
    ), (History(date(2021, 6, 1), ("卢默", "费墨"), ("ceramic glaze",)),))
    other = Profile("ch-amber", "晁雁", (), (TrustedIdentifier("registry", "amber"),), (
        Affiliation("农工所", date(2019, 1, 1)),
    ))
    before, boundary, later = date(2023, 6, 30), date(2023, 7, 1), date(2024, 1, 17)
    # Work `c-pair` has two evaluated inventor slots, which must be linked separately.
    rows = (
        (Mention("c01", "c-before", "paper", 1, "顾青", "Droplet routing", before, "旧院"),
         moved.id, "pre-transfer affiliation"),
        (Mention("c02", "c-boundary", "patent", 2, "顾青", "Brine spectroscopy", boundary,
                 "新院", ("白砚", "薛未")), homonym.id, "shared unit, same-day history unavailable"),
        (Mention("c03", "c-after", "paper", 3, "顾青", "Microfluidics flow", later,
                 "新院", ("苏可", "艾雯")), moved.id, "shared unit, informative prior history"),
        (Mention("c04", "c-alias", "paper", 1, "Ｂａｉ·Ｔａｏ", "Ceramic glaze", later,
                 "陶瓷所", ("卢默", "费墨")), alias.id, "explicit romanized alias"),
        (Mention("c05", "c-pair", "patent", 1, "陶白", "Glaze deposition", later, "陶瓷所"),
         alias.id, "first inventor in multi-slot patent"),
        (Mention("c06", "c-pair", "patent", 2, "常川", "Glaze deposition", later, "外单位"),
         None, "external second inventor on same work"),
        (Mention("c07", "c-rescue", "patent", 2, "陶陌", "Kiln", later,
                 trusted_ids=alias.trusted_ids), alias.id, "verified ID recovers misspelling"),
        (Mention("c08", "c-conflict", "paper", 1, "陶白", "Ceramic glaze", later, "陶瓷所",
                 ("卢默", "费墨"), (TrustedIdentifier("lab", "different"),)),
         alias.id, "verified conflict vetoes strong soft match"),
        (Mention("c09", "c-two-ids", "patent", 1, "顾青", "Microfluidics", later, "新院",
                 trusted_ids=(*moved.trusted_ids, *other.trusted_ids)),
         moved.id, "trusted assertions point at different profiles"),
        (Mention("c10", "c-no-date", "paper", 2, "晁雁", "Seeder", affiliation="农工所"),
         other.id, "missing date cannot prove current affiliation"),
        (Mention("c11", "c-outsider", "patent", 1, "顾青", "Coupler", later),
         None, "external homonym without metadata"),
        (Mention("c12", "c-impostor", "paper", 1, "晁雁", "Seeder housing", later, "农工所"),
         None, "external lookalike with misleading affiliation (intentional failure probe)"),
    )
    return Dataset("challenge", (moved, homonym, alias, other), tuple(row[0] for row in rows),
                   tuple(Label(m.id, target, scenario) for m, target, scenario in rows))


def assert_isolated(*datasets: Dataset) -> None:
    """Fail rather than silently letting a shared identity, name block or work cross splits."""
    def keys(dataset: Dataset) -> dict[str, set]:
        return {
            "identity": {p.id for p in dataset.profiles},
            "normalized name/alias": ({normalize_name(name) for p in dataset.profiles
                                       for name in (p.name, *p.aliases)}
                                      | {normalize_name(m.name) for m in dataset.mentions}) - {""},
            "verified identifier": {i for item in (*dataset.profiles, *dataset.mentions)
                                    for i in item.trusted_ids},
            "mention": {m.id for m in dataset.mentions},
            "work": {m.work_id for m in dataset.mentions},
            "normalized title": {normalize_phrase(m.title) for m in dataset.mentions} - {""},
        }

    keyed = [(dataset.name, keys(dataset)) for dataset in datasets]
    for (left_name, left), (right_name, right) in combinations(keyed, 2):
        for key, values in left.items():
            if values & right[key]:
                raise ValueError(f"Shared {key} across {left_name}/{right_name}")
