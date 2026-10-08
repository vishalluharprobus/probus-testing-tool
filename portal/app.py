"""
The Studio's web server: the page, a small JSON API, and the run files.

    GET  /                         the Studio (portal/static)
    GET  /api/meta                 servers, products, tests, levels, companies
    GET  /api/health               is each server up; is there a live login
    GET  /api/vehicles?product&q   MMV search
    GET  /api/rtos?q               RTO search
    POST /api/plan                 what a quote sweep would run (no browser)
    POST /api/runs                 start a run (it joins the queue)
    GET  /api/runs                 every run, newest first
    GET  /api/runs/{id}            one run, with its files
    GET  /api/runs/{id}/log        its output, live (server-sent events)
    GET  /api/runs/{id}/results    its results.csv as rows, with a summary
    POST /api/runs/{id}/stop       stop it (and its browsers)
    *    /api/live-login/...       log in to the live site from the browser
    GET  /files/<path>             a file under reports/

An optional access code ("portal_access_code" in config/settings.local.json)
keeps the Studio to people who know it: they type it once, the browser keeps
a cookie.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from portal import catalog, health, jobs, livelogin, recipes

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "studio_access"
PLAN_TIMEOUT = 120


def _local_settings() -> dict:
    try:
        return json.loads(settings.LOCAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def create_app(queue: jobs.Queue | None = None, access_code: str | None = None) -> FastAPI:
    local = _local_settings()
    code = access_code if access_code is not None else str(local.get("portal_access_code") or "")
    token = hashlib.sha256(f"studio:{code}".encode()).hexdigest()[:32] if code else ""
    q = queue or jobs.Queue(max_parallel=int(local.get("portal_max_parallel") or 2))
    login = livelogin.LiveLogin()
    plan_cache: dict[str, tuple[float, dict]] = {}
    plan_lock = threading.Lock()

    app = FastAPI(title="Probus Test Studio", docs_url=None, redoc_url=None)
    app.state.queue = q

    threading.Thread(target=catalog.refresh_rtos, daemon=True).start()

    # ------------------------------------------------------------ access
    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        open_paths = ("/api/unlock", "/api/meta")
        if token and (path.startswith("/api/") or path.startswith("/files/")) \
                and path not in open_paths and request.cookies.get(COOKIE) != token:
            return JSONResponse({"error": "locked"}, status_code=401)
        return await call_next(request)

    @app.post("/api/unlock")
    async def unlock(request: Request):
        body = await _json(request)
        if not token:
            return {"ok": True}
        if str(body.get("code") or "") != code:
            raise HTTPException(403, "That is not the access code.")
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=180 * 86400, httponly=True,
                            samesite="lax")
        return response

    # -------------------------------------------------------------- meta
    @app.get("/api/meta")
    def meta(request: Request):
        return {
            "locked": bool(token) and request.cookies.get(COOKIE) != token,
            "targets": [{"id": k, **v, "live": settings.TARGETS[k].live,
                         "url": settings.TARGETS[k].base_url}
                        for k, v in recipes.TARGETS.items()],
            "products": [{"id": k, "label": v} for k, v in recipes.PRODUCTS.items()],
            "tests": [{"id": k, "label": v["label"], "blurb": v["blurb"],
                       "targets": list(v["targets"]), "uses": list(v["uses"])}
                      for k, v in recipes.TESTS.items()],
            "levels": [{"id": k, "label": v["label"], "blurb": v["blurb"]}
                       for k, v in recipes.LEVELS.items()],
            "stages": [{"id": k, "label": v["label"]} for k, v in recipes.STAGES.items()],
            "speeds": [{"id": k, "label": v["label"], "blurb": v["blurb"]}
                       for k, v in recipes.SPEEDS.items()],
            "insurers": catalog.insurers(),
            "presets": catalog.PRESETS,
            "choices": {
                "policy": [recipes.matrix.CP, recipes.matrix.TP, recipes.matrix.OD],
                "previous": list(recipes.matrix.PREVIOUS),
                "ncb": list(recipes.matrix.NCB_VALUES),
                "claim": ["No", "Yes"],
                "this_year": recipes.matrix.this_year(),
                "rules": {p: {"od_max_age": r.od_max_age, "cp_max_age": r.cp_max_age}
                          for p, r in recipes.matrix.RULES.items()},
            },
            "max_parallel": q.max_parallel,
        }

    @app.get("/api/health")
    def get_health(force: bool = False):
        return health.servers(force=force)

    @app.get("/api/vehicles")
    def get_vehicles(product: str = "bike", q: str = "", limit: int = 40):
        if product not in recipes.PRODUCTS:
            raise HTTPException(400, "unknown product")
        return catalog.vehicles(product, q, max(1, min(limit, 100)))

    @app.get("/api/rtos")
    def get_rtos(q: str = "", limit: int = 40):
        return catalog.rtos(q, max(1, min(limit, 100)))

    # -------------------------------------------------------------- plan
    @app.post("/api/plan")
    async def plan(request: Request):
        form = await _json(request)
        recipe = _recipe(form)
        if not recipe.plan_command:
            return {"ok": True, "journeys": None, "command": recipes.shown(recipe.command)}
        key = json.dumps(recipe.plan_command)
        with plan_lock:
            hit = plan_cache.get(key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        result = await asyncio.to_thread(_run_plan, recipe.plan_command)
        result["command"] = recipes.shown(recipe.command)
        with plan_lock:
            plan_cache[key] = (time.time(), result)
        return result

    # -------------------------------------------------------------- runs
    @app.post("/api/runs")
    async def start(request: Request):
        form = await _json(request)
        recipe = _recipe(form)
        if recipe.target == "live" and form.get("confirm") != "LIVE":
            raise HTTPException(400, "Type LIVE to confirm a run on the live site.")
        job = q.submit(recipe, str(form.get("who") or ""), _public_form(form))
        return _job(job)

    @app.get("/api/runs")
    def list_runs(limit: int = 100):
        return {"runs": [_job(j, files=False) for j in q.list()[:max(1, min(limit, 500))]],
                "max_parallel": q.max_parallel}

    @app.get("/api/runs/{job_id}")
    def get_run(job_id: str):
        return _job(_find(job_id))

    @app.post("/api/runs/{job_id}/stop")
    def stop_run(job_id: str):
        if not q.stop(_find(job_id).id):
            raise HTTPException(409, "This run is not going.")
        return {"ok": True}

    @app.post("/api/runs/{job_id}/again")
    async def run_again(job_id: str, request: Request):
        old = _find(job_id)
        body = await _json(request)
        recipe = _recipe(dict(old.form))
        if recipe.target == "live" and body.get("confirm") != "LIVE":
            raise HTTPException(400, "Type LIVE to confirm a run on the live site.")
        who = str(body.get("who") or old.who)
        return _job(q.submit(recipe, who, _public_form(old.form)))

    @app.get("/api/runs/{job_id}/log")
    async def log(job_id: str, request: Request, offset: int = 0):
        job = _find(job_id)

        async def events():
            pos = max(0, offset)
            last_beat = 0.0
            while True:
                if await request.is_disconnected():
                    return
                chunk = ""
                if job.log.exists():
                    with job.log.open("rb") as fh:
                        fh.seek(pos)
                        data = fh.read(256_000)
                    if data:
                        # Only whole lines; the rest waits for the next read.
                        cut = data.rfind(b"\n") + 1
                        if cut:
                            chunk = data[:cut].decode("utf-8", errors="replace")
                            pos += cut
                state = job.public()
                if chunk or time.time() - last_beat > 2:
                    payload = {"text": chunk, "offset": pos,
                               "status": state["status"], "outcome": state["outcome"],
                               "done": state["done"], "total": state["total"],
                               "eta": state["eta"], "seconds": state["seconds"],
                               "headline": state["headline"]}
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_beat = time.time()
                if job.status not in jobs.ACTIVE and not chunk:
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.6)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{job_id}/results")
    def results(job_id: str, limit: int = 3000):
        path = jobs.results_csv(_find(job_id))
        if not path:
            return {"columns": [], "rows": [], "summary": [], "file": None}
        return read_results(path, limit)

    # -------------------------------------------------------- live login
    @app.get("/api/live-login")
    def live_status():
        return login.status()

    @app.post("/api/live-login/start")
    async def live_start(request: Request):
        body = await _json(request)
        return login.start(str(body.get("who") or ""))

    @app.post("/api/live-login/credentials")
    async def live_credentials(request: Request):
        body = await _json(request)
        if not body.get("username") or not body.get("password"):
            raise HTTPException(400, "Type the username and the password.")
        return login.credentials(str(body["username"]), str(body["password"]))

    @app.post("/api/live-login/otp")
    async def live_otp(request: Request):
        body = await _json(request)
        if not str(body.get("otp") or "").strip():
            raise HTTPException(400, "Type the OTP.")
        return login.otp(str(body["otp"]).strip())

    @app.post("/api/live-login/cancel")
    def live_cancel():
        login.cancel()
        return login.status()

    # -------------------------------------------------------------- files
    @app.get("/files/{rel:path}")
    def files(rel: str, download: bool = False):
        path = (jobs.REPORTS / rel).resolve()
        if not jobs._inside(path, jobs.REPORTS) or not path.is_file():
            raise HTTPException(404, "No such file.")
        kind = {".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                ".log": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                ".webm": "video/webm"}.get(path.suffix.lower())
        return FileResponse(path, media_type=kind,
                            filename=path.name if download else None)

    # ------------------------------------------------------------ helpers
    def _recipe(form: dict) -> recipes.Recipe:
        try:
            return recipes.build(form, {i["code"] for i in catalog.insurers()})
        except recipes.Invalid as exc:
            raise HTTPException(400, str(exc))

    def _find(job_id: str) -> jobs.Job:
        job = q.get(job_id)
        if not job:
            raise HTTPException(404, "No such run.")
        return job

    def _job(job: jobs.Job, files: bool = True) -> dict:
        data = job.public()
        data["position"] = q.position(job) if job.status == "queued" else 0
        if files:
            data["files"] = jobs.files_of(job)
        return data

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _public_form(form: dict) -> dict:
    keep = ("test", "target", "product", "insurers", "vehicles", "rtos", "policy", "speed",
            "years", "previous", "ncb", "claim", "level", "journeys", "stage", "watch")
    return {k: form.get(k) for k in keep if form.get(k) not in (None, "", [])}


def _run_plan(command: list[str]) -> dict:
    try:
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=PLAN_TIMEOUT,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The planner took too long - try fewer choices."}
    return recipes.read_plan(done.stdout + "\n" + done.stderr)


def read_results(path: Path, limit: int = 3000) -> dict:
    """A results.csv as rows, plus Success/Failure per company."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    columns = list(rows[0].keys()) if rows else []
    company = next((c for c in ("insurer", "company", "Company", "Insurer")
                    if c in columns), None)
    status = next((c for c in ("outcome", "status", "Status", "quote", "Quote")
                   if c in columns and any(r.get(c) in ("Success", "Failure")
                                           for r in rows)), None)
    summary: dict[str, dict] = {}
    if company and status:
        for r in rows:
            name = r.get(company) or "?"
            s = summary.setdefault(name, {"company": name, "success": 0, "failure": 0,
                                          "cheapest": None})
            if r.get(status) == "Success":
                s["success"] += 1
                try:
                    price = float(r.get("premium") or 0)
                    if price and (s["cheapest"] is None or price < s["cheapest"]):
                        s["cheapest"] = price
                except ValueError:
                    pass
            elif r.get(status) == "Failure":
                s["failure"] += 1
    return {"columns": columns, "rows": rows[:limit], "total": len(rows),
            "company_column": company, "status_column": status,
            "summary": sorted(summary.values(),
                              key=lambda s: (-s["success"], s["failure"], s["company"])),
            "findings": read_findings(path.parent / "findings.txt"),
            "file": path.relative_to(jobs.REPORTS).as_posix()}


FINDING = re.compile(r"^(DEFECT|LOOK)\s+(\S+)\s+(.*?)\s*(?:\[([^\]]*)\])?\s*$")


def read_findings(path: Path) -> list[dict]:
    """findings.txt (core/matrixreport.py) as rows: Bug / Worth a look, the
    company, what was found, why it matters, and on which journeys."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = FINDING.match(line)
        if m:
            out.append({"kind": "bug" if m.group(1) == "DEFECT" else "look",
                        "company": m.group(2), "title": m.group(3).strip(),
                        "journeys": m.group(4) or "", "detail": ""})
        elif out and line.startswith("        ") and not out[-1]["detail"]:
            out[-1]["detail"] = line.strip()
        elif line and not line.startswith(" ") and out:
            break                         # the next section (TWIN RULES ...)
    return out
