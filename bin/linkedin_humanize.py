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
RENDER_TIMEOUT_S = 600.0   # a page that has not rendered after 10 minutes is given up on
RENDER_POLL_S = 2.0
RENDER_MIN_CHARS = 300     # less visible text than this is still the loading shell
SKIP_DAY_P = 0.1
WANDER_MIN, WANDER_MAX = 1, 3
_HOURS_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")


class PageTimeout(Exception):
    """A page did not respond or finish rendering within RENDER_TIMEOUT_S."""


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


class HumanPage:
    """Wraps a Playwright page. Navigation, scrolling and cursor movement all
    happen here and nowhere else, so every one of them is paced."""

    def __init__(self, page, human):
        self.page = page
        self.human = human
        self._pos = (400.0, 300.0)

    def goto(self, url):
        self.human.sleep(self.human.gap_before_load())
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_S * 1000)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "TimeoutError":
                raise PageTimeout() from exc
            raise
        self.idle_mouse()

    def evaluate(self, js):
        return self.page.evaluate(js)

    def wait_rendered(self, timeout=RENDER_TIMEOUT_S, poll=RENDER_POLL_S, min_chars=RENDER_MIN_CHARS):
        """LinkedIn renders client-side, so a loaded page is not a rendered page.
        Poll the visible text length until it is long enough and has stopped
        changing for two polls in a row; PageTimeout after `timeout` seconds."""
        clock = self.human._clock
        deadline = clock() + timeout
        last, stable = -1, 0
        while True:
            try:
                size = int(self.page.evaluate("() => (document.body && document.body.innerText || '').length") or 0)
            except Exception:  # noqa: BLE001 - mid-navigation: just poll again
                size = 0
            stable = stable + 1 if size == last and size >= min_chars else 0
            last = size
            if stable >= 2:
                return
            if clock() >= deadline:
                raise PageTimeout()
            self.human.sleep(poll)

    def _move_to(self, locator):
        box = locator.bounding_box()
        if not box:
            return
        end = (box["x"] + box["width"] * self.human.rng.uniform(0.3, 0.7),
               box["y"] + box["height"] * self.human.rng.uniform(0.3, 0.7))
        for x, y in self.human.mouse_path(self._pos, end):
            self.page.mouse.move(x, y)
            self.human.sleep(self.human.rng.uniform(0.005, 0.03))
        self._pos = end

    def click(self, selector):
        """Click the first visible match like a person. False when nothing matches."""
        locator = self.page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=8000)
        except Exception:  # noqa: BLE001 - absent control: the caller falls back
            return False
        self._move_to(locator)
        self.human.sleep(self.human.pause_seconds())
        self.page.mouse.click(*self._pos)
        return True

    def type_into(self, selector, text):
        """Click a field and type with uneven key delays, then Enter. False if the field is missing."""
        if not self.click(selector):
            return False
        for ch in text:
            self.page.keyboard.type(ch)
            self.human.sleep(self.human.rng.uniform(0.05, 0.22))
        self.human.sleep(self.human.pause_seconds())
        self.page.keyboard.press("Enter")
        return True

    def idle_mouse(self):
        end = (self.human.rng.uniform(200, 1200), self.human.rng.uniform(150, 700))
        for x, y in self.human.mouse_path(self._pos, end):
            self.page.mouse.move(x, y)
            self.human.sleep(self.human.rng.uniform(0.005, 0.03))
        self._pos = end

    def scroll(self, total_px):
        for delta, pause in self.human.scroll_plan(total_px):
            self.page.mouse.wheel(0, delta)
            self.human.sleep(pause)

    def dwell(self, text_len=0):
        self.human.sleep(self.human.dwell_seconds(text_len))
