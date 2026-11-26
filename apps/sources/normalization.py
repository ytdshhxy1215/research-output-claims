"""Versioned synthetic feed -> catalog facts, without touching attribution."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime

from apps.catalog.models import ContributionSlot, Work

from .connector import IngestionError, utc_timestamp
from .models import SourceRecord, SourceRecordRevision, SourceScope

DOI_PREFIX = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
DOI = re.compile(r"10\.[0-9]{4,9}/\S+", re.ASCII)
APPLICATION = re.compile(r"[A-Z0-9][A-Z0-9./-]*", re.ASCII)


def text(data, name, max_length, *, optional=False):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if len(value) > max_length or (not optional and not value):
        raise ValueError(f"invalid {name}")
    return value


def exact_key(data, name):
    value = text(data, name, 128)
    if value != data[name]:
        raise ValueError(f"invalid leading/trailing whitespace in {name}")
    return value


def record_date(payload, name):
    value = text(payload, name, 10, optional=True)
    if not value:
        return None
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError(f"invalid {name}")
    return result


@dataclass(frozen=True)
class Event:
    external_id: str
    event_id: str
    updated_at: datetime
    kind: str
    identifier: str
    title: str
    published_on: date | None
    granted_on: date | None
    slots: list[tuple[str, str, str]]
    payload: dict
    raw: dict
    content_hash: str

    @property
    def order_key(self):
        return self.updated_at, self.event_id


def parse_event(raw, stream):
    try:
        if not isinstance(raw, dict) or not isinstance(payload := raw.get("record"), dict):
            raise ValueError("expected an event with a record object")
        external_id, event_id = exact_key(raw, "external_id"), exact_key(raw, "event_id")
        updated_at = utc_timestamp(raw.get("updated_at"))
        title = text(payload, "title", 500)
        published_on = record_date(payload, "published_on")
        granted_on = None
        if stream == SourceScope.Stream.WORKS:
            kind, role = Work.Kind.PAPER, "authors"
            identifier = DOI_PREFIX.sub("", text(payload, "doi", 255)).lower()
            if not DOI.fullmatch(identifier):
                raise ValueError("invalid DOI")
        else:
            kind, role = Work.Kind.PATENT, "inventors"
            jurisdiction = text(payload, "jurisdiction", 2).upper()
            application = text(payload, "application_number", 250).upper()
            if not re.fullmatch("[A-Z]{2}", jurisdiction) or not APPLICATION.fullmatch(application):
                raise ValueError("invalid patent application identity")
            identifier = f"{jurisdiction}:{application}"
            document_kind = text(payload, "document_kind", 8)
            text(payload, "document_number", 128)
            granted_on = record_date(payload, "granted_on")
            if document_kind not in ("A", "B") or (document_kind == "B") != bool(granted_on):
                raise ValueError("invalid publication or grant event")
        contributions = payload.get(role)
        if not isinstance(contributions, list) or not contributions:
            raise ValueError(f"expected a nonempty {role} list")
        slots = []
        ids = set()
        for contribution in contributions:
            if not isinstance(contribution, dict):
                raise ValueError(f"invalid {role} slot")
            slot_id = exact_key(contribution, "slot_id")
            if slot_id in ids:
                raise ValueError(f"duplicate source slot ID: {slot_id}")
            ids.add(slot_id)
            slots.append(
                (
                    slot_id,
                    text(contribution, "name", 255),
                    text(contribution, "affiliation", 500, optional=True),
                )
            )
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return Event(
            external_id,
            event_id,
            updated_at,
            kind,
            identifier,
            title,
            published_on,
            granted_on,
            slots,
            payload,
            raw,
            hashlib.sha256(encoded.encode()).hexdigest(),
        )
    except (TypeError, ValueError) as exc:
        raise IngestionError("invalid_record", str(exc), payload=raw) from exc


def ordered_events(page, run):
    previous = (run.last_event_at, run.last_event_id) if run.last_event_at else None
    events = []
    for raw in page.events:
        event = parse_event(raw, run.stream)
        if not run.window_start <= event.updated_at < run.window_end:
            raise IngestionError("event_window", "event outside the fixed window", payload=raw)
        if previous is not None and event.order_key <= previous:
            raise IngestionError("event_order", "event does not follow the checkpoint", payload=raw)
        events.append(event)
        previous = event.order_key
    return events


def persist_event(run, event):
    record = SourceRecord.objects.filter(
        scope_id=run.scope_id, external_id=event.external_id
    ).first()
    if record:
        work = Work.objects.select_for_update().get(pk=record.work_id)
        if (work.kind, work.identifier) != (event.kind, event.identifier):
            raise IngestionError(
                "identity_conflict", "source record changed work identity", payload=event.raw
            )
    else:
        work, _ = Work.objects.select_for_update().get_or_create(
            kind=event.kind,
            identifier=event.identifier,
            defaults={"title": event.title},
        )
        record = SourceRecord.objects.create(
            scope_id=run.scope_id, external_id=event.external_id, work=work
        )
    SourceRecordRevision.objects.get_or_create(
        record=record,
        content_hash=event.content_hash,
        defaults={
            "run": run,
            "event_id": event.event_id,
            "updated_at": event.updated_at,
            "payload": event.payload,
        },
    )
    if work.source_updated_at is not None and event.order_key <= (
        work.source_updated_at,
        work.source_event_id,
    ):
        return  # A replay cannot replace the current projection with an older version.

    existing = {slot.source_slot_id: slot for slot in work.slots.all()}
    incoming = {slot_id: name for slot_id, name, _ in event.slots}
    if any(incoming.get(slot_id) != slot.name for slot_id, slot in existing.items()):
        raise IngestionError(
            "slot_conflict", "existing source slot removed or renamed", payload=event.raw
        )
    for position, (slot_id, name, affiliation) in enumerate(event.slots, start=1):
        ContributionSlot.objects.update_or_create(
            work=work,
            source_slot_id=slot_id,
            defaults={"position": position, "name": name, "affiliation": affiliation},
        )
    work.title = event.title
    work.published_on = event.published_on or work.published_on
    work.granted_on = event.granted_on or work.granted_on
    work.source_updated_at, work.source_event_id = event.order_key
    work.save(
        update_fields=(
            "title",
            "published_on",
            "granted_on",
            "source_updated_at",
            "source_event_id",
        )
    )
