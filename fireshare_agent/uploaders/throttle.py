"""
Bandwidth pacing for outbound uploads.

Why this is a *between-requests* limiter rather than a stream-level one: `requests` builds a
multipart body by materializing it in full (`RequestEncodingMixin._encode_files` calls `fp.read()`
on anything file-like), and urllib3 then hands the finished body to a single `sendall`. There is
no hook between those two points, so a lazy file wrapper that slept as it was read would pace
nothing - the sleeping would all happen while the body was being assembled in memory, and the
socket write would still go out at line speed.

So the unit of control is one chunk POST. The limiter caps the *average* rate across chunks; the
burst is one chunk, and the way to make the pacing finer-grained is to lower the chunk size. The
Settings caption says exactly this rather than implying a smoothness this cannot deliver.

Usage is "wait before, charge after":

    limiter.wait()          # sleep off whatever the previous sends still owe
    ...send some bytes...
    limiter.charge(n)       # record them

That order matters in both directions. Charging before a send would stall the very first chunk of
every upload for a full chunk's worth of time before a single byte moved, and sleeping after the
last chunk would delay the success event - and the post-upload move/delete - for a transfer that
has already finished.

Not thread-safe, and does not need to be: the pipeline uploads one file at a time on one worker
thread, and the Settings window's throwaway uploaders never transfer anything.
"""
from __future__ import annotations

import time
from typing import Callable


class RateLimiter:
    """Paces a byte stream to `bytes_per_second`. A rate of 0 (or less) disables it entirely -
    every method becomes a no-op, which is what the "0 = unlimited" setting relies on.

    `sleep` and `monotonic` are injectable so the pacing can be tested against a fake clock
    instead of by actually waiting. The pipeline also uses `sleep` for a second purpose: it passes
    one backed by the shutdown event, so a limiter pause - which at a low limit can be minutes -
    returns immediately when the agent is closing rather than holding up `stop()`."""

    def __init__(
        self,
        bytes_per_second: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = max(0.0, float(bytes_per_second))
        self._sleep = sleep
        self._monotonic = monotonic
        # Bytes already sent that the budget has not caught up with yet. Never negative: unused
        # time is not banked, so an agent that has been idle for an hour does not get to open with
        # an hour's worth of unthrottled transfer.
        self._debt = 0.0
        self._last_settled_at = monotonic()

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    def charge(self, num_bytes: int) -> None:
        """Records bytes that have just been sent. Never sleeps."""
        if not self.enabled or num_bytes <= 0:
            return
        self._settle()
        self._debt += num_bytes

    def wait(self) -> float:
        """Sleeps until everything charged so far has been paid for at the configured rate.
        Returns the seconds slept (0.0 if nothing was owed), which is what the tests assert on."""
        if not self.enabled:
            return 0.0
        self._settle()
        if self._debt <= 0:
            return 0.0

        delay = self._debt / self._rate
        self._sleep(delay)
        # Cleared rather than re-settled from the clock: the debt is paid by definition, and an
        # interrupted sleep (shutdown) should not leave the next call trying to collect the
        # remainder of a transfer that is being abandoned anyway.
        self._debt = 0.0
        self._last_settled_at = self._monotonic()
        return delay

    def _settle(self) -> None:
        """Credits the time that has passed since the last call against the outstanding debt.

        This is what makes the limiter free when the network is already slower than the limit: if
        a chunk took longer to upload than its budget allowed, the debt is gone by the time the
        next `wait()` asks about it and nothing sleeps at all."""
        now = self._monotonic()
        self._debt = max(0.0, self._debt - (now - self._last_settled_at) * self._rate)
        self._last_settled_at = now
