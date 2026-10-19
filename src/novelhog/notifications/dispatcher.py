"""The `notifier` consumer: reads the event log and delivers notifications (spec 6.6)."""

from contextlib import suppress
from dataclasses import dataclass

from novelhog import log
from novelhog.domain.errors import NotFound
from novelhog.domain.payloads import NovelView
from novelhog.notifications import rules
from novelhog.notifications.channels import Delivered, Failed, Gone, NotificationChannel
from novelhog.repo import Repo
from novelhog.repo.events import EventRow
from novelhog.worker.runtime import Heartbeat

CONSUMER = "notifier"
_HEARTBEAT_DIVISOR = 3


@dataclass(frozen=True)
class DispatchReport:
    """What one `dispatch` call did."""

    claimed: bool = False
    events: int = 0
    delivered: int = 0
    gone: int = 0
    failed: int = 0
    skipped: int = 0
    advanced: bool = False
    lost: bool = False


def _novel(repo: Repo, event: EventRow) -> NovelView | None:
    try:
        return repo.novels.get(event.novel_id)
    except NotFound:
        return None


def dispatch(
    repo: Repo,
    registry: dict[str, NotificationChannel],
    *,
    owner: str,
    ttl: int,
    public_url: str,
    batch: int = 100,
) -> DispatchReport:
    """Deliver the notifications of the next `batch` events, then advance the cursor.

    Does nothing when another live process holds the consumer. Delivery is at least once: a crash
    before the cursor moves repeats that batch. When the claim is lost the cursor is not moved.
    """
    last_seq = repo.consumers.claim(CONSUMER, owner, ttl)
    if last_seq is None:
        return DispatchReport()
    logger = log.get(consumer=CONSUMER)
    try:
        events = repo.events.since(last_seq, limit=batch)
        if not events:
            repo.consumers.release(CONSUMER, owner)
            return DispatchReport(claimed=True)
        delivered = gone = failed = skipped = 0
        with Heartbeat(
            lambda: repo.consumers.renew(CONSUMER, owner, ttl), ttl / _HEARTBEAT_DIVISOR
        ) as beat:
            for event in events:
                if beat.lost:
                    break
                message = rules.match(event, _novel(repo, event), public_url=public_url)
                if message is None:
                    continue
                for subscription in repo.subscriptions.all_with_address():
                    channel = registry.get(subscription.channel)
                    if channel is None:
                        skipped += 1
                        logger.info(
                            "notification_channel_unavailable",
                            channel=subscription.channel,
                            subscription_id=subscription.id,
                        )
                        continue
                    result = channel.send(subscription, message)
                    if isinstance(result, Delivered):
                        delivered += 1
                    elif isinstance(result, Gone):
                        gone += 1
                        with suppress(NotFound):  # another process may have removed it already
                            repo.subscriptions.delete(subscription.id)
                    elif isinstance(result, Failed):
                        failed += 1
                        logger.warning(
                            "notification_failed",
                            channel=subscription.channel,
                            subscription_id=subscription.id,
                            reason=result.reason,
                        )
        lost = beat.lost or not repo.consumers.advance(CONSUMER, owner, events[-1].seq)
        if lost:
            logger.warning("notifier_claim_lost", last_seq=last_seq)
        return DispatchReport(
            claimed=True,
            events=len(events),
            delivered=delivered,
            gone=gone,
            failed=failed,
            skipped=skipped,
            advanced=not lost,
            lost=lost,
        )
    except BaseException:
        repo.consumers.release(CONSUMER, owner)
        raise
