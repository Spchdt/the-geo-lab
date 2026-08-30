"""FastAPI dashboard: run creation, live status polling, and result rendering."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import ROOT, connect, decode_json_fields, fetch_all, fetch_one
from .geo import submission_form
from .pipeline import (
    cancel_run,
    create_confirmation_run,
    create_geo_run,
    create_run,
    load_fixture,
    load_gap_report,
    retry_gap_summary,
    start_worker,
)
from .reasoning import llm_configured, load_env_file


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Requeue runs interrupted by a restart, then hand control to the server."""
    load_fixture()
    with connect() as db:
        db.execute("UPDATE runs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE status='running' AND cancel_requested=1")
        db.execute("UPDATE run_jobs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE status='running' AND run_id IN (SELECT id FROM runs WHERE status='cancelled')")
        db.execute("UPDATE runs SET status='queued' WHERE status='running' AND cancel_requested=0")
        db.execute("UPDATE run_jobs SET status='pending',started_at=NULL WHERE status='running' AND run_id IN (SELECT id FROM runs WHERE status='queued')")
    start_worker()
    yield


CONTROL_CONDITIONS = {"identity_copy", "identity_control", "misleading_control"}
NEUTRAL_TOLERANCE = 0.00005
STALL_WARNING_SECONDS = 35

app = FastAPI(title="The GEO Lab", docs_url="/api/docs", lifespan=lifespan)
templates = Jinja2Templates(directory=ROOT / "src" / "intenttwin" / "templates")
app.mount("/static", StaticFiles(directory=ROOT / "src" / "intenttwin" / "static"), name="static")


def _format_points(value: float) -> str:
    return f"{value * 100:+.1f} percentage points"


def _classify_effect(condition: str, value: float) -> str:
    if condition == "original":
        return "metric-neutral"
    if condition in CONTROL_CONDITIONS:
        return "metric-good" if abs(value) < NEUTRAL_TOLERANCE else "metric-bad"
    if value > 0:
        return "metric-good"
    if value < 0:
        return "metric-bad"
    return "metric-neutral"


def decorate_metrics(metrics: list[dict]) -> list[dict]:
    decorated = []
    for source in metrics:
        metric = dict(source)
        condition = metric["condition"]
        metric["inclusion_class"] = _classify_effect(condition, metric.get("inclusion_delta", 0))
        metric["recommendation_class"] = _classify_effect(condition, metric.get("delta", 0))
        metric["retrieval_mrr_class"] = _classify_effect(condition, metric.get("retrieval_mrr_delta", 0))
        metric["retrieval_top3_class"] = _classify_effect(condition, metric.get("retrieval_top3_delta", 0))
        metric["top1_class"] = _classify_effect(condition, metric.get("top1_delta", 0))
        metric["top3_class"] = _classify_effect(condition, metric.get("top3_delta", 0))
        low, high = metric.get("ci_low"), metric.get("ci_high")
        if condition == "original" or low is None or high is None:
            metric["interval_class"] = "metric-neutral"
        elif condition in CONTROL_CONDITIONS:
            metric["interval_class"] = metric["recommendation_class"]
        elif low > 0:
            metric["interval_class"] = "metric-good"
        elif high < 0:
            metric["interval_class"] = "metric-bad"
        else:
            metric["interval_class"] = "metric-warning"
        attempts = metric.get("attempts", 0)
        valid = metric.get("valid_attempts", attempts)
        unsupported = metric.get("unsupported_template_claims", 0)
        metric["valid_class"] = "metric-neutral" if condition == "misleading_control" else ("metric-good" if attempts and valid == attempts else "metric-bad")
        metric["unsupported_class"] = ("metric-good" if attempts and unsupported == attempts else "metric-bad") if condition == "misleading_control" else ("metric-good" if unsupported == 0 else "metric-bad")
        decorated.append(metric)
    return decorated


