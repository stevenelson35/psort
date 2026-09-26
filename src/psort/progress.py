"""Progress for `psort run`: which step, how far through it, how far through the whole run, and a
heartbeat so it's always clear psort is alive.

In a terminal it's one line, redrawn in place, with a spinner that keeps turning even while psort
waits on a slow disk:
    ⠹ [1/6] Scanning inbox  3,412 / 7,512 files  45%  ·  overall 23% · 4m12s elapsed, about 11m left
When output goes to a file or another program, it prints a line every 10%, plus a "still working"
line if nothing has been printed for 30 seconds.
"""

import shutil
import sys
import threading
import time
from dataclasses import dataclass

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
HEARTBEAT_TTY = 0.1  # seconds between spinner frames
HEARTBEAT_LOG = 30.0  # seconds of silence before a "still working" line when not in a terminal


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
    def __init__(self, stages: list[Stage], stream=None, tty: bool | None = None, heartbeat: bool = True):
        self.stages = stages
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty() if tty is None else tty
        self.started = time.monotonic()
        self.current = -1
        self.fraction = 0.0
        self._detail = ""
        self._line = ""
        self._frame = 0
        self._last_draw = 0.0
        self._last_output = time.monotonic()
        self._last_decile = -1
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        if heartbeat:
            self._thread = threading.Thread(target=self._beat, daemon=True)
            self._thread.start()

    # ---- driving it ----

    def start(self, index: int, detail: str = "") -> None:
        with self._lock:
            self.current, self.fraction, self._last_decile = index, 0.0, -1
            self._draw(detail or "starting…", force=True)

    def set_weight(self, index: int, weight: float) -> None:
        """Refine a later step's estimate once its size is known (e.g. photos to scan for faces)."""
        self.stages[index].weight = max(weight, 0.1)

    def update(self, done: int, total: int, unit: str = "") -> None:
        with self._lock:
            self.fraction = done / total if total else 1.0
            self._draw(f"{done:,} / {total:,}{(' ' + unit) if unit else ''}")

    def note(self, detail: str) -> None:
        """Show what the current step is doing when there's no total yet (e.g. 'found 4,200 files')."""
        with self._lock:
            self._draw(detail, force=not self.tty)

    def finish(self, index: int, summary: str = "") -> None:
        with self._lock:
            self.current, self.fraction = index, 1.0
            self._clear()
            stage = self.stages[index]
            self._write(f"[{index + 1}/{len(self.stages)}] {stage.name}: done"
                        f"{(' · ' + summary) if summary else ''}  ({self._overall_text()})\n")
            self._detail = ""

    def echo(self, message: str) -> None:
        """Print a normal line without garbling the progress line."""
        with self._lock:
            line = self._line
            self._clear()
            self._write(message + "\n")
            if self.tty and line:
                self._line = line
                self._write(line)

    def done(self) -> None:
        self.close()
        with self._lock:
            self._clear()
            self._write(f"Finished in {_clock(time.monotonic() - self.started)}.\n")

    def close(self) -> None:
        """Stop the heartbeat (also when a run is interrupted)."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        with self._lock:
            self._clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

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

    def _write(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()
        self._last_output = time.monotonic()

    def _status(self) -> str:
        stage = self.stages[self.current]
        return (f"[{self.current + 1}/{len(self.stages)}] {stage.name}  {self._detail}  {self.fraction:.0%}"
                f"  ·  {self._overall_text()}")

    def _draw(self, detail: str, force: bool = False) -> None:
        self._detail = detail
        now = time.monotonic()
        if self.tty:
            if not force and now - self._last_draw < 0.1:
                return
            self._render()
        else:
            decile = int(self.fraction * 10)
            if force or decile > self._last_decile:
                self._last_decile = decile
                self._write(self._status() + "\n")

    def _render(self) -> None:
        width = shutil.get_terminal_size((120, 20)).columns - 1
        text = f"{SPINNER[self._frame % len(SPINNER)]} {self._status()}"[:width]
        pad = max(0, len(self._line) - 1 - len(text))  # wipe leftovers from a longer previous line
        self._line = "\r" + text + " " * pad
        self._write(self._line)
        self._last_draw = time.monotonic()

    def _clear(self) -> None:
        if self.tty and self._line:
            width = shutil.get_terminal_size((120, 20)).columns - 1
            self.stream.write("\r" + " " * width + "\r")
            self.stream.flush()
            self._line = ""

    def _beat(self) -> None:
        """Keep the spinner turning (or say 'still working') while the work itself is busy."""
        interval = HEARTBEAT_TTY if self.tty else 1.0
        while not self._stop.wait(interval):
            with self._lock:
                if self.current < 0 or not self._detail:
                    continue
                if self.tty:
                    self._frame += 1
                    self._render()
                elif time.monotonic() - self._last_output >= HEARTBEAT_LOG:
                    self._write(f"  … still working on {self.stages[self.current].name.lower()} "
                                f"({_clock(time.monotonic() - self.started)} elapsed)\n")
