import pytest

from planner.services.routing import resilience
from planner.services.routing.resilience import CircuitBreaker, OutboundThrottle


class FakeClock:
    """Stands in for the time module inside resilience.py: sleeping just moves time on."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(resilience, "time", fake)
    return fake


@pytest.fixture
def breaker(clock) -> CircuitBreaker:
    return CircuitBreaker("osrm", threshold=3, reset_seconds=60)


def test_breaker_opens_after_three_failures_in_a_row(breaker):
    for _ in range(2):
        breaker.record_failure()
    assert (breaker.state(), breaker.allow_request()) == ("closed", True)

    breaker.record_failure()

    assert (breaker.state(), breaker.allow_request()) == ("open", False)
    assert breaker.seconds_until_half_open() == 60


def test_a_success_resets_the_count(breaker):
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state() == "closed"


def test_after_the_pause_exactly_one_trial_goes_through(breaker, clock):
    for _ in range(3):
        breaker.record_failure()
    clock.now += 60

    assert breaker.state() == "half_open"
    assert breaker.allow_request() is True
    assert breaker.allow_request() is False  # a second caller waits for the trial's result


def test_a_successful_trial_closes_the_breaker(breaker, clock):
    for _ in range(3):
        breaker.record_failure()
    clock.now += 60
    breaker.allow_request()

    breaker.record_success()

    assert (breaker.state(), breaker.allow_request()) == ("closed", True)


def test_a_failed_trial_opens_it_for_another_pause(breaker, clock):
    for _ in range(3):
        breaker.record_failure()
    clock.now += 60
    breaker.allow_request()

    breaker.record_failure()

    assert breaker.state() == "open"
    assert breaker.seconds_until_half_open() == 60
    clock.now += 60
    assert breaker.allow_request() is True  # and a fresh trial after that pause


def test_breakers_are_per_provider(breaker):
    for _ in range(3):
        breaker.record_failure()

    assert CircuitBreaker("ors", threshold=3, reset_seconds=60).state() == "closed"


def test_throttle_sleeps_out_the_rest_of_the_interval(clock):
    throttle = OutboundThrottle("osrm", min_interval_seconds=1.0)

    throttle.wait_turn()  # the first call goes at once
    clock.now += 0.3
    throttle.wait_turn()  # 0.3 s later: waits the remaining 0.7 s
    clock.now += 5
    throttle.wait_turn()  # long after: no wait

    assert clock.slept == [pytest.approx(0.7)]


def test_throttle_with_no_interval_never_sleeps(clock):
    throttle = OutboundThrottle("osrm", min_interval_seconds=0)

    for _ in range(3):
        throttle.wait_turn()

    assert clock.slept == []
