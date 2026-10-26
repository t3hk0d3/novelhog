"""Tests for the worker's FlareSolverr client."""

from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from novelhog.flaresolverr import (
    SESSION_TTL_MINUTES,
    FlareSolverr,
    FlareSolverrFailed,
    FlareSolverrUnavailable,
)
from tests.fixtures.fake_flaresolverr import FakeFlareSolverr

PAGE = "https://site.test/toc"
OTHER = "https://site.test/c/1"
DEAD = "http://127.0.0.1:9"
CLEARANCE = {
    "userAgent": "Solver UA",
    "cookies": [{"name": "cf_clearance", "value": "token", "domain": ".site.test"}],
}


@pytest.fixture
def fake() -> Iterator[FakeFlareSolverr]:
    with FakeFlareSolverr({PAGE: "<html>toc</html>", OTHER: "<html>one</html>"}) as server:
        yield server


class TestFlareSolverrGet__Session:
    def test_get__creates_the_workers_session_on_first_use(self, fake: FakeFlareSolverr) -> None:
        FlareSolverr(fake.url, "w1").get(PAGE)
        assert [c["session"] for c in fake.commands_named("sessions.create")] == ["novelhog-w1"]
        (get,) = fake.commands_named("request.get")
        assert get["session"] == "novelhog-w1"
        assert get["session_ttl_minutes"] == SESSION_TTL_MINUTES

    def test_get__reuses_the_session_for_later_pages(self, fake: FakeFlareSolverr) -> None:
        solver = FlareSolverr(fake.url, "w1")
        solver.get(PAGE)
        solver.get(OTHER)
        assert len(fake.commands_named("sessions.create")) == 1
        assert fake.requested_urls == [PAGE, OTHER]

    def test_get__a_lost_session_is_created_again_once(self, fake: FakeFlareSolverr) -> None:
        solver = FlareSolverr(fake.url, "w1")
        solver.get(PAGE)
        fake.sessions.clear()  # FlareSolverr restarted
        assert solver.get(OTHER).html == "<html>one</html>"
        assert len(fake.commands_named("sessions.create")) == 2

    def test_get__sends_the_cookies(self, fake: FakeFlareSolverr) -> None:
        FlareSolverr(fake.url, "w1").get(PAGE, {"toc_show": "10000"})
        (get,) = fake.commands_named("request.get")
        assert get["cookies"] == [{"name": "toc_show", "value": "10000"}]


class TestFlareSolverrGet__Clearance:
    def test_get__returns_the_html_and_records_the_clearance(self, fake: FakeFlareSolverr) -> None:
        fake.solution = CLEARANCE
        solver = FlareSolverr(fake.url, "w1")
        with capture_logs() as logs:
            solution = solver.get(PAGE)
        assert solution.html == "<html>toc</html>"
        assert solution.clearance is not None
        assert solver.clearances.for_url(OTHER) is solution.clearance
        assert [e["event"] for e in logs] == ["clearance_solved"]

    def test_get__page_without_clearance_records_none(self, fake: FakeFlareSolverr) -> None:
        solver = FlareSolverr(fake.url, "w1")
        assert solver.get(PAGE).clearance is None
        assert solver.clearances.for_url(PAGE) is None


class TestFlareSolverrGet__Failures:
    def test_get__unsolved_page_raises_failed(self, fake: FakeFlareSolverr) -> None:
        with pytest.raises(FlareSolverrFailed, match="Error solving"):
            FlareSolverr(fake.url, "w1").get("https://site.test/unknown")
        assert len(fake.commands_named("sessions.create")) == 1

    def test_get__empty_page_raises_failed(self, fake: FakeFlareSolverr) -> None:
        fake.pages[PAGE] = ""
        with pytest.raises(FlareSolverrFailed, match="no content"):
            FlareSolverr(fake.url, "w1").get(PAGE)

    def test_get__flaresolverr_down_raises_unavailable(self) -> None:
        with pytest.raises(FlareSolverrUnavailable):
            FlareSolverr(DEAD, "w1").get(PAGE)


class TestFlareSolverrClose:
    def test_close__destroys_the_session(self, fake: FakeFlareSolverr) -> None:
        solver = FlareSolverr(fake.url, "w1")
        solver.get(PAGE)
        solver.close()
        assert [c["session"] for c in fake.commands_named("sessions.destroy")] == ["novelhog-w1"]
        assert fake.sessions == set()

    def test_close__without_a_session_sends_nothing(self, fake: FakeFlareSolverr) -> None:
        FlareSolverr(fake.url, "w1").close()
        assert fake.commands == []

    def test_close__flaresolverr_down_is_logged(self, fake: FakeFlareSolverr) -> None:
        solver = FlareSolverr(fake.url, "w1")
        solver.get(PAGE)
        fake.stop()
        with capture_logs() as logs:
            solver.close()
        assert [e["event"] for e in logs] == ["flaresolverr_session_not_destroyed"]
