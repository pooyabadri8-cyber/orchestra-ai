import os
import subprocess
import sys
import time

from orchestra.cli import _single_instance, _take_over

HOLD = "import sys,time; sys.path.insert(0, '.'); from orchestra.cli import _single_instance; from pathlib import Path; " \
       "l=_single_instance(Path(sys.argv[1])); print('held', flush=True); time.sleep(60)"


def _holder(lock):
    p = subprocess.Popen([sys.executable, "-c", HOLD, str(lock)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_second_copy_stops_the_first_when_owner_agrees(tmp_path):
    lock, pid = tmp_path / "bot.lock", tmp_path / "bot.pid"
    old = _holder(lock)
    pid.write_text(str(old.pid))
    assert _single_instance(lock) is None
    got = _take_over(lock, pid, ask=lambda _: "")
    assert got is not None
    old.wait(timeout=10)


def test_owner_says_no(tmp_path):
    lock, pid = tmp_path / "bot.lock", tmp_path / "bot.pid"
    old = _holder(lock)
    pid.write_text(str(old.pid))
    killed = []
    assert _take_over(lock, pid, ask=lambda _: "n", kill=killed.append) is None and killed == []
    old.kill()


def test_no_pid_file_nothing_to_stop(tmp_path):
    assert _take_over(tmp_path / "x.lock", tmp_path / "none.pid", ask=lambda _: "") is None