def build_metric_conclusion(metrics: list[dict]) -> dict | None:
    by_condition = {metric["condition"]: metric for metric in metrics}
    grounded = by_condition.get("grounded_enriched")
    normalized = by_condition.get("normalized")
    identity = by_condition.get("identity_copy")
    misleading = by_condition.get("misleading_control")
    if not grounded:
        return None

    retrieval_delta = grounded.get("inclusion_delta", 0)
    recommendation_delta = grounded.get("delta", 0)
    if retrieval_delta > 0:
        retrieval_direction = f"Grounded enrichment retrieved the PUT more often ({_format_points(retrieval_delta)} versus original)."
    elif retrieval_delta < 0:
        retrieval_direction = f"Grounded enrichment retrieved the PUT less often ({_format_points(retrieval_delta)} versus original)."
    else:
        retrieval_direction = "Grounded enrichment did not change reasoning-set retrieval versus original."

    low, high = grounded.get("ci_low"), grounded.get("ci_high")
    if low is not None and low > 0:
        recommendation_strength = f"The recommendation lift ({_format_points(recommendation_delta)}) is consistently positive in this run."
        headline_class = "conclusion-good" if retrieval_delta >= 0 else "conclusion-warning"
    elif high is not None and high < 0:
        recommendation_strength = f"Recommendation decreased ({_format_points(recommendation_delta)}) and the interval stays below zero."
        headline_class = "conclusion-bad"
    else:
        recommendation_strength = f"The recommendation change ({_format_points(recommendation_delta)}) is uncertain because its interval includes zero."
        headline_class = "conclusion-warning"

    normalized_text = ""
    if normalized:
        normalized_text = f" Normalization changed retrieval by {_format_points(normalized.get('inclusion_delta', 0))}."
    identity_retrieval = identity.get("inclusion_delta", 0) if identity else 0
    identity_recommendation = identity.get("delta", 0) if identity else 0
    identity_text = "Identity control passed for retrieval." if abs(identity_retrieval) < NEUTRAL_TOLERANCE else f"Identity control failed for retrieval ({_format_points(identity_retrieval)}); do not trust treatment retrieval effects."
    if abs(identity_recommendation) >= NEUTRAL_TOLERANCE:
        identity_text += f" Its recommendation drift of {_format_points(identity_recommendation)} shows model variability."
    misleading_detected = misleading and misleading.get("attempts", 0) and misleading.get("unsupported_template_claims", 0) == misleading.get("attempts", 0)
    safety_text = "Misleading-control claims were detected in every attempt." if misleading_detected else "The misleading safety control was not fully detected; do not make safety claims."

    same_direction = (retrieval_delta == 0 or recommendation_delta == 0 or (retrieval_delta > 0) == (recommendation_delta > 0))
    relationship = "Retrieval and recommendation point in the same direction." if same_direction else "Retrieval and recommendation move in different directions. This is not automatically a contradiction: retrieval decides whether the PUT enters the reasoning set, while recommendation evaluates it inside a fixed candidate set."
    return {
        "class": headline_class,
        "headline": f"{retrieval_direction} {recommendation_strength}",
        "treatments": normalized_text.strip(),
        "controls": f"{identity_text} {safety_text}",
        "relationship": relationship,
        "caveat": "Smoke-test evidence is directional, not confirmatory. ‘Recommended’ currently means the PUT appeared anywhere in the model’s ordered list, not necessarily first choice.",
    }


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/runs")


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "runs.html", {"runs": fetch_all("SELECT *,json_extract(manifest_json,'$.recommendation_model') AS model,json_extract(manifest_json,'$.product_snapshot.name') AS product_name,json_extract(manifest_json,'$.phase') AS phase FROM runs ORDER BY id DESC"), "llm_ready": llm_configured()})


@app.post("/runs")
async def new_run(request: Request) -> RedirectResponse:
    parsed = parse_qs((await request.body()).decode())
    form = {key: values[0] for key, values in parsed.items()}
    names = parsed.get("attribute_name", [])
    values = parsed.get("attribute_value", [])
    units = parsed.get("attribute_unit", [])
    preferences = parsed.get("attribute_preference", [])
    form["attributes_json"] = json.dumps([{"name": name, "value": values[index] if index < len(values) else "", "unit": units[index] if index < len(units) else "", "preference": preferences[index] if index < len(preferences) else "exact"} for index, name in enumerate(names)])
    try:
        run_id = create_geo_run(form)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400 if isinstance(exc, ValueError) else 503, str(exc)) from exc
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int) -> HTMLResponse:
    run = fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    run = decode_json_fields(run, "manifest_json")
    jobs = fetch_all("SELECT * FROM run_jobs WHERE run_id=? ORDER BY position", (run_id,))
    metrics = [decode_json_fields(row, "data_json")["data_json"] for row in fetch_all("SELECT * FROM metrics WHERE run_id=? ORDER BY condition", (run_id,))]
    gap_report = load_gap_report(run_id)
    conclusion = None if gap_report else build_metric_conclusion(metrics)
    metrics = decorate_metrics(metrics)
    failures = fetch_all("SELECT a.query_group_id,a.condition,a.trial,v.errors_json FROM validations v JOIN llm_attempts a ON a.id=v.attempt_id WHERE a.run_id=? AND v.valid=0", (run_id,))
    artifacts = fetch_all("SELECT * FROM artifacts WHERE run_id=?", (run_id,))
    return templates.TemplateResponse(request, "run_detail.html", {"run": run, "jobs": jobs, "metrics": metrics, "conclusion": conclusion, "gap_report": gap_report, "failures": failures, "artifacts": artifacts})


