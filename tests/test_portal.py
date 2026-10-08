"""
Prove the Test Studio (portal/) - offline, without touching any real server.

    venv\\Scripts\\python -m pytest tests/test_portal.py -q

Each test is one promise the Studio makes to the team:
  * what you pick becomes exactly the runner command, and the live site
    takes the quote sweep only, with named companies, and a typed LIVE;
  * the queue runs, stops, remembers, and never overlaps runs that share notes;
  * pinned choices (vehicle, RTO, policy ...) are what the planner plans;
  * files outside reports/ can never be read; the access code locks the rest;
  * the live login relay types the password and OTP and saves the session.
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data import matrix  # noqa: E402
from portal import catalog, jobs, recipes  # noqa: E402

KNOWN = {"BAJAJ", "TATA", "DIGIT", "SBI", "KOTAK", "ICICI"}


# ================================================================ recipes

def build(**form):
    return recipes.build(form, KNOWN)


def test_sweep_becomes_the_matrix_command_with_every_pin():
    r = build(test="sweep", target="testsite", product="bike",
              insurers=["bajaj", "TATA"], vehicles=["HONDA|ACTIVA|3G (110 CC) (PETROL)"],
              rtos=["MH-12 Pune"], policy=["Third Party"], years=["2021"],
              level="full")
    cmd = r.command
    assert cmd[2] == "run_quote_matrix.py"
    assert cmd[cmd.index("--insurers") + 1] == "BAJAJ,TATA"
    assert cmd[cmd.index("--vehicle") + 1] == "HONDA|ACTIVA|3G (110 CC) (PETROL)"
    assert cmd[cmd.index("--rto") + 1] == "MH-12 Pune"
    assert cmd[cmd.index("--policy") + 1] == "Third Party"
    assert cmd[cmd.index("--year") + 1] == "2021"
    assert "--all" in cmd and "--headless" in cmd
    assert r.plan_command[-1] == "--plan" and "--headless" not in r.plan_command
    assert r.lock == "sweep-bike-shared"


def test_sweeps_are_fast_unless_the_browser_is_asked_for():
    assert "--fast" in build(test="sweep", target="testsite").command
    assert "--fast" in build(test="sweep", target="testsite").plan_command  # honest estimate
    assert "--fast" not in build(test="sweep", target="testsite", speed="browser").command
    with pytest.raises(recipes.Invalid, match="speed"):
        build(test="sweep", speed="warp")


def test_live_takes_only_the_sweep():
    for test in ("lab", "journey", "best"):
        with pytest.raises(recipes.Invalid, match="quotes only"):
            build(test=test, target="live", insurers=["BAJAJ"])


def test_live_sweep_needs_named_companies_and_has_its_own_notes():
    with pytest.raises(recipes.Invalid, match="pick the companies"):
        build(test="sweep", target="live")
    assert build(test="sweep", target="live", insurers=["SBI"]).lock == "sweep-bike-live"


def test_deep_dive_needs_exactly_one_company_and_stops_at_quotes_by_default():
    with pytest.raises(recipes.Invalid, match="exactly one"):
        build(test="lab", target="testsite", insurers=["BAJAJ", "TATA"])
    r = build(test="lab", target="testsite", insurers=["TATA"],
              vehicles=["MARUTI|SWIFT|VXI (1298 CC) (PETROL)"], rtos=["GJ-01 Ahmedabad"])
    # The lab's own default (-1) goes all the way to payment - never by accident.
    assert r.command[r.command.index("--journeys") + 1] == "0"
    assert r.command[r.command.index("--vehicle") + 1] == "MARUTI SWIFT VXI (1298 CC) (PETROL)"
    assert "--journeys" not in build(test="lab", target="testsite", insurers=["TATA"],
                                     journeys="all").command


def test_bad_choices_are_refused_in_words():
    with pytest.raises(recipes.Invalid, match="Unknown company"):
        build(test="sweep", insurers=["NOPE"])
    with pytest.raises(recipes.Invalid, match="GJ-01 Ahmedabad"):
        build(test="sweep", rtos=["Pune"])
    with pytest.raises(recipes.Invalid, match="registration year"):
        build(test="sweep", years=["1850"])
    with pytest.raises(recipes.Invalid, match="policy"):
        build(test="sweep", policy=["Gold"])


def test_command_is_shown_as_someone_would_type_it():
    shown = recipes.shown([recipes.PYTHON, "-u", "run_quote_matrix.py", "--rto",
                           "MH-12 Pune", "--vehicle", "A|B|C (1 CC)"])
    assert shown == ('venv\\Scripts\\python run_quote_matrix.py --rto "MH-12 Pune" '
                     '--vehicle "A|B|C (1 CC)"')


PLAN_OUTPUT = """
THE CHOICES
  bike      HONDA ACTIVA 3G (110 CC) (PETROL)   (up to 150cc)
  RTOs      GJ-01 Ahmedabad

THE PLAN - 2 journeys (~0 h 6 min, 20s pause between)
   1. [baseline] HONDA ACTIVA · GJ-01 · 2022 · Comprehensive
                 known-good journey
   2. [twin    ] HONDA ACTIVA · GJ-01 · 2022 · Third Party
                 twin: same as baseline but Third Party

  Coverage: this plan tests 30 of 354 pairs.
