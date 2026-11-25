"""Version 1 of the deterministic, entirely synthetic fixture dataset."""

RELEASES = {
    "batch-1": "2025-02-02T00:00:00Z",
    "batch-2": "2025-02-03T00:00:00Z",
}
STREAMS = ("works", "patents")


def event(event_id, external_id, updated_at, record):
    return {
        "event_id": event_id,
        "external_id": external_id,
        "updated_at": updated_at,
        "record": record,
    }


def slot(slot_id, name, affiliation):
    return {"slot_id": slot_id, "name": name, "affiliation": affiliation}


PAPER = {
    "doi": "https://doi.org/10.5555/P1",
    "title": "合成论文：增量归集与证据追溯",
    "published_on": "2025-01-09",
    "authors": [slot("p1-a1", "林岚", "alpha"), slot("p1-a2", "陈星", "乙院")],
}
UPDATED_PAPER = {
    **PAPER,
    "authors": [{**PAPER["authors"][0], "affiliation": "甲院"}, PAPER["authors"][1]],
}
PATENT = {
    "jurisdiction": "CN",
    "application_number": "FIXTURE-2025-0001",
    "title": "合成专利：一种示例归集方法",
    "document_number": "CNFIXTURE0001A",
    "document_kind": "A",
    "published_on": "2025-01-16",
    "inventors": [slot("pat1-i1", "陈星", "乙院")],
}
GRANT = {
    **PATENT,
    "document_number": "CNFIXTURE0001B",
    "document_kind": "B",
    "granted_on": "2025-01-30",
}

EVENTS = {
    "works": (
        event("paper-01", "paper-1", "2025-02-01T12:00:00Z", PAPER),
        # Exactly on batch-1's exclusive upper bound, with a stable tie-breaker.
        event("paper-02-update", "paper-1", RELEASES["batch-1"], UPDATED_PAPER),
        event("paper-03-duplicate", "paper-1", RELEASES["batch-1"], UPDATED_PAPER),
        event(
            "paper-04-new",
            "paper-2",
            RELEASES["batch-1"],
            {
                "doi": "10.5555/p2",
                "title": "合成论文：可重放数据管道",
                "published_on": "2025-01-23",
                "authors": [slot("p2-a1", "周宁", "甲院")],
            },
        ),
    ),
    "patents": (
        event("patent-01-publication", "patent-1-a", "2025-02-01T12:00:00Z", PATENT),
        event("patent-02-grant", "patent-1-b", RELEASES["batch-1"], GRANT),
    ),
}
