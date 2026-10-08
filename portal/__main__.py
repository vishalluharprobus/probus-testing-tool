"""
Start the Test Studio.

    venv\\Scripts\\python -m portal               # port 8765, the whole office
    venv\\Scripts\\python -m portal --port 9000
    venv\\Scripts\\python -m portal --local-only  # just this computer
"""
from __future__ import annotations

import argparse
import socket

from core import console


def _addresses() -> list[str]:
    names = {socket.gethostname()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            names.add(info[4][0])
    except OSError:
        pass
    # The address this computer uses to reach the network.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            names.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(n for n in names if not n.startswith("127."))


def main() -> None:
    console.use_utf8()
    ap = argparse.ArgumentParser(description="Probus Test Studio")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--local-only", action="store_true",
                    help="only this computer can open it")
    args = ap.parse_args()
    host = "127.0.0.1" if args.local_only else "0.0.0.0"

    import uvicorn
    from portal.app import create_app

    print("\n  PROBUS TEST STUDIO")
    print(f"  On this computer : http://localhost:{args.port}")
    if not args.local_only:
        for name in _addresses():
            print(f"  For your team    : http://{name}:{args.port}")
        print("  (If a colleague's browser says 'took too long to respond', allow "
              "the port once, in PowerShell as Administrator:\n"
              f"   New-NetFirewallRule -DisplayName \"Probus Test Studio\" -Direction "
              f"Inbound -Action Allow -Protocol TCP -LocalPort {args.port} -Profile Any "
              f"-RemoteAddress LocalSubnet )")
    print("  Stop it with Ctrl+C. Runs that are going will be marked interrupted.\n")
    uvicorn.run(create_app(), host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
