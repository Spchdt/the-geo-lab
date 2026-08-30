import json
import time

from intenttwin.db import all, connect, migrate, one
from intenttwin.pipeline import cancel_run, create_run, suite_path, wait_with_cancel
from intenttwin.reasoning import LLMRequestError, load_env_file


def test_provider_wait_never_sleeps_for_negative_time(monkeypatch):
    clock = iter([0.0, 0.5, 1.1])
    sleeps = []
    monkeypatch.setattr("intenttwin.pipeline.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("intenttwin.pipeline.time.sleep", sleeps.append)
    monkeypatch.setattr("intenttwin.pipeline.cancel_requested", lambda run_id: False)
    assert wait_with_cancel(1, 1.0)
    assert sleeps == [0.25]


def test_all_condition_smoke_pairs_60_calls_and_reuses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("intenttwin.db.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("intenttwin.pipeline.connect", __import__("intenttwin.db", fromlist=["connect"]).connect)
    monkeypatch.setenv("INTENTTWIN_LLM_URL", "https://recorded.test/v1/chat/completions")
    monkeypatch.setenv("INTENTTWIN_LLM_MODEL", "recorded-test-model")
    monkeypatch.setenv("INTENTTWIN_LLM_API_KEY", "test-key")
    monkeypatch.setenv("INTENTTWIN_LLM_MIN_INTERVAL", "0")
    monkeypatch.setenv("INTENTTWIN_LLM_RATE_LIMIT_BASE_DELAY", "0")
    live_calls = []
    rate_limited = True
    def recorded(request):
        nonlocal rate_limited
        live_calls.append(request)
        if rate_limited:
            rate_limited = False
            raise LLMRequestError("LLM HTTP 429: quota exceeded", status_code=429, retry_after=0)
        with connect() as db:
            db.execute("UPDATE runs SET progress=progress WHERE id=(SELECT MAX(id) FROM runs)")
        picked = request["candidates"][:2]
        presented = {row["product_id"]: row for row in request["candidate_presentations"]}
        claims = [{"product_id": product_id, "text": "Recorded response.", "evidence_ids": presented[product_id]["exposed_evidence_ids"][:1]} for product_id in picked if presented[product_id]["exposed_evidence_ids"]]
        return {"decision": "recommend", "ordered_product_ids": picked, "claims": claims, "uncertainty": "low"}, "recorded-test-model", 0
    monkeypatch.setattr("intenttwin.pipeline.recommend", recorded)
    run_id = create_run()
    for _ in range(200):
        run = one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run and run["status"] not in {"queued", "running"}: break
        time.sleep(.03)
    assert run["status"] == "passed"
    assert len(all("SELECT * FROM metrics WHERE run_id=?", (run_id,))) == 5
    attempts = all("SELECT query_group_id,condition,trial,request_json FROM llm_attempts WHERE run_id=? ORDER BY id", (run_id,))
    assert len(attempts) == 60
    first_group = [row for row in attempts if row["query_group_id"] == attempts[0]["query_group_id"]]
    requests = [json.loads(row["request_json"]) for row in first_group]
    assert {row["condition"] for row in first_group} == {"original", "normalized", "grounded_enriched", "identity_copy", "misleading_control"}
    assert __import__("builtins").all(requests[0]["candidates"] == request["candidates"] for request in requests[1:])
    first = {row["product_id"]: row["presentation"] for row in requests[0]["candidate_presentations"]}
    treatment = {row["product_id"]: row["presentation"] for row in requests[2]["candidate_presentations"]}
    assert first["pb-017"] != treatment["pb-017"]
    assert __import__("builtins").all(first[product_id] == treatment[product_id] for product_id in first if product_id != "pb-017")
    assert len(live_calls) == 61
    assert "Rate limited (1/8)" in one("SELECT log_text FROM run_jobs WHERE run_id=? AND stage='reason'", (run_id,))["log_text"]
    misleading = json.loads(one("SELECT data_json FROM metrics WHERE run_id=? AND condition='misleading_control'", (run_id,))["data_json"])
    assert misleading["unsupported_template_claims"] == 12
    assert misleading["unsupported_template_claim_rate"] == 1
    assert misleading["valid_attempts"] == 0

    cached_run_id = create_run()
    for _ in range(200):
        cached_run = one("SELECT * FROM runs WHERE id=?", (cached_run_id,))
        if cached_run and cached_run["status"] not in {"queued", "running"}: break
        time.sleep(.03)
    assert cached_run["status"] == "passed"
    assert len(live_calls) == 61
    assert {row["status"] for row in all("SELECT status FROM llm_attempts WHERE run_id=?", (cached_run_id,))} == {"cached"}


def test_paper_suite_contract_and_sealed_gate(tmp_path, monkeypatch):
    assert sum(1 for line in suite_path("diagnostic").read_text().splitlines() if line) == 24
    assert sum(1 for line in suite_path("demand").read_text().splitlines() if line) == 40
    assert sum(1 for line in suite_path("sealed").read_text().splitlines() if line) == 40
    monkeypatch.setattr("intenttwin.db.DB_PATH", tmp_path / "sealed.db")
    monkeypatch.setattr("intenttwin.pipeline.connect", __import__("intenttwin.db", fromlist=["connect"]).connect)
    monkeypatch.setenv("INTENTTWIN_LLM_URL", "https://recorded.test/v1/chat/completions")
    monkeypatch.setenv("INTENTTWIN_LLM_MODEL", "recorded-test-model")
    monkeypatch.setenv("INTENTTWIN_LLM_API_KEY", "test-key")
    monkeypatch.setattr("intenttwin.pipeline.start_worker", lambda: None)
    demand_id = create_run(suite="demand")
    manifest = json.loads(one("SELECT manifest_json FROM runs WHERE id=?", (demand_id,))["manifest_json"])
    assert manifest["query_groups"] == 40
    assert manifest["trials"] == 3
    assert manifest["llm_policy"]["maximum_calls"] == 600
    try:
        create_run(suite="sealed")
    except ValueError as exc:
        assert "locked demand revision" in str(exc)
    else:
        raise AssertionError("sealed suite opened without a locked demand revision")


def test_missing_llm_blocks_run(monkeypatch):
    monkeypatch.setattr("intenttwin.reasoning.load_env_file", lambda path: None)
    for name in ("INTENTTWIN_LLM_URL", "INTENTTWIN_LLM_MODEL", "INTENTTWIN_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    try:
        create_run()
    except RuntimeError as exc:
        assert "LLM not configured" in str(exc)
    else:
        raise AssertionError("run created without LLM")


def test_env_file_loads_without_overwriting_shell(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=from-file\nB='quoted value'\n# ignored\n")
    monkeypatch.setenv("A", "from-shell")
    monkeypatch.delenv("B", raising=False)
    load_env_file(path)
    assert __import__("os").environ["A"] == "from-shell"
    assert __import__("os").environ["B"] == "quoted value"


def test_cancel_is_immediate_for_queued_and_acknowledged_for_running(tmp_path, monkeypatch):
    monkeypatch.setattr("intenttwin.db.DB_PATH", tmp_path / "cancel.db")
    migrate()
    with connect() as db:
        queued = db.execute("INSERT INTO runs(put_product_id,suite,manifest_hash,manifest_json,status) VALUES('pb','diagnostic','h','{}','queued')").lastrowid
        running = db.execute("INSERT INTO runs(put_product_id,suite,manifest_hash,manifest_json,status) VALUES('pb','diagnostic','h','{}','running')").lastrowid
        db.execute("INSERT INTO run_jobs(run_id,stage,position,status) VALUES(?, 'reason', 0, 'pending')", (queued,))
        db.execute("INSERT INTO run_jobs(run_id,stage,position,status) VALUES(?, 'reason', 0, 'running')", (running,))
    assert cancel_run(queued) == "cancelled"
    assert cancel_run(running) == "cancelling"
    assert one("SELECT status FROM runs WHERE id=?", (queued,))["status"] == "cancelled"
    assert one("SELECT status FROM runs WHERE id=?", (running,))["status"] == "cancelling"