@app.post("/runs/{run_id}/confirm")
def confirm(run_id: int) -> RedirectResponse:
    try:
        new_id = create_confirmation_run(run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/runs/{new_id}", status_code=303)


@app.post("/runs/{run_id}/summary/retry")
def retry_summary(run_id: int) -> RedirectResponse:
    try:
        retry_gap_summary(run_id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/cancel")
def cancel(request: Request, run_id: int):
    try:
        state = cancel_run(run_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if request.headers.get("x-requested-with") == "fetch":
        return {"status": state}
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/retry")
def retry(run_id: int) -> RedirectResponse:
    run = fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    manifest = json.loads(run["manifest_json"])
    if manifest.get("experiment_version") == "geo-gap-v1":
        form = submission_form(manifest["product_snapshot"])
        promoted = [condition for condition in manifest["conditions"] if condition not in {"original", "identity_control"}]
        new_id = create_geo_run(form, manifest["phase"], run_id, promoted if manifest["phase"] == "confirmation" else None)
    else:
        new_id = create_run(run["put_product_id"], run["suite"], run_id)
    return RedirectResponse(f"/runs/{new_id}", status_code=303)


@app.post("/runs/{run_id}/lock")
def lock(run_id: int) -> RedirectResponse:
    with connect() as db:
        db.execute("UPDATE runs SET locked=1 WHERE id=?", (run_id,))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}/artifact/{artifact_id}")
def artifact(run_id: int, artifact_id: int) -> FileResponse:
    row = fetch_one("SELECT * FROM artifacts WHERE id=? AND run_id=?", (artifact_id, run_id))
    if not row:
        raise HTTPException(404)
    return FileResponse(row["path"], filename=row["name"])


@app.get("/api/runs/{run_id}/status")
def status(run_id: int) -> dict:
    run = fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    jobs = fetch_all("SELECT stage,status,log_text,error_text,started_at,completed_at,CASE WHEN status='running' THEN CAST(strftime('%s','now')-strftime('%s',started_at) AS INTEGER) END AS seconds_idle FROM run_jobs WHERE run_id=? ORDER BY position", (run_id,))
    current = next((job for job in jobs if job["status"] == "running"), None)
    if run["status"] == "cancelling":
        detail = "Cancellation requested; waiting for current LLM call to stop."
    elif run["status"] == "queued":
        ahead = fetch_one("SELECT COUNT(*) AS count FROM runs WHERE status IN ('queued','running','cancelling') AND id<?", (run_id,))["count"]
        detail = f"Pending; {ahead} run(s) ahead."
    elif current and (current["seconds_idle"] or 0) > STALL_WARNING_SECONDS:
        detail = f"No activity for {current['seconds_idle']}s; current call may be slow or stuck."
    elif current:
        detail = current["log_text"].splitlines()[-1] if current["log_text"] else f"Running {current['stage']}."
    elif run["status"] == "failed":
        failed = next((job for job in jobs if job["status"] == "failed"), None)
        detail = (failed or {}).get("error_text") or "Run failed."
    else:
        detail = run["status"].capitalize() + "."
    return {"run": run, "jobs": jobs, "detail": detail}


@app.get("/api/runs/{run_id}/metrics")
def metrics(run_id: int) -> list[dict]:
    return [json.loads(row["data_json"]) for row in fetch_all("SELECT data_json FROM metrics WHERE run_id=?", (run_id,))]


@app.get("/api/runs/{run_id}/retrieval/{query_group_id}")
def retrieval(run_id: int, query_group_id: str) -> list[dict]:
    return fetch_all("SELECT condition,channel,product_id,score,rank FROM retrieval_traces WHERE run_id=? AND query_group_id=? ORDER BY condition,channel,rank", (run_id, query_group_id))


@app.get("/api/runs/{run_id}/failures")
def failures(run_id: int) -> list[dict]:
    return fetch_all("SELECT a.query_group_id,a.condition,a.trial,v.errors_json FROM validations v JOIN llm_attempts a ON a.id=v.attempt_id WHERE a.run_id=? AND v.valid=0", (run_id,))


def main() -> None:
    load_env_file(ROOT / ".env")
    uvicorn.run("intenttwin.web:app", host=os.getenv("INTENTTWIN_HOST", "127.0.0.1"), port=int(os.getenv("INTENTTWIN_PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
