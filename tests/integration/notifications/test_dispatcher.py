"""The notification dispatcher against a real database (spec 6.6)."""

from contextlib import suppress

import pytest

from novelhog.domain.errors import NotFound
from novelhog.domain.types import Subscription
from novelhog.notifications import channels, dispatcher
from novelhog.notifications.channels import (
    Delivered,
    Failed,
    Gone,
    NotificationChannel,
    SendResult,
)
from novelhog.notifications.rules import NotificationMessage
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from tests.conftest import FakeClock

PUBLIC = "https://n.example"
URL = "https://example.com/toc"


class FakeChannel:
    name = "webpush"

    def __init__(self, results: dict[str, SendResult] | None = None) -> None:
        self.results = results or {}
        self.sent: list[tuple[str, NotificationMessage]] = []

    def send(self, subscription: Subscription, message: NotificationMessage) -> SendResult:
        self.sent.append((subscription.id, message))
        return self.results.get(subscription.id, Delivered())


def _registry(channel: FakeChannel) -> dict[str, NotificationChannel]:
    return {"webpush": channel}


def _novel(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    return repo.novels.create(URL, followed=True).id


def _release(repo: Repo, novel_id: str, released: list[int]) -> int:
    payload: dict[str, object] = {"followed": True, "released": released}
    with db.write(repo.engine) as conn:
        return event_log.append(conn, novel_id, novel_id, payload, now=repo.clock())


def _subscribe(repo: Repo, name: str, channel: str = "webpush") -> str:
    view, _ = repo.subscriptions.upsert(channel, {"endpoint": name}, name, None)
    return view.id


def _run(
    repo: Repo, channel: FakeChannel, owner: str = "w1", **kw: int
) -> dispatcher.DispatchReport:
    return dispatcher.dispatch(
        repo, _registry(channel), owner=owner, ttl=60, public_url=PUBLIC, **kw
    )


def _start(repo: Repo) -> str:
    """Create a novel and move the cursor past its creation events."""
    novel = _novel(repo)
    assert repo.consumers.claim("notifier", "setup", 60) is not None
    assert repo.consumers.advance("notifier", "setup", repo.events.since(0)[-1].seq)
    return novel


class TestDispatch__WhenTheConsumerIsFree:
    def test_dispatch__sends_each_matching_message_to_every_subscription(self, repo: Repo) -> None:
        novel = _start(repo)
        first, second = _subscribe(repo, "a"), _subscribe(repo, "b")
        _release(repo, novel, [1, 2])
        channel = FakeChannel()

        report = _run(repo, channel)

        assert sorted(sid for sid, _ in channel.sent) == sorted([first, second])
        assert channel.sent[0][1].body == "2 new chapters (1–2)"
        assert (report.events, report.delivered) == (1, 2)

    def test_dispatch__reads_at_most_batch_events_in_order(self, repo: Repo) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        for n in (1, 2, 3):
            _release(repo, novel, [n])
        channel = FakeChannel()

        report = _run(repo, channel, batch=2)

        assert [m.body for _, m in channel.sent] == [
            "1 new chapter (1–1)",
            "1 new chapter (2–2)",
        ]
        assert report.events == 2

    def test_dispatch__deletes_gone_subscriptions_and_logs_failed(self, repo: Repo) -> None:
        novel = _start(repo)
        gone, failed, fine = _subscribe(repo, "a"), _subscribe(repo, "b"), _subscribe(repo, "c")
        _release(repo, novel, [1])
        channel = FakeChannel({gone: Gone(), failed: Failed("boom")})

        report = _run(repo, channel)

        assert sorted(s.id for s in repo.subscriptions.all_with_address()) == sorted([failed, fine])
        assert (report.gone, report.failed, report.delivered) == (1, 1, 1)

    def test_dispatch__continues_when_a_gone_subscription_was_already_deleted(
        self, repo: Repo
    ) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        _subscribe(repo, "b")
        _release(repo, novel, [1])

        class Racing(FakeChannel):
            def send(self, subscription: Subscription, message: NotificationMessage) -> SendResult:
                with suppress(NotFound):
                    repo.subscriptions.delete(subscription.id)
                return Gone()

        channel = Racing()
        report = _run(repo, channel)

        assert report.gone == 2
        assert repo.subscriptions.all_with_address() == []

    def test_dispatch__skips_subscriptions_of_unknown_channels(self, repo: Repo) -> None:
        novel = _start(repo)
        _subscribe(repo, "tg", channel="telegram")
        keep = _subscribe(repo, "a")
        _release(repo, novel, [1])
        channel = FakeChannel()

        report = _run(repo, channel)

        assert [sid for sid, _ in channel.sent] == [keep]
        assert report.skipped == 1

    def test_dispatch__advances_to_the_last_event_read_and_releases_the_claim(
        self, repo: Repo
    ) -> None:
        novel = _start(repo)
        seq = _release(repo, novel, [1])

        report = _run(repo, FakeChannel())

        assert report.advanced is True
        assert repo.consumers.claim("notifier", "w2", 60) == seq

    def test_dispatch__sends_nothing_for_a_deleted_novel(self, repo: Repo) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        _release(repo, novel, [1])
        repo.novels.delete(novel)
        channel = FakeChannel()

        _run(repo, channel)

        assert channel.sent == []


class TestDispatch__OnTheFirstRun:
    def test_dispatch__starts_at_the_clock_and_ignores_older_events(self, repo: Repo) -> None:
        novel = _novel(repo)
        _subscribe(repo, "a")
        _release(repo, novel, [1])
        channel = FakeChannel()

        report = _run(repo, channel)

        assert channel.sent == []
        assert report.events == 0


class TestDispatch__WhenAnotherProcessHoldsTheClaim:
    def test_dispatch__does_nothing(self, repo: Repo) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        _release(repo, novel, [1])
        assert repo.consumers.claim("notifier", "other", 60) is not None
        channel = FakeChannel()

        report = _run(repo, channel)

        assert channel.sent == []
        assert report.claimed is False


class TestDispatch__AfterACrash:
    def test_dispatch__takes_over_an_expired_claim_and_redelivers_the_batch(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        _release(repo, novel, [1])
        crashed = FakeChannel()
        assert repo.consumers.claim("notifier", "crashed", 60) is not None
        clock.advance(seconds=120)

        _run(repo, crashed, owner="w2")

        assert len(crashed.sent) == 1


class TestDispatch__WhenPushIsNotConfigured:
    def test_dispatch__still_advances_the_cursor(self, repo: Repo) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        seq = _release(repo, novel, [1])

        report = dispatcher.dispatch(repo, {}, owner="w1", ttl=60, public_url=PUBLIC)

        assert (report.advanced, report.skipped) == (True, 1)
        assert repo.consumers.claim("notifier", "w2", 60) == seq


class TestDispatch__WhenTheClaimIsLost:
    def test_dispatch__does_not_advance_and_reports_it(self, repo: Repo, clock: FakeClock) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        before = repo.events.since(0)[-1].seq
        _release(repo, novel, [1])

        class Stealing(FakeChannel):
            def send(self, subscription: Subscription, message: NotificationMessage) -> SendResult:
                clock.advance(seconds=120)
                assert repo.consumers.claim("notifier", "thief", 60) == before
                return Delivered()

        report = _run(repo, Stealing())

        assert (report.advanced, report.lost) == (False, True)
        assert repo.consumers.release("notifier", "thief") is True
        assert repo.consumers.claim("notifier", "w3", 60) == before


class TestDispatch__WithABadSubscription:
    def test_dispatch__carries_on_and_advances_when_a_channel_reports_failed(
        self, repo: Repo
    ) -> None:
        import requests

        from novelhog import vapid
        from novelhog.notifications.channels import WebPushChannel

        novel = _start(repo)
        bad = repo.subscriptions.upsert(
            "webpush",
            {"endpoint": "https://push.example/a", "keys": {"p256dh": "p", "auth": "a"}},
            "bad",
            None,
        )[0].id
        good = _subscribe(repo, "good", channel="fake")
        seq = _release(repo, novel, [1])
        fake = FakeChannel()
        fake.name = "fake"
        real = WebPushChannel(vapid._generate(), "mailto:a@b.c", requests.Session())  # noqa: SLF001

        report = dispatcher.dispatch(
            repo, {"webpush": real, "fake": fake}, owner="w1", ttl=60, public_url=PUBLIC
        )

        assert [sid for sid, _ in fake.sent] == [good]
        assert (report.failed, report.advanced) == (1, True)
        assert bad in {s.id for s in repo.subscriptions.all_with_address()}
        assert repo.consumers.claim("notifier", "w2", 60) == seq


class TestDispatch__WithARegistryHolder:
    def test_dispatch__sends_once_keys_appear_after_a_pass_without_webpush(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        from novelhog import config, vapid
        from novelhog.notifications.channels import RegistryHolder

        settings = config.load(
            {"NOVELHOG_PUBLIC_URL": PUBLIC, "NOVELHOG_VAPID_SUBJECT": "mailto:a@b.c"}
        )
        holder = RegistryHolder(settings, repo, requests.Session())
        novel = _start(repo)
        _subscribe(repo, "a")
        _release(repo, novel, [1])

        first = dispatcher.dispatch(repo, holder.get, owner="w1", ttl=60, public_url=PUBLIC)
        assert (first.skipped, first.delivered) == (1, 0)

        vapid.ensure_keys(repo)
        calls: list[object] = []
        monkeypatch.setattr(channels, "webpush", lambda **kw: calls.append(kw))
        _release(repo, novel, [2])

        second = dispatcher.dispatch(repo, holder.get, owner="w1", ttl=60, public_url=PUBLIC)

        assert (second.delivered, len(calls)) == (1, 1)


class TestDispatch__WhenTheHeartbeatReportsALostClaim:
    def test_dispatch__stops_sending_and_does_not_advance(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        novel = _start(repo)
        _subscribe(repo, "a")
        _subscribe(repo, "b")
        before = repo.events.since(0)[-1].seq
        _release(repo, novel, [1])
        _release(repo, novel, [2])

        class Beat:
            lost = False

            def __init__(self, *_args: object) -> None:
                beats.append(self)

            def __enter__(self) -> Beat:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        beats: list[Beat] = []
        monkeypatch.setattr(dispatcher, "Heartbeat", Beat)

        class Losing(FakeChannel):
            def send(self, subscription: Subscription, message: NotificationMessage) -> SendResult:
                beats[0].lost = True
                return super().send(subscription, message)

        channel = Losing()
        report = _run(repo, channel)

        assert len(channel.sent) == 1
        assert (report.advanced, report.lost) == (False, True)
        assert repo.consumers.release("notifier", "w1") is True
        assert repo.consumers.claim("notifier", "w2", 60) == before


class TestDispatchTaskEvents:
    def test_dispatch__advances_past_task_events_and_sends_nothing_for_them(
        self, repo: Repo
    ) -> None:
        novel_id = _start(repo)
        _subscribe(repo, "a")
        with db.write(repo.engine) as conn:
            task_seq = event_log.append(
                conn, novel_id, "TK000000000001", {"state": "done"}, now=repo.clock()
            )
        channel = FakeChannel()

        report = _run(repo, channel)

        assert channel.sent == []
        assert report.advanced
        assert repo.consumers.claim("notifier", "w2", 60) == task_seq
