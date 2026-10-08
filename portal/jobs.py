"""
The team's run queue: start runs, watch them, stop them, remember them.

One queue for everybody using the Studio, on the computer that hosts it:
  * at most `portal_max_parallel` runs at once (settings.local.json, default 2)
    - each run is a real browser, and the environments are shared;
  * runs that write the same notebook never overlap (Recipe.lock) - two quote
    sweeps on the same product would otherwise overwrite each other's lessons;
  * each run gets reports/portal/<run id>/: its output.log, and (through
    PROBUS_RUN_FOLDER, core/browser.py) its videos, screenshots and reports;
  * the list survives a restart (reports/portal/runs.json). A run that was
    going when the Studio stopped is marked "interrupted", never "passed".
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from portal import recipes

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
STUDIO = REPORTS / "portal"
STORE = STUDIO / "runs.json"

PROGRESS = re.compile(r"^\s*\[\s*(\d+)\s*/\s*(\d+)\]")
# Lines worth showing as "what it is doing now" in the run list.
HEADLINE = re.compile(r"^\s*\[\s*\d+\s*/\s*\d+\]\s*(.+)$")
# Report paths the runners print at the end ("Report   : reports\...").
PRINTED_PATH = re.compile(r"(?:Report|Excel|Notebook|Saved|Results?)\s*:\s*(\S.+?)\s*$")

ACTIVE = ("queued", "running")


@dataclass
class Job:
    id: str
    who: str
    form: dict
    test: str
    target: str
    product: str
    title: str
    command: list
    lock: str
    status: str = "queued"          # queued running passed bugs failed stopped interrupted
    outcome: str = "Waiting for a free slot"
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    exit_code: int | None = None
    done: int = 0
    total: int = 0
    headline: str = ""
    pid: int | None = None
    printed: list = field(default_factory=list)

    @property
    def folder(self) -> Path:
        return STUDIO / self.id

    @property
    def log(self) -> Path:
        return self.folder / "output.log"

    def public(self) -> dict:
        data = asdict(self)
        data.pop("pid", None)
        data["command_text"] = recipes.shown(self.command)
        data["seconds"] = round((self.finished or time.time()) - self.started) \
            if self.started else 0
        data["eta"] = self._eta()
        return data

    def _eta(self) -> int | None:
        if self.status != "running" or not self.started or not self.total or not self.done:
            return None
        per = (time.time() - self.started) / self.done
        return round(per * (self.total - self.done))


class Queue:
    def __init__(self, max_parallel: int = 2, store: Path = STORE):
        self.max_parallel = max(1, max_parallel)
        self.store = store
        self.jobs: dict[str, Job] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()
        self._wake = threading.Event()
        self._load()
        self._thread = threading.Thread(target=self._loop, name="studio-queue",
                                        daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            rows = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        for row in rows:
            try:
                job = Job(**{k: v for k, v in row.items()
                             if k in Job.__dataclass_fields__})
            except TypeError:
                continue
            if job.status in ACTIVE:
                job.status = "interrupted"
                job.outcome = "The Studio stopped while this run was going"
                job.finished = job.finished or time.time()
            self.jobs[job.id] = job

    def _save(self) -> None:
        with self.lock:
            rows = [asdict(j) for j in sorted(self.jobs.values(), key=lambda j: j.created)]
        self.store.parent.mkdir(parents=True, exist_ok=True)
        temp = self.store.with_suffix(".tmp")
        temp.write_text(json.dumps(rows[-500:], indent=1), encoding="utf-8")
        os.replace(temp, self.store)

    # ---------------------------------------------------------------- public
    def submit(self, recipe: recipes.Recipe, who: str, form: dict) -> Job:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        job = Job(id=f"{stamp}-{secrets.token_hex(2)}", who=(who or "someone")[:40],
                  form=form, test=recipe.test, target=recipe.target,
                  product=recipe.product, title=recipe.title,
                  command=recipe.command, lock=recipe.lock)
        job.folder.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.jobs[job.id] = job
        self._save()
        self._wake.set()
        return job

    def list(self) -> list[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def position(self, job: Job) -> int:
        """1 = next to start."""
        with self.lock:
            waiting = sorted((j for j in self.jobs.values() if j.status == "queued"),
                             key=lambda j: j.created)
        return next((i for i, j in enumerate(waiting, 1) if j.id == job.id), 0)

    def stop(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ACTIVE:
                return False
            if job.status == "queued":
                job.status, job.outcome = "stopped", "Cancelled before it started"
                job.finished = time.time()
                self._save()
                return True
            proc = self.procs.get(job_id)
            job.outcome = "Stopping ..."
        if proc:
            _kill_tree(proc)
        return True

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while True:
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self._start_what_fits()
            except Exception as exc:                 # noqa: BLE001 - keep serving
                print(f"[studio] queue error: {exc}", file=sys.stderr)

    def _start_what_fits(self) -> None:
        with self.lock:
            running = [j for j in self.jobs.values() if j.status == "running"]
            busy = {j.lock for j in running}
            waiting = sorted((j for j in self.jobs.values() if j.status == "queued"),
                             key=lambda j: j.created)
            for job in waiting:
                if len(running) >= self.max_parallel:
                    job.outcome = "Waiting for a free slot"
                    continue
                if job.lock in busy:
                    job.outcome = "Waiting: a run that writes the same notes is going"
                    continue
                self._launch(job)
                running.append(job)
                busy.add(job.lock)

    def _launch(self, job: Job) -> None:
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                    "PROBUS_RUN_FOLDER": str(job.folder)})
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        job.folder.mkdir(parents=True, exist_ok=True)
        log = job.log.open("w", encoding="utf-8")
        log.write(f"$ {recipes.shown(job.command)}\n\n")
        log.flush()
        try:
            proc = subprocess.Popen(job.command, cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1)
        except OSError as exc:
            log.write(f"Could not start: {exc}\n")
            log.close()
            job.status, job.outcome = "failed", f"Could not start: {exc}"
            job.finished = time.time()
            self._save()
            return
        job.status, job.outcome = "running", "Running"
        job.started, job.pid = time.time(), proc.pid
        self.procs[job.id] = proc
        self._save()
        threading.Thread(target=self._pump, args=(job, proc, log),
                         name=f"studio-{job.id}", daemon=True).start()

    def _pump(self, job: Job, proc: subprocess.Popen, log) -> None:
        last_save = time.time()
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                self._read_line(job, line.rstrip("\n"))
                if time.time() - last_save > 5:
                    self._save()
                    last_save = time.time()
            code = proc.wait()
        finally:
            log.close()
        with self.lock:
            self.procs.pop(job.id, None)
            job.exit_code, job.finished = code, time.time()
            if job.outcome == "Stopping ...":
                job.status, job.outcome = "stopped", "Stopped by hand"
            else:
                job.status, job.outcome = recipes.outcome(job.test, code)
            job.pid = None
        self._save()
        self._wake.set()

    def _read_line(self, job: Job, line: str) -> None:
        m = PROGRESS.match(line)
        if m:
            job.done = max(0, int(m.group(1)) - 1)     # the one starting now is not done
            job.total = int(m.group(2))
            head = HEADLINE.match(line)
            if head:
                job.headline = head.group(1).strip()[:160]
        elif job.total and "QUOTE MATRIX RESULTS" in line:
            job.done = job.total
        p = PRINTED_PATH.search(line)
        if p:
            job.printed = (job.printed + [p.group(1).strip()])[-12:]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the runner AND the browsers it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ------------------------------------------------------------ what a run made

MEDIA = {".webm": "video", ".mp4": "video", ".png": "image", ".jpg": "image",
         ".xlsx": "excel", ".csv": "csv", ".html": "report", ".txt": "text",
         ".zip": "trace", ".json": "data", ".log": "log"}


def files_of(job: Job) -> list[dict]:
    """Everything this run produced, as links under /files/."""
    seen: dict[Path, dict] = {}
    roots = [job.folder]
    for printed in job.printed:
        path = Path(printed)
        path = path if path.is_absolute() else ROOT / path
        if path.exists() and _inside(path, REPORTS):
            roots.append(path if path.is_dir() else path.parent)
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.name.startswith("~$") or path in seen:
                continue
            kind = MEDIA.get(path.suffix.lower())
            if not kind or kind in ("data",) and path.name != "quote-capture.json":
                continue
            rel = path.relative_to(REPORTS).as_posix()
            seen[path] = {"kind": kind, "name": path.name, "path": rel,
                          "url": f"/files/{rel}", "size": path.stat().st_size,
                          "group": path.parent.relative_to(REPORTS).as_posix(),
                          "modified": path.stat().st_mtime}
    return sorted(seen.values(), key=lambda f: (f["group"], f["name"]))


def results_csv(job: Job) -> Path | None:
    candidates = [Path(f["path"]) for f in files_of(job)
                  if f["name"] == "results.csv"]
    if not candidates:
        return None
    return max((REPORTS / c for c in candidates), key=lambda p: p.stat().st_mtime)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
