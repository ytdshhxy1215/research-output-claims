import logging
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections, transaction

from apps.sources import queue
from apps.sources.connector import FixtureConnector, IngestionError

logger = logging.getLogger(__name__)


def _require_autocommit():
    if not transaction.get_autocommit():
        raise RuntimeError("source HTTP requires database autocommit")


def process_run(connector, lease, *, lease_seconds):
    _require_autocommit()
    try:
        while True:
            checkpoint = queue.renew(lease, lease_seconds=lease_seconds)
            try:
                page = connector.page(checkpoint)
                if queue.commit_page(checkpoint, page, lease_seconds=lease_seconds):
                    logger.info("fixture run %s succeeded", lease.run_id)
                    return
            except IngestionError as exc:
                queue.fail(checkpoint, exc)
                logger.warning("fixture run %s: %s (%s)", lease.run_id, exc.code, exc)
                return
    except queue.LostLease as exc:
        logger.info("%s", exc)


def run_cycle(connector, *, page_limit, lease_seconds):
    _require_autocommit()
    queue.initialize_scopes()
    try:
        manifest = connector.manifest()
    except IngestionError as exc:
        logger.warning("fixture manifest: %s (%s)", exc.code, exc)
    else:
        queue.schedule(manifest, page_limit=page_limit)
    processed = 0
    while (lease := queue.claim(lease_seconds=lease_seconds)) is not None:
        process_run(connector, lease, lease_seconds=lease_seconds)
        processed += 1
    return processed


class Command(BaseCommand):
    help = "Poll the synthetic HTTP feed and process durable fixture SyncRuns."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Poll and drain ready runs once.")
        parser.add_argument("--page-limit", type=int, default=100, help="Page size for new runs.")
        parser.add_argument("--poll-interval", type=int, default=5, help="Seconds between polls.")
        parser.add_argument(
            "--request-timeout", type=int, default=10, help="HTTP timeout, seconds."
        )
        parser.add_argument(
            "--lease-seconds", type=int, default=60, help="Per-page lease, seconds."
        )
        parser.add_argument(
            "--replay",
            type=int,
            metavar="RUN_ID",
            help="Requeue a run and drain ready work once (implies --once).",
        )

    def handle(self, *args, **options):
        timeout = options["request_timeout"]
        lease_seconds = options["lease_seconds"]
        poll_interval = options["poll_interval"]
        page_limit = options["page_limit"]
        replay = options["replay"]
        once = options["once"] or replay is not None
        if not 1 <= page_limit <= 100:
            raise CommandError("--page-limit must be between 1 and 100")
        if not 0 < timeout < lease_seconds or poll_interval <= 0:
            raise CommandError("require 0 < request timeout < lease and a positive poll interval")
        if replay is not None:
            try:
                queue.requeue(replay)
            except queue.ReplayError as exc:
                raise CommandError(str(exc)) from exc
        connector = FixtureConnector(settings.FIXTURE_SOURCE_URL, timeout=timeout)
        try:
            while True:
                close_old_connections()
                try:
                    processed = run_cycle(
                        connector, page_limit=page_limit, lease_seconds=lease_seconds
                    )
                except OperationalError as exc:
                    if once:
                        raise CommandError(f"fixture worker database error: {exc}") from exc
                    logger.warning("fixture worker database unavailable; will retry: %s", exc)
                else:
                    if once:
                        self.stdout.write(f"fixture worker: {processed} attempt(s) claimed")
                        return
                sleep(poll_interval)
        except KeyboardInterrupt:
            pass