"""


def test_plan_output_is_read_for_the_summary():
    plan = recipes.read_plan(PLAN_OUTPUT)
    assert plan["ok"] and plan["journeys"] == 2 and plan["minutes"] == 6
    assert (plan["covered"], plan["wanted"], plan["unit"]) == (30, 354, "pairs")
    assert [i["kind"] for i in plan["items"]] == ["baseline", "twin"]
    assert plan["items"][1]["why"].startswith("twin:")
    refused = recipes.read_plan("Cannot plan this run: vehicle 'X' is not in the list.")
    assert not refused["ok"] and refused["error"].startswith("Cannot plan")


def test_exit_codes_read_as_plain_outcomes():
    assert recipes.outcome("sweep", 0) == ("passed", "Every check passed")
    assert recipes.outcome("sweep", 1)[0] == "bugs"
    assert recipes.outcome("sweep", 3)[1] == "Refused by a guard rail"
    assert recipes.outcome("journey", 0)[0] == "passed"
    assert recipes.outcome("best", None)[0] == "failed"


# ============================================================ pinned plans

def _opts(**pins):
    opts = matrix.options(["A", "B"], ["GJ-01 Ahmedabad", "MH-01 Mumbai"], ["ICICI", "BAJAJ"])
    opts.update(pins)
    return opts


def test_pinned_policy_is_all_the_planner_plans():
    journeys, _ = matrix.plan(_opts(policy=[matrix.TP]), budget=100_000)
    assert journeys and all(s.get("policy") == matrix.TP for s in journeys)
    assert journeys[0].kind == "baseline"       # the baseline follows the pin


def test_pins_that_the_form_never_allows_plan_nothing():
    year = str(matrix.this_year() - 15)
    journeys, _ = matrix.plan(_opts(policy=[matrix.OD], year=[year]), budget=50)
    assert journeys == []


def test_engine_size_search_finds_the_rounded_number():
    rows = [{"_text": "ROYAL ENFIELD CLASSIC ELECTRIC START (346 CC) (PETROL)", "cc": 346.0}]
    assert catalog._word_fits("350", rows[0])
    assert catalog._word_fits("CLASSIC", rows[0])
    assert not catalog._word_fits("500", rows[0])


# ================================================================== queue

def _recipe(code: str, lock: str = "x", test: str = "sweep") -> recipes.Recipe:
    return recipes.Recipe(test, "testsite", "bike", [sys.executable, "-u", "-c", code],
                          f"fake {lock}", lock)


def _wait(job, timeout=30):
    end = time.time() + timeout
    while job.status in jobs.ACTIVE and time.time() < end:
        time.sleep(0.2)
    return job


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STUDIO", tmp_path / "portal")
    return jobs.Queue(max_parallel=2, store=tmp_path / "portal" / "runs.json")


def test_a_run_is_logged_followed_and_judged(queue):
    code = ("import sys\nprint('[ 1/3] first')\nprint('[ 2/3] second')\n"
            "print('[ 3/3] third')\nprint('QUOTE MATRIX RESULTS')\nsys.exit(1)")
    job = _wait(queue.submit(_recipe(code), "Mayur", {}))
    assert (job.status, job.exit_code, job.done, job.total) == ("bugs", 1, 3, 3)
    assert job.headline == "third"
    log = job.log.read_text(encoding="utf-8")
    assert log.startswith("$ ") and "[ 2/3] second" in log


def test_a_run_can_be_stopped(queue):
    job = queue.submit(_recipe("import time\nprint('[ 1/9] go', flush=True)\ntime.sleep(60)"),
                       "Mayur", {})
    end = time.time() + 15
    while job.status != "running" and time.time() < end:
        time.sleep(0.1)
    assert queue.stop(job.id)
    assert _wait(job).status == "stopped"


def test_runs_sharing_notes_never_overlap(queue):
    slow = "import time\ntime.sleep(1.5)"
    a = queue.submit(_recipe(slow, lock="sweep-bike-shared"), "a", {})
    b = queue.submit(_recipe(slow, lock="sweep-bike-shared"), "b", {})
    end = time.time() + 10
    while a.status != "running" and time.time() < end:
        time.sleep(0.05)
    while "same notes" not in b.outcome and time.time() < end:
        time.sleep(0.05)                 # the queue says why on its next pass
    assert b.status == "queued" and "same notes" in b.outcome
    _wait(a), _wait(b)
    assert b.started >= a.finished - 0.5


def test_a_restart_never_calls_a_lost_run_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STUDIO", tmp_path / "portal")
    store = tmp_path / "portal" / "runs.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps([{"id": "r1", "who": "a", "form": {}, "test": "sweep",
                                  "target": "live", "product": "bike", "title": "t",
                                  "command": ["x"], "lock": "l", "status": "running"}]))
    job = jobs.Queue(store=store).get("r1")
    assert job.status == "interrupted" and "stopped" in job.outcome


# ==================================================================== api

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from portal import app as studio
    monkeypatch.setattr(jobs, "STUDIO", tmp_path / "portal")
    monkeypatch.setattr(catalog, "refresh_rtos", lambda force=False: None)
    q = jobs.Queue(store=tmp_path / "portal" / "runs.json")

    def make(code=""):
        return TestClient(studio.create_app(queue=q, access_code=code))
    return make


def test_meta_lists_servers_tests_and_companies(client):
    meta = client().get("/api/meta").json()
    assert {t["id"] for t in meta["targets"]} == {"local", "testsite", "live"}
    live = next(t for t in meta["targets"] if t["id"] == "live")
    assert live["live"] is True
    assert {t["id"] for t in meta["tests"]} == {"sweep", "lab", "journey", "best"}
    assert any(p["id"] == "top5" for p in meta["presets"])


def test_live_run_needs_the_word_live(client):
    c = client()
    form = {"test": "sweep", "target": "live", "insurers": ["BAJAJ"], "level": "smoke"}
    res = c.post("/api/runs", json=form)
    assert res.status_code == 400 and "LIVE" in res.json()["error"]
    lab = c.post("/api/runs", json={"test": "lab", "target": "live", "insurers": ["BAJAJ"],
                                    "confirm": "LIVE"})
    assert lab.status_code == 400 and "quotes only" in lab.json()["error"]


@pytest.mark.parametrize("path", ["/files/../config/settings.local.json",
                                  "/files/..%2Fconfig%2Fsettings.local.json",
                                  "/files/..%5C..%5Cconfig%5Csettings.local.json"])
def test_nothing_outside_reports_can_be_read(client, path):
    assert client().get(path).status_code == 404


def test_the_access_code_locks_everything_but_the_door(client):
    c = client("s3cret")
    assert c.get("/api/meta").json()["locked"] is True
    assert c.get("/api/runs").status_code == 401
    assert c.get("/files/portal/x").status_code == 401
    assert c.post("/api/unlock", json={"code": "wrong"}).status_code == 403
    assert c.post("/api/unlock", json={"code": "s3cret"}).status_code == 200
    assert c.get("/api/runs").status_code == 200        # the cookie now opens it


def test_findings_are_read_in_plain_words(tmp_path):
    from portal.app import read_findings
    f = tmp_path / "findings.txt"
    f.write_text("DEFECT  TATA         refused with an error from OUR integration  [#1]\n"
                 "        A null value reached the insurer request.\n"
                 "LOOK    SBI          the portal never asked SBI  [#1, #2]\n"
                 "        Either it is switched off.\n\nTWIN RULES\n  not checked  x\n",
                 encoding="utf-8")
    found = read_findings(f)
    assert [(x["kind"], x["company"]) for x in found] == [("bug", "TATA"), ("look", "SBI")]
    assert found[0]["detail"].startswith("A null value") and found[1]["journeys"] == "#1, #2"


# ============================================================= live login

LOGIN_PAGE = b"""<!doctype html><title>Login</title>
<input type="text" placeholder="Enter User Name" style="display:none">
<div id="cred"><input type="text" id="u"><input type="password" id="p">
<button onclick="go()">Login</button><span class="text-danger" id="err"></span></div>
<div id="otp" style="display:none"><input placeholder="Enter OTP" id="o"><button onclick="confirmOtp()">Confirm OTP</button></div>
<script>
function go(){ if(u.value==='me'&&p.value==='pw'){cred.style.display='none';otp.style.display='block'}
  else { err.textContent='Invalid username or password'; } }
