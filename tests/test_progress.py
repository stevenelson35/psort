import io

from psort.progress import Progress, Stage


def test_line_mode_prints_steps_and_tenths():
    out = io.StringIO()
    p = Progress([Stage("Scanning inbox", 90), Stage("Scoring", 10)], stream=out, tty=False, heartbeat=False)
    p.start(0, "100 files")
    for i in range(101):
        p.update(i, 100, "files")
    p.finish(0, "done here")
    p.start(1)
    p.update(1, 2)
    assert abs(p.overall() - 0.95) < 1e-9  # 90 of 100 done, plus half of the last 10
    p.finish(1)
    p.done()
    text = out.getvalue()
    assert "[1/2] Scanning inbox  100 files" in text
    assert "[1/2] Scanning inbox  50 / 100 files  50%" in text
    assert text.count("[1/2] Scanning inbox  ") == 11  # start (0%), then 10%,…,100%
    assert "[1/2] Scanning inbox: done · done here  (overall 90%" in text
    assert "[2/2] Scoring: done" in text and "Finished in" in text


def test_terminal_mode_redraws_one_line_and_keeps_messages_clean():
    out = io.StringIO()
    p = Progress([Stage("Arranging library", 1)], stream=out, tty=True, heartbeat=False)
    p.start(0)
    p.update(3, 10)
    p.echo("  move a → b")
    p.finish(0)
    text = out.getvalue()
    assert "\r⠋ [1/1] Arranging library" in text
    assert "\r  move a → b\n" in text  # the progress line is cleared, then the message gets its own line
    assert "[1/1] Arranging library: done" in text


def test_run_shows_steps(psort):
    out = psort("run").output
    for step in ["[1/6] Scanning inbox", "[2/6] Grouping moments", "[3/6] Scoring", "[4/6] Arranging library",
                 "[5/6] Finding faces", "[6/6] Updating highlights"]:
        assert step in out, step
    assert "overall 100%" in out and "Finished in" in out
    assert "[4/4] Arranging library" in psort("run", "--dry-run").output  # dry run skips faces/highlights


def test_spinner_keeps_turning_while_work_is_busy():
    import time

    out = io.StringIO()
    p = Progress([Stage("Scanning inbox", 1)], stream=out, tty=True)
    p.start(0, "looking at the inbox…")
    time.sleep(0.45)  # the work is stuck on something slow; the heartbeat still animates
    p.close()
    frames = {ch for ch in out.getvalue() if ch in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"}
    assert len(frames) >= 3


def test_still_working_line_when_output_is_not_a_terminal(monkeypatch):
    import time

    import psort.progress as progress_mod

    monkeypatch.setattr(progress_mod, "HEARTBEAT_LOG", 0.5)
    out = io.StringIO()
    p = Progress([Stage("Finding faces", 1)], stream=out, tty=False)
    p.start(0, "300 photos to scan")
    time.sleep(1.6)
    p.close()
    assert "… still working on finding faces" in out.getvalue()


def test_first_line_appears_before_the_inbox_is_listed(psort):
    out = psort("run").output
    assert out.index("looking at the inbox") < out.index("Ingest:")
