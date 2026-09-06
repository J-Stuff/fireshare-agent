"""
Coverage for the upload speed limiter. Everything here runs against a fake clock and a fake
sleep - a test that actually waited for a rate limit would be the slowest thing in the suite and
would still be flaky on a loaded CI runner.
"""
from fireshare_agent.uploaders.throttle import RateLimiter


class FakeClock:
    """A monotonic clock that only moves when a test says so - including when the limiter sleeps,
    which is what makes "did it sleep long enough" checkable without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(kbps: float, clock: FakeClock) -> RateLimiter:
    return RateLimiter(kbps * 1024, sleep=clock.sleep, monotonic=clock.monotonic)


def test_a_zero_limit_never_sleeps():
    clock = FakeClock()
    limiter = _limiter(0, clock)

    limiter.charge(500 * 1024 * 1024)
    slept = limiter.wait()

    assert slept == 0.0
    assert clock.slept == []
    assert limiter.enabled is False


def test_a_negative_limit_is_treated_as_no_limit():
    # Reachable from a hand-edited config.json; clamp_speed_limit normalizes it, but the limiter
    # must not misbehave if one ever reaches it anyway.
    clock = FakeClock()
    limiter = _limiter(-100, clock)

    limiter.charge(10 * 1024 * 1024)

    assert limiter.wait() == 0.0


def test_the_first_send_is_never_delayed():
    """wait() is called before each chunk, so an empty limiter must let the transfer start
    immediately - charging up front would stall every upload before its first byte."""
    clock = FakeClock()
    limiter = _limiter(100, clock)

    assert limiter.wait() == 0.0
    assert clock.slept == []


def test_sleeps_for_exactly_the_time_the_charged_bytes_cost():
    clock = FakeClock()
    limiter = _limiter(100, clock)  # 100 KB/s

    limiter.charge(500 * 1024)  # 500 KB, so 5 seconds' worth
    slept = limiter.wait()

    assert slept == 5.0
    assert clock.slept == [5.0]


def test_time_already_spent_uploading_counts_towards_the_budget():
    """The point of the limiter is an average rate, so a chunk that took a while to upload has
    already paid part of its own way - it must not then be made to wait the full time again."""
    clock = FakeClock()
    limiter = _limiter(100, clock)

    limiter.charge(500 * 1024)  # 5 seconds' worth
    clock.advance(2.0)          # the POST itself took 2 seconds
    slept = limiter.wait()

    assert slept == 3.0


def test_a_connection_slower_than_the_limit_never_sleeps():
    clock = FakeClock()
    limiter = _limiter(100, clock)

    limiter.charge(100 * 1024)  # 1 second's worth
    clock.advance(9.0)          # but the chunk took 9 seconds to send
    slept = limiter.wait()

    assert slept == 0.0
    assert clock.slept == []


def test_idle_time_is_not_banked_into_a_later_burst():
    """An agent that has been sitting idle for an hour must not get an hour of unthrottled
    transfer the moment a clip appears - unused budget is discarded, not accumulated."""
    clock = FakeClock()
    limiter = _limiter(100, clock)

    clock.advance(3600.0)
    limiter.charge(500 * 1024)

    assert limiter.wait() == 5.0


def test_pacing_holds_its_average_over_a_run_of_chunks():
    clock = FakeClock()
    limiter = _limiter(100, clock)
    chunk = 200 * 1024  # 2 seconds' worth each
    chunks = 5

    started_at = clock.now
    for _ in range(chunks):
        limiter.wait()
        clock.advance(0.5)  # each chunk uploads far faster than the limit allows
        limiter.charge(chunk)

    # Four waits of 2s - the fifth chunk is charged but never waited on, since the loop ends -
    # plus the 0.5s each send actually took.
    elapsed = clock.now - started_at
    assert elapsed == 10.5
    assert (chunks * chunk) / elapsed < 100 * 1024


def test_charge_ignores_nonpositive_byte_counts():
    clock = FakeClock()
    limiter = _limiter(100, clock)

    limiter.charge(0)
    limiter.charge(-4096)

    assert limiter.wait() == 0.0
