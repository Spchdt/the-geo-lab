import json
import os
import time

import pytest
from conftest import form

from geolab.db import connect, fetch_all, fetch_one, migrate
from geolab.pipeline import cancel_run, create_geo_run, wait_with_cancel
from geolab.reasoning import LLMRequestError, load_env_file


def test_provider_wait_never_sleeps_for_negative_time(monkeypatch):
    clock = iter([0.0, 0.5, 1.1])
    sleeps = []
    monkeypatch.setattr("geolab.pipeline.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("geolab.pipeline.time.sleep", sleeps.append)
    monkeypatch.setattr("geolab.pipeline.cancel_requested", lambda run_id: False)
    assert wait_with_cancel(1, 1.0)
    assert sleeps == [0.25]


def await_run(run_id, attempts=300):
    for _ in range(attempts):
        run = fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run and run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} never settled")


def test_screen_survives_rate_limits_and_reuses_cached_observations(tmp_path, monkeypatch):
    monkeypatch.setattr("geolab.db.DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr("geolab.pipeline.connect", connect)
    monkeypatch.setenv("GEOLAB_LLM_URL", "https://recorded.test/v1/chat/completions")
    monkeypatch.setenv("GEOLAB_LLM_MODEL", "recorded-test-model")
    monkeypatch.setenv("GEOLAB_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GEOLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setenv("GEOLAB_LLM_RATE_LIMIT_BASE_DELAY", "0")
    live_calls = []
    rate_limited = True

    def recorded(request):
        nonlocal rate_limited
        live_calls.append(request)
        if rate_limited:
            rate_limited = False
            raise LLMRequestError("LLM HTTP 429: quota exceeded", status_code=429, retry_after=0)
        picked = request["candidates"][:2]
        presented = {row["product_id"]: row for row in request["candidate_presentations"]}
        claims = [{"product_id": product_id, "text": "Recorded response.", "evidence_ids": presented[product_id]["exposed_evidence_ids"][:1]} for product_id in picked if presented[product_id]["exposed_evidence_ids"]]
        return {"decision": "recommend", "ordered_product_ids": picked, "claims": claims, "uncertainty": "low"}, "recorded-test-model", 0

    def summarized(request):
        return {"headline": "Controlled result.", "gaps": [], "suggestions": [], "revised_title": "PocketVolt", "revised_body": "Verified listing details.", "caveat": "Controlled catalogue only."}, "recorded-test-model", 0

    monkeypatch.setattr("geolab.pipeline.recommend", recorded)
    monkeypatch.setattr("geolab.pipeline.summarize_gap_report", summarized)

    run_id = create_geo_run(form())
    assert await_run(run_id)["status"] == "passed"
    attempts = fetch_all("SELECT query_group_id,condition,request_json FROM llm_attempts WHERE run_id=? ORDER BY id", (run_id,))
    assert len(attempts) == 60
    assert len(live_calls) == 61

    first_group = [row for row in attempts if row["query_group_id"] == attempts[0]["query_group_id"]]
    requests = [json.loads(row["request_json"]) for row in first_group]
    assert all(requests[0]["candidates"] == request["candidates"] for request in requests[1:])
    baseline = {row["product_id"]: row["presentation"] for row in requests[0]["candidate_presentations"]}
    treatment = {row["product_id"]: row["presentation"] for row in requests[2]["candidate_presentations"]}
    put_id = fetch_one("SELECT put_product_id FROM runs WHERE id=?", (run_id,))["put_product_id"]
    assert baseline[put_id] != treatment[put_id]
    assert all(baseline[product_id] == treatment[product_id] for product_id in baseline if product_id != put_id)

    log = fetch_one("SELECT log_text FROM run_jobs WHERE run_id=? AND stage='reason'", (run_id,))["log_text"]
    assert "Rate limited (1/8)" in log

    misleading = json.loads(fetch_one("SELECT data_json FROM metrics WHERE run_id=? AND condition='misleading_control'", (run_id,))["data_json"])
    assert misleading["unsupported_template_claim_rate"] == 1
    assert misleading["valid_attempts"] == 0

    cached_run_id = create_geo_run(form())
    assert await_run(cached_run_id)["status"] == "passed"
    assert len(live_calls) == 61
    assert {row["status"] for row in fetch_all("SELECT status FROM llm_attempts WHERE run_id=?", (cached_run_id,))} == {"cached"}


def test_missing_llm_blocks_run(monkeypatch):
    monkeypatch.setattr("geolab.reasoning.load_env_file", lambda path: None)
    for name in ("GEOLAB_LLM_URL", "GEOLAB_LLM_MODEL", "GEOLAB_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="LLM not configured"):
        create_geo_run(form())


def test_env_file_loads_without_overwriting_shell(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=from-file\nB='quoted value'\n# ignored\n")
    monkeypatch.setenv("A", "from-shell")
    monkeypatch.delenv("B", raising=False)
    load_env_file(path)
    assert os.environ["A"] == "from-shell"
    assert os.environ["B"] == "quoted value"


def test_cancel_is_immediate_for_queued_and_acknowledged_for_running(tmp_path, monkeypatch):
    monkeypatch.setattr("geolab.db.DB_PATH", tmp_path / "cancel.db")
    migrate()
    with connect() as db:
        queued = db.execute("INSERT INTO runs(put_product_id,suite,manifest_hash,manifest_json,status) VALUES('pb','screen','h','{}','queued')").lastrowid
        running = db.execute("INSERT INTO runs(put_product_id,suite,manifest_hash,manifest_json,status) VALUES('pb','screen','h','{}','running')").lastrowid
        db.execute("INSERT INTO run_jobs(run_id,stage,position,status) VALUES(?, 'reason', 0, 'pending')", (queued,))
        db.execute("INSERT INTO run_jobs(run_id,stage,position,status) VALUES(?, 'reason', 0, 'running')", (running,))
    assert cancel_run(queued) == "cancelled"
    assert cancel_run(running) == "cancelling"
    assert fetch_one("SELECT status FROM runs WHERE id=?", (queued,))["status"] == "cancelled"
    assert fetch_one("SELECT status FROM runs WHERE id=?", (running,))["status"] == "cancelling"
