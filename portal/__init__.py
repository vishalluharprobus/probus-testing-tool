"""
Probus Test Studio - a web front door to this test tool.

    venv\\Scripts\\python -m portal            # http://<this-computer>:8765

A developer picks the server, product, test, companies, vehicle, RTO and any
policy details (all optional - whatever is left empty is chosen for them), and
the Studio runs the same command-line tools this repository already has,
one queue for the whole team, with the live log, results, Excel, report and
videos in the browser.

Nothing here changes what a test may do: every run goes through the same
runners and the same guard rails (core/safety.py). The live site stays quotes
only, whoever presses the button.
"""
