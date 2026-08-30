import time

from intenttwin.db import fetch_one
from intenttwin.pipeline import create_run

run_id = create_run()
while True:
    run = fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    print(f"run {run_id}: {run['status']} {run['progress']}%")
    if run["status"] not in {"queued", "running"}:
        raise SystemExit(0 if run["status"] == "passed" else 1)
    time.sleep(.25)
