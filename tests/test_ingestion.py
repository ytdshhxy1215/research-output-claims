import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import StringIO
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.utils import timezone

from apps.catalog.models import ContributionSlot, Work
from apps.sources import queue
from apps.sources.connector import FixtureConnector, Manifest
from apps.sources.models import SourceRecordRevision, SourceScope, SyncRun, initial_watermark
from fixture_source.__main__ import FixtureServer
from fixture_source.data import EVENTS, RELEASES


def http_json(base_url, path, data=None, **params):
    suffix = "?" + urlencode(params) if params else ""
    body = None if data is None else json.dumps(data).encode()
    request = Request(
        base_url + path + suffix, data=body, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=3) as response:
        return json.load(response)


@pytest.fixture
def feed(settings):
    with FixtureServer(("127.0.0.1", 0)) as server:
        server_thread = Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        server_thread.start()
        settings.FIXTURE_SOURCE_URL = f"http://127.0.0.1:{server.server_port}"
        try:
            yield settings.FIXTURE_SOURCE_URL
        finally:
            server.shutdown()
            server_thread.join(timeout=3)


def poll(**options):
    if "replay" in options:
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission

        actor, _ = get_user_model().objects.get_or_create(username="ingestion-replay-test")
        actor.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="sources", codename__in=("view_syncrun", "request_syncreplay"),
        ))
        options.update(actor=actor.username, reason="验证重放幂等性")
    call_command("run_sync_worker", once=True, stdout=StringIO(), **options)


def scope(stream):
    return SourceScope.objects.get(source="fixture", stream=stream)


def batch_run(stream, release):
    return SyncRun.objects.get(scope=scope(stream), release_id=release)


def publish_batch_2(feed):
    return http_json(feed, "/__fixture/release", {"release_id": "batch-2"})


def request_batch_2_works(feed, **extra):
    params = {
        "release_id": "batch-2",
        "from": RELEASES["batch-1"],
        "to": RELEASES["batch-2"],
        "limit": 1,
        **extra,
    }
    return http_json(feed, "/v1/works", **params)


def test_fixture_http_pins_release_and_breaks_timestamp_ties(feed):
    assert http_json(feed, "/healthz") == {"status": "ok", "source": "fixture"}
    assert http_json(feed, "/v1/manifest")["release_id"] == "batch-1"
    with pytest.raises(HTTPError) as unpublished:
        request_batch_2_works(feed)
    assert unpublished.value.code == 400
    publish_batch_2(feed)
    assert http_json(feed, "/v1/manifest")["streams"]["works"]["upper_bound"] == RELEASES["batch-2"]
    pinned = http_json(
        feed,
        "/v1/works",
        release_id="batch-1",
        **{"from": "1970-01-01T00:00:00Z", "to": RELEASES["batch-1"]},
        limit=1,
    )
    assert [event["event_id"] for event in pinned["events"]] == ["paper-01"]
    assert pinned["next_cursor"] is None
    first = request_batch_2_works(feed)
    assert first == request_batch_2_works(feed)
    second = request_batch_2_works(feed, cursor=first["next_cursor"])
    third = request_batch_2_works(feed, cursor=second["next_cursor"])
    events = [page["events"][0] for page in (first, second, third)]
    assert [event["event_id"] for event in events] == [
        "paper-02-update",
        "paper-03-duplicate",
        "paper-04-new",
    ]
    assert {event["updated_at"] for event in events} == {RELEASES["batch-1"]}
    assert first["events"][0]["record"] == second["events"][0]["record"]
    assert third["next_cursor"] is None
    with pytest.raises(HTTPError) as wrong_cursor:
        http_json(
            feed,
            "/v1/patents",
            release_id="batch-2",
            cursor=first["next_cursor"],
            **{"from": RELEASES["batch-1"], "to": RELEASES["batch-2"]},
            limit=1,
        )
    assert wrong_cursor.value.code == 400


