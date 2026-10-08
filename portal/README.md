# Probus Test Studio

A web page for running this repository's motor tests. Pick a server, a product, a test, and optionally companies, vehicles (MMV), RTOs and policy details. Anything you leave empty, the Studio picks for you. Then press **Launch**. While the run goes you can watch its live log, and afterwards its results, Excel, report and per-journey videos.

Every run is one of the existing command-line tools, with the same checks and the same guard rails. The live site stays **quotes only**: this is enforced in `config/settings.py` and `core/safety.py`, whatever anyone clicks.

## Start it

On the computer that will host it, double-click `start_studio.bat`, or run:

```
venv\Scripts\python -m portal
```

It prints the addresses to use:

```
On this computer : http://localhost:8765
For your team    : http://<computer-name>:8765
```

Send the "For your team" address to colleagues. They need nothing installed, only a browser. Keep the window open while people use it; closing it stops the Studio, and any run that was going is marked *interrupted*.

## Why it is hosted this way

The tests drive real browsers. They also need things that only exist on a developer's machine:
- Playwright and Chrome
- the saved logins (`.session/`)
- `config/settings.local.json`
- the KYC test documents
- for **Local dev**, the Angular app and API on `localhost`

A public website cannot do any of that. It would also put a button that sends live quote requests on the internet. So the Studio runs on **one office computer** and the team opens it over the office network, or over the VPN when remote.

Good hosts, best first:
1. **An always-on office PC or VM** with this repo, the venv and `settings.local.json`. Make the Studio start at logon: Task Scheduler, then *Create Task*, then trigger *At log on*, with action `start_studio.bat`.
2. **Your own PC** while you are at work. This is the quickest way to start.

"Local dev" always means the host's own localhost.

## If a colleague cannot open it

Windows Firewall blocks the port by default. If the colleague's browser says "took too long to respond", the firewall is the cause. Run this once on the host, in a PowerShell window opened **as Administrator**:

```
New-NetFirewallRule -DisplayName "Probus Test Studio" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Any -RemoteAddress LocalSubnet
```

`-Profile Any` matters because office Ethernet often shows up as a *Public* network (it did on the first host, 2026-10-07). A rule limited to Domain/Private networks then never applies. `LocalSubnet` keeps the port open only to the office network.

Give colleagues the number address the Studio prints (e.g. `http://192.168.1.142:8765`). The computer name does not resolve on every PC.

## Keep it to the team (optional)

In `config/settings.local.json` on the host:

```json
"portal_access_code": "pick-something",
"portal_max_parallel": 2
```

- **portal_access_code:** each browser asks for the code once, then remembers it for 180 days.
- **portal_max_parallel:** how many runs may go at once. Each run is a real browser, and the environments are shared. Runs that write the same notes never overlap anyway; they queue.

The Studio serves plain HTTP. On the office network that is fine. Do not expose the port to the internet.

## Live login

Live runs reuse a saved login (`.session/live.json`). When it expires, choose **Live production** and press **Log in**. Type the username, the password and the OTP in the Studio. They are typed into the live login page on the host and never saved.

## What is where

| File | What it does |
|---|---|
| `portal/app.py` | the web server and its small JSON API |
| `portal/recipes.py` | turns what you picked into the exact runner command; the early "no" for the live site |
| `portal/jobs.py` | the team queue: start, stop, log, progress, history (`reports/portal/runs.json`) |
| `portal/catalog.py` | companies, vehicles (MMV) and RTOs, from the portal's own master data |
| `portal/health.py` | the server dots in the top bar |
| `portal/livelogin.py` | the live login relay: password, then OTP, then the saved session |
| `portal/static/` | the page itself: plain HTML, CSS and JS with no build step, so edit and reload |

Each run's files land in `reports/portal/<run id>/`. Tests: `venv\Scripts\python -m pytest tests/test_portal.py -q`.
