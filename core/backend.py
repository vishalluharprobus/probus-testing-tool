"""
Is the app's own API answering, before we bother opening a browser?

WHY THIS EXISTS
---------------
The Angular portal on :4200 is only the front half. It fetches its master data -
RTO cities, previous insurers, makes and models - from a .NET API that a
developer runs locally on :53339. When that API stops answering, the portal does
not report an error. It shows a spinner and the word "Loading", forever.

From the test's point of view that is indistinguishable from a broken selector,
and it cost a long stretch of this project's time to tell apart: four runs in a
row failed at screen one, each burning a login and a 20-second wait, before the
network log showed two calls that were simply never answered.

    GET /api/Motor/RTOcityJson                              never returned
    GET /api/Motor/TwoWheeler/PreviousInsurerForBrokerRenewal   never returned

So we ask the API directly first. Two seconds here saves ninety later, and turns
"the test failed" into "your back end is down", which is a different sentence
with a different person fixing it.

THE FAILURE IS NOT A DEAD PORT
------------------------------
Worth knowing, because it decides what the message should say. Windows' HTTP
stack keeps listening on the port even when the IIS Express worker behind it has
wedged, so the port is open, the TCP connection is accepted, and nothing ever
answers. A "is the port open?" check would cheerfully report the API healthy.
Only an actual HTTP request with a timeout tells the truth.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

# Short on purpose. A healthy API answers this in well under a second; anything
# slower is the wedged case, and waiting longer only delays the diagnosis.
PROBE_TIMEOUT_SECONDS = 6

# A cheap master-data endpoint the portal itself calls on screen one. Chosen
# because a failure here is exactly the failure that blocks a run - not a
# separate health endpoint that can be fine while the real ones hang.
PROBE_PATH = "/api/Motor/RTOcityJson"


@dataclass
class Health:
    ok: bool
    detail: str

    def __bool__(self) -> bool:
        return self.ok


def check(api_url: str) -> Health:
    """
    Ask the API one real question and see whether it answers.

    Any HTTP response at all counts as healthy, including a 404 or a 500: those
    prove a worker is alive and processing, which is the thing we need to know.
    Only a timeout or a refused connection means the journey cannot run.
    """
    if not api_url:
        return Health(True, "no API url configured - skipping the check")

    url = api_url.rstrip("/") + PROBE_PATH
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return Health(True, f"answered {response.status}")
    except urllib.error.HTTPError as exc:
        # An error STATUS is still an answer - the worker is alive.
        return Health(True, f"answered {exc.code}")
    except (socket.timeout, TimeoutError):
        return Health(False, _wedged_message(api_url))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return Health(False, _wedged_message(api_url))
        return Health(False, _down_message(api_url, str(reason)))
    except Exception as exc:                       # noqa: BLE001 - never block a run
        return Health(True, f"could not check ({type(exc).__name__}) - continuing")


def _wedged_message(api_url: str) -> str:
    host = urlparse(api_url).hostname or api_url
    port = urlparse(api_url).port or ""
    return (
        f"The app's API at {api_url} accepted the connection but never "
        f"answered (waited {PROBE_TIMEOUT_SECONDS}s).\n"
        f"  This is the wedged case, not a dead one: Windows keeps the port "
        f"open even after the IIS Express worker behind it has stopped "
        f"processing, so the port looks fine and every request hangs.\n"
        f"  The portal will sit on 'Loading' forever, because this is where it "
        f"fetches RTO and insurer master data from.\n"
        f"  What to do: restart the API (stop and start it in Visual Studio). "
        f"To confirm the diagnosis first:\n"
        f"      netstat -ano | findstr {port or host}\n"
        f"  Many sockets in CLOSE_WAIT means the worker is not releasing "
        f"connections, which is the wedge.\n"
        f"  This is not a test failure - nothing was run."
    )


def _down_message(api_url: str, reason: str) -> str:
    return (
        f"The app's API at {api_url} is not reachable: {reason}\n"
        f"  The portal cannot load its master data without it, so the journey "
        f"would stop on 'Loading' at screen one.\n"
        f"  What to do: start the API in Visual Studio, then run this again.\n"
        f"  This is not a test failure - nothing was run."
    )
