"""
Log in to the LIVE site from the Studio - password and OTP typed in your own
browser, into a browser running on the host computer.

Why: live runs from the Studio are headless (on the host), and the live login
can ask for an OTP, so nobody can type it there. This drives that login one
step at a time, shows a screenshot after each step, and saves the session to
.session/live.json - the file every live run reuses (core/auth.py). The
password is typed into the page and forgotten; it is never written anywhere.

One login at a time; it gives up after ten quiet minutes.
"""
from __future__ import annotations

import base64
import queue
import threading
import time

from config import settings
from core import auth

IDLE_SECONDS = 600
OTP_INPUT = 'input[placeholder="Enter OTP"]:visible'


class LiveLogin:
    def __init__(self, cfg=None):
        self.cfg = cfg or settings.TARGETS["live"]
        self.state = "idle"       # idle starting credentials otp working done failed
        self.message = ""
        self.screenshot = ""      # data: URL of the last step
        self.who = ""
        self.updated = time.time()
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- public
    def status(self) -> dict:
        if self.state in ("credentials", "otp") and time.time() - self.updated > IDLE_SECONDS:
            self.cancel()
            self.message = "Gave up after ten quiet minutes - start again."
        return {"state": self.state, "message": self.message,
                "screenshot": self.screenshot, "who": self.who}

    def start(self, who: str = "") -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self.state not in ("done", "failed"):
                    return self.status()
            self.state, self.message, self.who = "starting", "Opening the live login page ...", who
            self.screenshot = ""
            self._jobs = queue.Queue()
            self._thread = threading.Thread(target=self._run, name="studio-live-login",
                                            daemon=True)
            self._thread.start()
        return self.status()

    def credentials(self, username: str, password: str) -> dict:
        if self.state != "credentials":
            return self.status()
        self._send("credentials", username, password)
        return self.status()

    def otp(self, code: str) -> dict:
        if self.state != "otp":
            return self.status()
        self._send("otp", code)
        return self.status()

    def cancel(self) -> None:
        if self._thread and self._thread.is_alive():
            self._jobs.put(("cancel",))
        if self.state not in ("done",):
            self.state = "idle"

    # ------------------------------------------------------------ worker
    def _send(self, *job) -> None:
        self.state, self.updated = "working", time.time()
        self.message = "Working ..."
        self._jobs.put(job)

    def _run(self) -> None:
        # Playwright objects live on the thread that made them, so every step
        # happens here, fed through the queue.
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1280, "height": 860},
                                              ignore_https_errors=True)
                page = context.new_page()
                page.goto(f"{self.cfg.auth_base_url}/Account/Login",
                          wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1500)
                self._shot(page, "credentials",
                           "Type the live username and password.")
                while True:
                    try:
                        job = self._jobs.get(timeout=5)
                    except queue.Empty:
                        if time.time() - self.updated > IDLE_SECONDS:
                            self.state, self.message = "idle", "Timed out."
                            break
                        continue
                    if job[0] == "cancel":
                        break
                    if job[0] == "credentials":
                        self._type_credentials(page, job[1], job[2])
                    elif job[0] == "otp":
                        self._type_otp(page, job[1])
                    if self.state in ("done", "failed"):
                        break
                    if self.state == "working" and "/Account/Login" not in page.url:
                        self._finish(context, page)
                        break
                browser.close()
        except Exception as exc:                       # noqa: BLE001
            self.state = "failed"
            self.message = f"The login browser stopped: {type(exc).__name__}: {str(exc)[:160]}"

    def _type_credentials(self, page, username: str, password: str) -> None:
        page.locator('input[type="text"]:visible').first.fill(username)
        page.locator('input[type="password"]:visible').first.fill(password)
        page.get_by_role("button", name="Login").filter(visible=True).first.click()
        self._settle(page)
        self._after_step(page, "Wrong username or password? The page says: ")

    def _type_otp(self, page, code: str) -> None:
        page.locator(OTP_INPUT).first.fill(code)
        page.get_by_role("button", name="Confirm OTP").filter(visible=True).first.click()
        self._settle(page)
        self._after_step(page, "The OTP was not accepted. The page says: ")

    def _after_step(self, page, why: str) -> None:
        if "/Account/Login" not in page.url:
            self.state = "working"
            return
        if page.locator(OTP_INPUT).count():
            self._shot(page, "otp", "Type the OTP sent to the registered mobile.")
            return
        said = self._page_message(page)
        self._shot(page, "credentials", (why + said) if said else
                   "Still on the login page - check the screenshot and try again.")

    def _finish(self, context, page) -> None:
        self.message = "Logged in - collecting the journey token ..."
        page.goto(f"{self.cfg.auth_base_url}/motor-journey/two-wheeler",
                  wait_until="domcontentloaded", timeout=45_000)
        for _ in range(20):
            if any(c["name"] == ".mob-token-w"
                   for c in context.cookies(self.cfg.auth_base_url)):
                auth.LIVE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(auth.LIVE_SESSION_FILE))
                self._shot(page, "done", "Logged in. Live runs will use this login.")
                return
            page.wait_for_timeout(1000)
        self._shot(page, "failed", "Logged in, but the two-wheeler journey never "
                   "issued its token. Check that this account can open it.")

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _settle(page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

    @staticmethod
    def _page_message(page) -> str:
        for sel in (".text-danger:visible", ".alert:visible", ".error:visible",
                    ".toast-message:visible", ".swal2-html-container:visible"):
            try:
                text = page.locator(sel).first.inner_text(timeout=500).strip()
                if text:
                    return text[:200]
            except Exception:
                continue
        return ""

    def _shot(self, page, state: str, message: str) -> None:
        try:
            png = page.screenshot(type="jpeg", quality=70)
            self.screenshot = "data:image/jpeg;base64," + base64.b64encode(png).decode()
        except Exception:
            pass
        self.state, self.message, self.updated = state, message, time.time()
