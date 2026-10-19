"""The worker runtime: heartbeat and backoff (spec section 6.1)."""

import threading
import time

from novelhog.worker.runtime import Backoff, Heartbeat

TICK = 0.01


class TestHeartbeat:
    def test_heartbeat__renews_every_interval_while_the_block_runs_and_stops_afterwards(
        self,
    ) -> None:
        calls = 0
        enough = threading.Event()

        def renew() -> bool:
            nonlocal calls
            calls += 1
            if calls >= 3:
                enough.set()
            return True

        with Heartbeat(renew, TICK) as beat:
            assert enough.wait(5)
        after = calls
        time.sleep(TICK * 10)

        assert calls == after
        assert beat.lost is False

    def test_heartbeat__sets_lost_when_renew_returns_false_and_stops_renewing(self) -> None:
        calls = 0
        first = threading.Event()

        def renew() -> bool:
            nonlocal calls
            calls += 1
            first.set()
            return False

        with Heartbeat(renew, TICK) as beat:
            assert first.wait(5)
            time.sleep(TICK * 10)

        assert beat.lost is True
        assert calls == 1

    def test_heartbeat__keeps_renewing_after_renew_raises(self) -> None:
        calls = 0
        done = threading.Event()

        def renew() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                message = "database busy"
                raise RuntimeError(message)
            done.set()
            return True

        with Heartbeat(renew, TICK) as beat:
            assert done.wait(5)

        assert beat.lost is False

    def test_heartbeat__does_not_wait_a_whole_interval_to_exit(self) -> None:
        started = time.monotonic()
        with Heartbeat(lambda: True, 60):
            pass

        assert time.monotonic() - started < 5


class TestBackoff:
    def test_backoff__yields_5_10_20_capped_and_reset_starts_again(self) -> None:
        backoff = Backoff(30)

        assert [backoff.next() for _ in range(5)] == [5, 10, 20, 30, 30]
        backoff.reset()
        assert backoff.next() == 5

    def test_backoff__cap_below_five_limits_the_first_delay(self) -> None:
        assert Backoff(3).next() == 3
