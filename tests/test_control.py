"""ComicBOM Control: starting and stopping the two jobs, the control lock that keeps the nightly run out, and the
polite stop flag. The jobs here are stand-in sleeping processes, not renders."""
import datetime
import os
import subprocess
import sys
import time
import pytest
from bom_comic import book, nightly
from bom_comic.control import Controller, alive, read_pid

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process handling")
SLEEP = [sys.executable, "-c", "import time; time.sleep(120)"]


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = Controller(image_command=SLEEP, story_command=SLEEP, root=tmp_path / "runs" / "book")
    yield c
    for job in ("image", "story"):
        pid = read_pid(c.pid_files[job])
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def held_elsewhere(path):
    """What a separate process (the scheduled nightly run) sees of a lock."""
    code = f"from bom_comic import book; print(book.held({str(path)!r}))"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip() == "True"


def test_a_running_job_holds_the_control_lock_until_both_stop(control):
    assert control.status("image") == ("Idle", False) and not control.control_lock.exists()
    image, story = control.start("image"), control.start("story")
    assert alive(image) and alive(story) and control.status("image") == ("Running", True)
    # The scheduled nightly run (another process) sees the lock and stays out.
    assert control.control_lock.exists() and held_elsewhere(control.control_lock)
    with pytest.raises(RuntimeError):
        control.start("image")
    assert control.stop("story") == "stopped"
    time.sleep(1)
    control.cleanup()
    assert not alive(story) and control.control_lock.exists()  # the render still runs
    # Image rendering stops politely first: a flag the render checks between panels; a second stop kills it.
    assert control.stop("image") == "stopping after the current panel" and control.stop_flag.exists()
    assert control.status("image")[0].startswith("Stopping")
    assert control.stop("image") == "stopped"
    time.sleep(1)
    control.cleanup()
    assert not alive(image) and not control.control_lock.exists() and not control.pid_files["image"].exists()
    assert control.stop("image") == "not running"


def test_a_stale_lock_from_a_crashed_window_does_not_block_the_nightly_run(control):
    control.control_lock.write_text("999999")  # no such process
    assert not held_elsewhere(control.control_lock)


def test_the_render_stops_for_a_flag_made_after_it_started_not_for_an_old_one(tmp_path, monkeypatch):
    flag = tmp_path / "stop.flag"
    monkeypatch.setattr(nightly, "STOP_FLAG", flag)
    later = datetime.datetime.now() + datetime.timedelta(hours=1)
    assert not nightly.past(later)  # no flag
    flag.write_text("x")
    old = nightly._STARTED - 3600
    os.utime(flag, (old, old))
    assert not nightly.past(later)  # left from an earlier stop
    os.utime(flag, None)
    assert nightly.past(later)  # asked for since this run started
    assert nightly.past(datetime.datetime.now() - datetime.timedelta(seconds=1))  # the deadline still works


def test_the_reader_button_starts_a_reader_once_and_stops_it(control, monkeypatch):
    import socket
    import bom_comic.control as module
    monkeypatch.setattr(module.webbrowser, "open", lambda url: opened.append(url))
    opened = []
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # A stand-in reader: any web server.
    server = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
    c = Controller(root=control.root, reader_command=server, reader_port=port)
    assert c.reader_status() == ("Stopped", False)
    url = c.open_reader()
    assert url == f"http://127.0.0.1:{port}/" and opened == [url] and c.reader_status() == ("Running", True)
    first = read_pid(c.reader_pid_file)
    c.open_reader()  # already up: only the browser opens again, no second server
    assert len(opened) == 2 and read_pid(c.reader_pid_file) == first
    c.stop_reader()
    time.sleep(1)
    assert c.reader_status() == ("Stopped", False) and not c.reader_pid_file.exists()