def catalog_snapshot():
    return {
        "works": list(Work.objects.order_by("pk").values()),
        "slots": list(ContributionSlot.objects.order_by("pk").values()),
        "revisions": list(SourceRecordRevision.objects.order_by("pk").values()),
        "watermarks": list(SourceScope.objects.order_by("pk").values_list("watermark", flat=True)),
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("inject_429", [False, True], ids=["clean", "http-429-resume"])
def test_incremental_terminal_state_and_historical_replay(feed, monkeypatch, inject_429):
    real_get = FixtureConnector._get
    calls = []

    def observe_get(self, path, params=None):
        assert connection.get_autocommit() and not connection.in_atomic_block
        calls.append((path, params))
        return real_get(self, path, params)

    monkeypatch.setattr(FixtureConnector, "_get", observe_get)
    poll(page_limit=1)
    assert SourceScope.objects.count() == 2
    assert Work.objects.count() == 2
    paper = Work.objects.get(kind=Work.Kind.PAPER, identifier="10.5555/p1")
    assert paper.published_on == date(2025, 1, 9)
    slots = list(paper.slots.values_list("pk", "position", "source_slot_id", "name", "affiliation"))
    assert [slot[1:] for slot in slots] == [
        (1, "p1-a1", "林岚", "alpha"),
        (2, "p1-a2", "陈星", "乙院"),
    ]
    patent = Work.objects.get(kind=Work.Kind.PATENT, identifier="CN:FIXTURE-2025-0001")
    assert patent.published_on == date(2025, 1, 16) and patent.granted_on is None
    assert SourceRecordRevision.objects.count() == 2
    batch_1 = batch_run("works", "batch-1")
    original_watermark = scope("works").watermark
    assert original_watermark == batch_1.window_end
    publish_batch_2(feed)
    if inject_429:
        cursor = request_batch_2_works(feed)["next_cursor"]
        http_json(
            feed,
            "/__fixture/failure",
            {
                "stream": "works",
                "cursor": cursor,
                "status": 429,
                "count": 1,
                "retry_after": 61,
            },
        )
    poll(page_limit=1)
    batch_2 = batch_run("works", "batch-2")
    if inject_429:
        assert batch_2.status == SyncRun.Status.RETRY
        assert batch_2.cursor == cursor
        assert batch_2.pages_committed == batch_2.events_committed == 1
        assert batch_2.error_code == "rate_limited"
        failed = batch_2.attempt_log.get(fencing_token=1)
        assert (failed.start_cursor, failed.end_cursor, failed.outcome) == ("", cursor, "retry")
        assert batch_2.next_attempt_at == failed.finished_at + timedelta(seconds=61)
        assert scope("works").watermark == original_watermark
        assert scope("patents").watermark > original_watermark  # independent scopes
        assert paper.slots.get(source_slot_id="p1-a1").affiliation == "甲院"
        assert not Work.objects.filter(identifier="10.5555/p2").exists()
        calls.clear()
        poll(page_limit=37)
        assert [path for path, _ in calls] == ["/v1/manifest"]
        # Advance only the retry eligibility clock, not the checkpoint or worker code.
        SyncRun.objects.filter(pk=batch_2.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        calls.clear()
        poll(page_limit=37)
        assert calls[1][0] == "/v1/works"
        assert calls[1][1]["cursor"] == cursor
        assert calls[1][1]["limit"] == 1  # durable page contract, independent of new process CLI
        batch_2.refresh_from_db()
        assert batch_2.attempts == 2
        assert batch_2.attempt_log.get(fencing_token=2).start_cursor == cursor
    assert batch_2.status == SyncRun.Status.SUCCEEDED
    assert batch_2.events_committed == batch_2.pages_committed == 3
    assert scope("works").watermark == batch_2.window_end
    assert scope("patents").watermark == batch_2.window_end
    assert list(Work.objects.order_by("identifier").values_list("identifier", flat=True)) == [
        "10.5555/p1",
        "10.5555/p2",
        "CN:FIXTURE-2025-0001",
    ]
    assert list(paper.slots.values_list("pk", "affiliation")) == [
        (slots[0][0], "甲院"),
        (slots[1][0], "乙院"),
    ]
    patent.refresh_from_db()
    assert patent.granted_on == date(2025, 1, 30)
    assert ContributionSlot.objects.count() == 4
    assert SourceRecordRevision.objects.count() == 5
    assert set(
        patent.source_records.values_list("revisions__payload__document_kind", flat=True)
    ) == {"A", "B"}
    final = catalog_snapshot()
    poll(replay=batch_1.pk)
    poll(replay=batch_2.pk)
    poll()
    assert catalog_snapshot() == final  # PKs, versions, projection, and watermarks survive replay.
    assert batch_1.attempt_log.count() == 2
    assert batch_2.attempt_log.count() == (3 if inject_429 else 2)
    assert SyncRun.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_new_release_cannot_overtake_failed_scope_window(feed):
    http_json(feed, "/__fixture/failure", {"stream": "works", "status": 503, "retry_after": 120})
    poll()
    first = batch_run("works", "batch-1")
    assert first.status == SyncRun.Status.RETRY and first.error_code == "http_transient"
    publish_batch_2(feed)
    poll()
    assert scope("works").watermark == initial_watermark()
    assert scope("works").runs.count() == 1
    assert batch_run("patents", "batch-2").status == SyncRun.Status.SUCCEEDED
    poll(replay=first.pk)
    first.refresh_from_db()
    assert scope("works").watermark == first.window_end
    assert first.status == SyncRun.Status.SUCCEEDED
    poll()
    second = batch_run("works", "batch-2")
    assert second.status == SyncRun.Status.SUCCEEDED and second.window_start == first.window_end


@pytest.mark.django_db(transaction=True)
def test_conflicting_event_rolls_back_whole_page_and_is_quarantined(feed, monkeypatch):
    originals = EVENTS["works"]
    poll()
    publish_batch_2(feed)
    broken = dict(
        originals[-1], external_id="paper-1"
    )  # the same source record now claims a new DOI
    monkeypatch.setitem(EVENTS, "works", (*originals[:-1], broken))
    poll()
    run = batch_run("works", "batch-2")
    assert run.status == SyncRun.Status.BLOCKED
    assert run.error_code == "identity_conflict"
    assert run.cursor == "" and run.pages_committed == 0
    assert scope("works").watermark == run.window_start
    assert Work.objects.get(identifier="10.5555/p1").slots.get(position=1).affiliation == "alpha"
    assert SourceRecordRevision.objects.filter(record__scope__stream="works").count() == 1
    assert run.attempt_log.get().rejected_payload == broken
    monkeypatch.setitem(EVENTS, "works", originals)
    poll(replay=run.pk)
    run.refresh_from_db()
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.attempt_log.get(fencing_token=1).rejected_payload == broken


@pytest.mark.django_db(transaction=True)
def test_postgresql_skip_locked_single_claim_and_expired_fencing(feed):
    assert connection.vendor == "postgresql"
    connector = FixtureConnector(feed)
    manifest = connector.manifest()
    queue.initialize_scopes()
    queue.schedule(
        Manifest(manifest.release_id, {**manifest.upper_bounds, "patents": initial_watermark()}),
        page_limit=1,
    )
    works = scope("works")

    def claim_in_connection(barrier=None):
        try:
            if barrier:
                barrier.wait(timeout=5)
            return queue.claim(lease_seconds=30)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with transaction.atomic():
            SourceScope.objects.select_for_update().get(pk=works.pk)
            assert pool.submit(claim_in_connection).result(timeout=5) is None
        barrier = Barrier(2)
        results = [pool.submit(claim_in_connection, barrier) for _ in range(2)]
        claims = [result.result(timeout=5) for result in results]
        assert claims.count(None) == 1
        original = next(lease for lease in claims if lease is not None)
        original_page = queue.renew(original, lease_seconds=30)
        response = connector.page(original_page)
        SyncRun.objects.filter(pk=original.run_id).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(queue.LostLease):
            queue.commit_page(original_page, response, lease_seconds=30)
        successor = pool.submit(claim_in_connection).result(timeout=5)
        assert successor.run_id == original.run_id and successor.token == original.token + 1
        with pytest.raises(queue.LostLease):
            queue.commit_page(original_page, response, lease_seconds=30)
        assert not Work.objects.exists()
        assert scope("works").watermark == initial_watermark()
        resumed = queue.renew(successor, lease_seconds=30)
        assert queue.commit_page(resumed, connector.page(resumed), lease_seconds=30)
    assert Work.objects.count() == SourceRecordRevision.objects.count() == 1
    run = SyncRun.objects.get(pk=original.run_id)
    assert scope("works").watermark == run.window_end
    assert list(run.attempt_log.order_by("fencing_token").values_list("outcome", flat=True)) == [
        "expired",
        "succeeded",
    ]
