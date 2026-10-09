"""ComicBOM Control: a small window to start and stop the two long jobs, image rendering and story writing (Codex).

    pythonw -m bom_comic.control          # the window (scripts/ComicBOM Control.cmd starts it)

Each job runs as its own background process, so it keeps going if the window is minimised. While a job started here
is running the window holds runs/book/control.lock, which keeps the scheduled nightly run from starting (the nightly
job exits when it sees it); the jobs started here set COMICBOM_CONTROL=1 and are exempt. Locks are released when the
jobs stop, and a lock left by a crash is ignored once its process is gone.

    Image rendering: bom_comic.nightly --until never --no-write. Stop asks it to finish the panel in progress and
        exit (it removes ComfyUI as it goes); a second Stop kills it.
    Story: repairs rejected scenes, re-reads chapters for story continuity, and writes chapter guides, over and over
        until nothing is left, waiting an hour when Codex's usage limit is hit. Stop ends it at once (each chapter's
        files are written whole, so a stop between writes loses only the call in progress).

    Reader: "Open reader" starts the draft reader (bom-comic --run runs/book read) if it is not already up and opens
        it in the browser. It does not use the control lock, and keeps running if the window closes.

    python -m bom_comic.control story-job     # the Story loop itself (the window starts this)
"""
import datetime
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from . import book

