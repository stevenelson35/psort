"""Progress for `psort run`: which step, how far through it, and how far through the whole run.

In a terminal it's one line, redrawn in place:
    [1/6] Scanning inbox   3,412 / 7,512  45%  ·  overall 23%  ·  4m12s elapsed, about 11m left
When output goes to a file or another program, it prints a line every 10% instead.
"""

import shutil
import sys
import time
from dataclasses import dataclass


@dataclass
class Stage:
    name: str
    weight: float  # estimated seconds; only the proportions matter


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02}m"


class Progress:
    def __init__(self, stages: list[Stage], stream=None, tty: bool | None = None):
        self.stages = stages
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty() if tty is None else tty
        self.started = time.monotonic()
        self.current = -1
        self.fraction = 0.0
        self._line = ""
        self._last_draw = 0.0
        self._last_decile = -1

    # ---- driving it ----

    def start(self, index: int, detail: str = "") -> None:
        self.current, self.fraction, self._last_decile = index, 0.0, -1
        self._draw(detail or "starting…", force=True)

    def set_weight(self, index: int, weight: float) -> None:
        """Refine a later step's estimate once its size is known (e.g. photos to scan for faces)."""
        self.stages[index].weight = max(weight, 0.1)

    def update(self, done: int, total: int, unit: str = "") -> None:
        self.fraction = done / total if total else 1.0
        counts = f"{done:,} / {total:,}{(' ' + unit) if unit else ''}"
        self._draw(counts)

    def finish(self, index: int, summary: str = "") -> None:
        self.current, self.fraction = index, 1.0
        self._clear()
        stage = self.stages[index]
        self.stream.write(f"[{index + 1}/{len(self.stages)}] {stage.name}: done"
                          f"{(' · ' + summary) if summary else ''}  ({self._overall_text()})\n")
        self.stream.flush()

    def echo(self, message: str) -> None:
        """Print a normal line without garbling the progress line."""
        self._clear()
        self.stream.write(message + "\n")
        self.stream.flush()
        if self.tty and self._line:
            self.stream.write(self._line)
            self.stream.flush()

    def done(self) -> None:
        self._clear()
        self.stream.write(f"Finished in {_clock(time.monotonic() - self.started)}.\n")
        self.stream.flush()

    # ---- numbers ----

    def overall(self) -> float:
        total = sum(s.weight for s in self.stages) or 1.0
        before = sum(s.weight for s in self.stages[: max(self.current, 0)])
        here = self.stages[self.current].weight * self.fraction if self.current >= 0 else 0.0
        return min(1.0, (before + here) / total)

    def _overall_text(self) -> str:
        elapsed = time.monotonic() - self.started
        frac = self.overall()
        text = f"overall {frac:.0%} · {_clock(elapsed)} elapsed"
        if 0.03 < frac < 1.0 and elapsed > 5:
            text += f", about {_clock(elapsed / frac - elapsed)} left"
        return text

    # ---- output ----

    def _draw(self, detail: str, force: bool = False) -> None:
        stage = self.stages[self.current]
        pct = f"{self.fraction:.0%}"
        line = f"[{self.current + 1}/{len(self.stages)}] {stage.name}  {detail}  {pct}  ·  {self._overall_text()}"
        now = time.monotonic()
        if self.tty:
            if not force and now - self._last_draw < 0.2:
                return
            width = shutil.get_terminal_size((120, 20)).columns - 1
            self._line = "\r" + line[:width].ljust(min(width, len(self._line) - 1 if self._line else 0))
            self.stream.write(self._line)
            self.stream.flush()
            self._last_draw = now
        else:
            decile = int(self.fraction * 10)
            if force or decile > self._last_decile:
                self._last_decile = decile
                self.stream.write(line + "\n")
                self.stream.flush()

    def _clear(self) -> None:
        if self.tty and self._line:
            width = shutil.get_terminal_size((120, 20)).columns - 1
            self.stream.write("\r" + " " * width + "\r")
            self._line = ""