function confirmOtp(){ if(o.value==='12345'){ location.href='/dashboard'; } }
</script>"""


class FakeLive(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/Account/Login"):
            body = LOGIN_PAGE
            self.send_response(200)
        elif self.path.startswith("/motor-journey"):
            body = b"<title>journey</title>ok"
            self.send_response(200)
            self.send_header("Set-Cookie", ".mob-token-w=token123; Path=/")
        else:
            body = b"<title>dashboard</title>welcome"
            self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_live_login_relay_types_password_and_otp_then_saves(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    from core import auth
    from portal import livelogin
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLive)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session = tmp_path / "live.json"
    monkeypatch.setattr(auth, "LIVE_SESSION_FILE", session)
    relay = livelogin.LiveLogin(SimpleNamespace(
        auth_base_url=f"http://127.0.0.1:{server.server_port}"))

    def until(*states, timeout=40):
        end = time.time() + timeout
        while relay.state not in states and time.time() < end:
            time.sleep(0.2)
        return relay.state

    try:
        relay.start("test")
        if until("credentials", "failed") == "failed" and "Executable" in relay.message:
            pytest.skip("no browser available")
        assert relay.state == "credentials" and relay.screenshot.startswith("data:image")
        relay.credentials("me", "wrong")
        assert until("credentials", "otp") == "credentials"
        relay.credentials("me", "pw")
        assert until("otp", "failed") == "otp"
        relay.otp("12345")
        assert until("done", "failed") == "done", relay.message
        saved = json.loads(session.read_text(encoding="utf-8"))
        assert any(c["name"] == ".mob-token-w" for c in saved["cookies"])
    finally:
        relay.cancel()
        server.shutdown()