ROOT = Path("runs/book")
IMAGE_COMMAND = [sys.executable, "-u", "-m", "bom_comic.nightly", "--until", "never", "--no-write"]
STORY_COMMAND = [sys.executable, "-u", "-m", "bom_comic.control", "story-job"]
READER_PORT = 8765
READER_COMMAND = [sys.executable, "-c", "from bom_comic.cli import main; main()", "--run", "runs/book", "read",
                  "--port", str(READER_PORT), "--no-browser"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def alive(pid):
    """Whether pid is a running Python process (Windows reuses process IDs)."""
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return any(line.lower().startswith('"python') and f'"{pid}"' in line for line in out.splitlines())


def kill_tree(pid):
    """End a process and everything it started (ComfyUI is a child of the render job)."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)


def read_pid(path):
    try:
        return int(Path(path).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


class Controller:
    """The window's logic: which jobs run, starting them, stopping them, and the control lock."""

    def __init__(self, image_command=None, story_command=None, root=ROOT, reader_command=None,
                 reader_port=READER_PORT):
        self.root = Path(root)
        self.state = self.root / "control"
        self.control_lock = self.root / "control.lock"
        self.image_lock = self.root / "nightly" / "nightly.lock"
        self.stop_flag = self.root / "nightly" / "stop.flag"
        self.commands = {"image": image_command or IMAGE_COMMAND, "story": story_command or STORY_COMMAND}
        self.pid_files = {"image": self.state / "image.pid", "story": self.state / "story.pid"}
        self.logs = {"image": self.state / "image.log", "story": self.state / "story.log"}
        self.reader_command, self.reader_port = reader_command or READER_COMMAND, reader_port
        self.reader_pid_file, self.reader_log = self.state / "reader.pid", self.state / "reader.log"
        self.stopping = {"image": False, "story": False}
        self.state.mkdir(parents=True, exist_ok=True)

    def pid(self, job):
        """The running job's process ID: one this window started, or (for images) any nightly run holding its lock."""
        pid = read_pid(self.pid_files[job])
        if alive(pid):
            return pid
        if job == "image" and book.held(self.image_lock):
            return read_pid(self.image_lock)
        return 0

    def started_here(self, job):
        return alive(read_pid(self.pid_files[job]))

    def running(self, job):
        return bool(self.pid(job))

    def start(self, job):
        if self.running(job):
            raise RuntimeError(f"The {job} job is already running")
        self.stopping[job] = False
        if job == "image":
            self.stop_flag.unlink(missing_ok=True)
        log = open(self.logs[job], "a", encoding="utf-8")
        log.write(f"\n=== started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        log.flush()
        process = subprocess.Popen(self.commands[job], stdout=log, stderr=subprocess.STDOUT, cwd=Path.cwd(),
                                   env={**os.environ, "COMICBOM_CONTROL": "1", "PYTHONUNBUFFERED": "1"},
                                   creationflags=NO_WINDOW)
        self.pid_files[job].write_text(str(process.pid))
        self.sync_lock()
        return process.pid

    def stop(self, job):
        """First call: finish politely (images: the panel in progress; story: stop now). A second call while it is
        still stopping kills the job's whole process tree."""
        pid = self.pid(job)
        if not pid:
            self.stopping[job] = False
            return "not running"
        if job == "image" and not self.stopping[job]:
            self.stop_flag.parent.mkdir(parents=True, exist_ok=True)
            self.stop_flag.write_text(datetime.datetime.now().isoformat())
            self.stopping[job] = True
            return "stopping after the current panel"
        kill_tree(pid)
        self.stopping[job] = False
        self.cleanup()
        return "stopped"

    def cleanup(self):
        """Remove lock and pid files that belong to jobs that are gone, then refresh the control lock."""
        for job, path in self.pid_files.items():
            if path.exists() and not alive(read_pid(path)):
                path.unlink(missing_ok=True)
        if not book.held(self.image_lock):
            self.image_lock.unlink(missing_ok=True)
        if not book.held(self.root / "writing.lock"):
            (self.root / "writing.lock").unlink(missing_ok=True)
        for job in self.stopping:
            if not self.running(job):
                self.stopping[job] = False
        self.sync_lock()

    def sync_lock(self):
        """Hold runs/book/control.lock exactly while a job started here is running."""
        if any(self.started_here(job) for job in self.commands):
            if read_pid(self.control_lock) != os.getpid():
                self.control_lock.write_text(str(os.getpid()))
        else:
            self.control_lock.unlink(missing_ok=True)

    def status(self, job):
        """(label, running) for display."""
        pid = self.pid(job)
        if not pid:
            return "Idle", False
        if self.stopping[job]:
            return "Stopping after the current panel…" if job == "image" else "Stopping…", True
        return ("Running" if self.started_here(job) else "Running (started elsewhere, e.g. the nightly task)"), True

    def reader_up(self):
        """Whether a reader (this window's or any other) is answering on its port."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.reader_port}/", timeout=3):
                return True
        except OSError:
            return False

    def open_reader(self, browser=True, wait=20):
        """Start the reader if nothing answers on its port, then open it in the browser. Returns its address."""
        url = f"http://127.0.0.1:{self.reader_port}/"
        if not self.reader_up():
            log = open(self.reader_log, "a", encoding="utf-8")
            log.write(f"\n=== started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            log.flush()
            process = subprocess.Popen(self.reader_command, stdout=log, stderr=subprocess.STDOUT, cwd=Path.cwd(),
                                       creationflags=NO_WINDOW)
            self.reader_pid_file.write_text(str(process.pid))
            deadline = time.time() + wait
            while time.time() < deadline and not self.reader_up():
                time.sleep(0.3)
            if not self.reader_up():
                raise RuntimeError(f"The reader did not start; see {self.reader_log}")
        if browser:
            webbrowser.open(url)
        return url

    def stop_reader(self):
        """Stop a reader this window started (one started elsewhere has no process ID here)."""
        pid = read_pid(self.reader_pid_file)
        if pid and alive(pid):
            kill_tree(pid)
        self.reader_pid_file.unlink(missing_ok=True)

    def reader_status(self):
        if not self.reader_up():
            return "Stopped", False
        return ("Running" if alive(read_pid(self.reader_pid_file)) else "Running (started elsewhere)"), True

    def tail(self, job, lines=18):
        try:
            text = self.logs[job].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])


def story_job(root=ROOT):
    """Repair, continuity-check and write guides until nothing is left; wait an hour at a Codex usage limit."""
    def say(message):
        print(f"{datetime.datetime.now():%H:%M:%S} {message}", flush=True)
    while True:
        busy = False
        for name, step in (("repair", book.repair_blocked), ("continuity", book.check_continuity),
                           ("guides", book.write_intros)):
            say(f"Story: {name}")
            done, reason = step(str(root))
            say(f"Story: {name} finished ({len(done)} chapters); {reason}")
            if "usage limit" in reason:
                say("Codex usage limit reached; trying again in an hour")
                time.sleep(3600)
                busy = True
                break
            if "another writer" in reason:
                say("Another writer holds the lock; trying again in 5 minutes")
                time.sleep(300)
                busy = True
                break
        if not busy:
            say("Story: nothing left to do")
            return


def window():
    import ctypes
    import tkinter as tk
    from tkinter import messagebox, ttk
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on a scaled display
    except Exception:
        pass
    controller = Controller()
    app = tk.Tk()
    app.title("ComicBOM Control")
    app.geometry("760x640")
    frames, labels, buttons, logs = {}, {}, {}, {}
    titles = {"image": "Image rendering (ComfyUI)", "story": "Story (Codex)"}
    notes = {"image": "Draws flagged panels first, then new chapters in order, until you stop it.",
             "story": "Repairs scenes, checks story continuity, and writes chapter guides."}
    for job in ("image", "story"):
        box = ttk.LabelFrame(app, text=titles[job], padding=10)
        box.pack(fill="x", padx=12, pady=(12, 0))
        labels[job] = ttk.Label(box, text="Idle", font=("Segoe UI", 11, "bold"))
        labels[job].grid(row=0, column=0, sticky="w")
        ttk.Label(box, text=notes[job]).grid(row=1, column=0, sticky="w")
        buttons[job] = (ttk.Button(box, text="Start"), ttk.Button(box, text="Stop"))
        buttons[job][0].grid(row=0, column=1, rowspan=2, padx=(20, 4))
        buttons[job][1].grid(row=0, column=2, rowspan=2)
        box.columnconfigure(0, weight=1)
    reader_box = ttk.LabelFrame(app, text="Reader (review and flag panels)", padding=10)
    reader_box.pack(fill="x", padx=12, pady=(12, 0))
    reader_label = ttk.Label(reader_box, text="Stopped", font=("Segoe UI", 11, "bold"))
    reader_label.grid(row=0, column=0, sticky="w")
    ttk.Label(reader_box, text="Opens the draft reader in your browser, starting it first if needed.").grid(
        row=1, column=0, sticky="w")
    reader_open = ttk.Button(reader_box, text="Open reader")
    reader_open.grid(row=0, column=1, rowspan=2, padx=(20, 4))
    reader_stop = ttk.Button(reader_box, text="Stop")
    reader_stop.grid(row=0, column=2, rowspan=2)
    reader_box.columnconfigure(0, weight=1)

    def open_reader():
        reader_open.configure(state="disabled", text="Starting…")
        app.update_idletasks()
        try:
            controller.open_reader()
        except RuntimeError as exc:
            messagebox.showinfo("ComicBOM Control", str(exc))
        reader_open.configure(text="Open reader")
        refresh()

    reader_open.configure(command=open_reader)
    reader_stop.configure(command=lambda: (controller.stop_reader(), refresh()))
    book_note = ttk.Label(app, text="", foreground="#666666")
    book_note.pack(anchor="w", padx=14, pady=(8, 0))
    notebook = ttk.Notebook(app)
    notebook.pack(fill="both", expand=True, padx=12, pady=8)
    for job in ("image", "story"):
        text = tk.Text(notebook, height=12, wrap="none", state="disabled", font=("Consolas", 9))
        notebook.add(text, text=f"{job.title()} log")
        logs[job] = text

    def start(job):
        try:
            controller.start(job)
        except RuntimeError as exc:
            messagebox.showinfo("ComicBOM Control", str(exc))
        refresh()

    def stop(job):
        if controller.stopping[job] and not messagebox.askyesno(
                "Force stop", "Kill the job now? A drawing in progress will be lost."):
            return
        controller.stop(job)
        refresh()

    for job in ("image", "story"):
        buttons[job][0].configure(command=lambda j=job: start(j))
        buttons[job][1].configure(command=lambda j=job: stop(j))

    def refresh():
        controller.cleanup()
        for job in ("image", "story"):
            label, running = controller.status(job)
            labels[job].configure(text=label)
            buttons[job][0].configure(state="disabled" if running else "normal")
            buttons[job][1].configure(state="normal" if running else "disabled",
                                      text="Force stop" if controller.stopping[job] else "Stop")
            text = controller.tail(job)
            logs[job].configure(state="normal")
            logs[job].delete("1.0", "end")
            logs[job].insert("end", text)
            logs[job].see("end")
            logs[job].configure(state="disabled")
        label, up = controller.reader_status()
        reader_label.configure(text=label)
        reader_open.configure(state="normal")
        reader_stop.configure(state="normal" if up and label == "Running" else "disabled")
        book_note.configure(text="The scheduled nightly run is blocked while a job started here is running."
                            if controller.control_lock.exists() else "The scheduled nightly run starts at 23:00 when nothing is running.")
        app.after(3000, refresh)

    def close():
        if any(controller.started_here(job) for job in ("image", "story")):
            if not messagebox.askyesno("ComicBOM Control", "Jobs started here are still running. Stop them and exit?\n"
                                       "(Image rendering finishes the panel it is drawing first.)"):
                return
            for job in ("image", "story"):
                if controller.started_here(job):
                    controller.stop(job)
            app.after(500, wait_then_exit)
            return
        controller.cleanup()
        app.destroy()

    def wait_then_exit(tries=[0]):
        tries[0] += 1
        controller.cleanup()
        if (not any(controller.started_here(job) for job in ("image", "story"))) or tries[0] > 600:
            controller.control_lock.unlink(missing_ok=True)
            app.destroy()
        else:
            for job in ("image", "story"):
                labels[job].configure(text="Stopping before exit…")
            app.after(1000, wait_then_exit)

    app.protocol("WM_DELETE_WINDOW", close)
    refresh()
    app.mainloop()


def main():
    os.chdir(Path(__file__).resolve().parent.parent)  # the project folder, wherever the shortcut starts
    if sys.argv[1:] == ["story-job"]:
        story_job()
    else:
        window()


if __name__ == "__main__":
    main()
