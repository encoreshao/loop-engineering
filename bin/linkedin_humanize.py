#!/usr/bin/env python3
"""Human-like pacing and motion for the LinkedIn browser. Humanizer is pure
(rng, sleep and clock are injectable so tests are deterministic); HumanPage,
added below, is the only code that drives a Playwright page."""
import math
import random
import re
import time

MIN_GAP_S = 2.5            # hard floor between two page loads (jitter is added on top)
DWELL_MIN_S = 5.0
DWELL_MAX_S = 60.0
READ_CHARS_PER_S = 22.0
START_DELAY_MAX_S = 1800.0
SKIP_DAY_P = 0.1
WANDER_MIN, WANDER_MAX = 1, 3
_HOURS_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")


def parse_active_hours(text, default=(9, 21)):
    """'9-21' -> (9, 21); anything malformed or out of range -> default."""
    match = _HOURS_RE.match(text) if isinstance(text, str) else None
    if not match:
        return default
    start, end = int(match.group(1)), int(match.group(2))
    if 0 <= start < end <= 24:
        return start, end
    return default


def within_hours(hour, window):
    return window[0] <= hour < window[1]


class Humanizer:
    def __init__(self, rng=None, sleep=None, clock=None):
        self.rng = rng or random.Random()
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._last_load = None

    def sleep(self, seconds):
        self._sleep(max(0.0, seconds))

    def dwell_seconds(self, text_len=0):
        base = self.rng.lognormvariate(math.log(12.0), 0.5)
        read = text_len / READ_CHARS_PER_S * self.rng.uniform(0.3, 0.7)
        return min(DWELL_MAX_S, max(DWELL_MIN_S, base + read))

    def pause_seconds(self, lo=0.4, hi=2.5):
        return self.rng.uniform(lo, hi)

    def gap_before_load(self):
        """Seconds to wait so two page loads are never closer than MIN_GAP_S
        (plus jitter). Records the load as happening after the wait."""
        now = self._clock()
        wait = 0.0
        if self._last_load is not None:
            wait = max(0.0, MIN_GAP_S + self.rng.uniform(0.0, 3.0) - (now - self._last_load))
        self._last_load = now + wait
        return wait

    def scroll_plan(self, total_px):
        """Uneven (delta_px, pause_s) wheel steps reaching at least total_px,
        with the occasional short scroll back up."""
        steps, done = [], 0
        while done < total_px:
            delta = min(int(self.rng.uniform(180, 520)), total_px - done)
            steps.append((max(delta, 1), self.rng.uniform(0.3, 2.2)))
            done += max(delta, 1)
            if done > 600 and self.rng.random() < 0.12:
                back = -int(self.rng.uniform(80, 240))
                steps.append((back, self.rng.uniform(0.4, 1.5)))
                done += back
        return steps

    def mouse_path(self, start, end, steps=None):
        """Points along a curved (quadratic Bezier) path, sometimes overshooting
        the target slightly before settling on it. The last point is `end`."""
        (x0, y0), (x1, y1) = start, end
        n = steps or self.rng.randint(14, 30)
        cx = (x0 + x1) / 2 + self.rng.uniform(-80, 80)
        cy = (y0 + y1) / 2 + self.rng.uniform(-80, 80)
        points = []
        for i in range(1, n + 1):
            t = i / n
            points.append((round((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1, 1),
                           round((1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1, 1)))
        points[-1] = (x1, y1)
        if self.rng.random() < 0.4:
            overshoot = (x1 + self.rng.choice((-1, 1)) * self.rng.uniform(4, 14),
                         y1 + self.rng.choice((-1, 1)) * self.rng.uniform(4, 14))
            points.insert(len(points) - 1, (round(overshoot[0], 1), round(overshoot[1], 1)))
        return points

    def wander_pages(self, candidates):
        pool = list(candidates)
        self.rng.shuffle(pool)
        return pool[:min(len(pool), self.rng.randint(WANDER_MIN, WANDER_MAX))]

    def skip_today(self):
        return self.rng.random() < SKIP_DAY_P

    def start_delay_seconds(self):
        return self.rng.uniform(0.0, START_DELAY_MAX_S)
