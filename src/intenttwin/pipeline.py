from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .core import (CONDITIONS, assert_competitors_unchanged, canonical_json, channel_ranks,
                   check_constraint, fuse, load_jsonl, paired_metrics,
                   render_treatment, stable_hash, treatment_claim_errors, validate_recommendation)
from .core import tokens
from .db import ROOT, connect, migrate
from .geo import (CONTROL_CONDITIONS, build_product_submission, controls,
                  gap_for_condition, generate_competitors, generate_queries,
                  generate_variants, submission_form, validate_summary)
from .reasoning import (LLMRequestError, llm_config, recommend,
                        summarize_gap_report)

STAGES = ("validate", "materialize", "retrieve", "reason", "validate_outputs", "analyze", "complete")
LLM_CONDITIONS = CONDITIONS
RUN_PROFILES = {
    "smoke": {"source_suite": "diagnostic", "limit": 12, "trials": 1, "evidence_level": "directional smoke test"},
    "diagnostic": {"source_suite": "diagnostic", "limit": None, "trials": 3, "evidence_level": "descriptive diagnostic"},
    "demand": {"source_suite": "demand", "limit": None, "trials": 3, "evidence_level": "descriptive independent demand"},
    "sealed": {"source_suite": "sealed", "limit": None, "trials": 3, "evidence_level": "descriptive sealed confirmation"},
}
_worker_lock = threading.Lock()
_last_llm_call_at = 0.0


def catalogue_path() -> Path:
    return ROOT / "data" / "catalogue.jsonl"


def suite_path(suite: str) -> Path:
    return ROOT / "data" / "suites" / f"{suite}.jsonl"


def load_fixture() -> None:
    migrate()
    products = load_jsonl(catalogue_path())
    queries = [query for suite in ("diagnostic", "demand", "sealed") for query in load_jsonl(suite_path(suite))]
    catalogue_hash = stable_hash(products)
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO catalogues(id, content_hash) VALUES(?,?)", ("power-banks-v2", catalogue_hash))
        for product in products:
            db.execute("INSERT OR REPLACE INTO products VALUES(?,?,?)", (product["product_id"], "power-banks-v2", canonical_json(product)))
            for fact in product["facts"]:
                db.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?)", (fact["fact_id"], product["product_id"], fact["predicate"], canonical_json(fact["value"]), fact.get("unit"), fact["status"]))
        db.execute("DELETE FROM query_groups")
        for query in queries:
            db.execute("INSERT OR REPLACE INTO query_groups VALUES(?,?,?,?)", (query["query_group_id"], query["suite"], canonical_json(query), stable_hash(query)))


def create_run(put_id: str = "pb-017", suite: str = "smoke", parent_run_id: int | None = None) -> int:
    if suite not in RUN_PROFILES:
        raise ValueError(f"unknown run profile: {suite}")
    config = llm_config()
    load_fixture()
    profile = RUN_PROFILES[suite]
    suite_rows = load_jsonl(suite_path(profile["source_suite"]))
    if profile["limit"]:
        suite_rows = suite_rows[:profile["limit"]]
    if suite == "sealed" and parent_run_id is None:
        with connect() as db:
            locked = db.execute("SELECT id FROM runs WHERE put_product_id=? AND suite='demand' AND status='passed' AND locked=1 ORDER BY id DESC LIMIT 1", (put_id,)).fetchone()
        if not locked:
            raise ValueError("sealed confirmation requires a passed, locked demand revision")
        parent_run_id = int(locked["id"])
    products = load_jsonl(catalogue_path())
    manifest = {
        "catalogue_hash": stable_hash(products),
        "truth_hash": stable_hash([{key: product[key] for key in ("product_id", "facts")} for product in products]),
        "suite_hash": stable_hash(suite_rows),
        "put_product_id": put_id,
        "conditions": CONDITIONS,
        "retrieval": {"semantic_model": "sha256-token-projection-v1", "rrf_k": 60, "reasoning_set_size": 8},
        "recommendation_model": config["model"],
        "llm_policy": {"mode": "all_conditions", "conditions": LLM_CONDITIONS, "trials": profile["trials"], "maximum_calls": len(suite_rows) * len(LLM_CONDITIONS) * profile["trials"]},
        "query_groups": len(suite_rows),
        "trials": profile["trials"],
        "evidence_level": profile["evidence_level"],
        "interpretation": "descriptive_not_confirmatory",
        "analysis_seed": 20260829,
    }
    with connect() as db:
        cursor = db.execute("INSERT INTO runs(put_product_id,suite,manifest_hash,manifest_json,parent_run_id) VALUES(?,?,?,?,?)", (put_id, suite, stable_hash(manifest), canonical_json(manifest), parent_run_id))
        run_id = int(cursor.lastrowid)
        db.executemany("INSERT INTO run_jobs(run_id,stage,position) VALUES(?,?,?)", [(run_id, stage, position) for position, stage in enumerate(STAGES)])
    start_worker()
    return run_id


def create_geo_run(form: dict[str, str], phase: str = "screen", parent_run_id: int | None = None, promoted_conditions: list[str] | None = None) -> int:
    if phase not in {"screen", "confirmation"}:
        raise ValueError("unknown GEO phase")
    config = llm_config()
    load_fixture()
    product = build_product_submission(form)
    queries = generate_queries(product, phase)
    variants = generate_variants(product)
    if phase == "confirmation":
        allowed = ["original", *(promoted_conditions or []), "identity_control"]
        variants = [variant for variant in variants if variant["condition"] in allowed]
        if len(variants) != 4:
            raise ValueError("confirmation requires two promoted variants")
    conditions = [variant["condition"] for variant in variants]
    catalogue = generate_competitors(product)
    manifest = {
        "experiment_version": "geo-gap-v1",
        "phase": phase,
        "category_pack": "generic-marketplace-v1",
        "catalogue_hash": stable_hash(catalogue),
        "truth_hash": stable_hash({"competitors": [{key: row[key] for key in ("product_id", "facts")} for row in catalogue], "product": product}),
        "competitor_snapshots": catalogue,
        "suite_hash": stable_hash(queries),
        "put_product_id": product["product_id"],
        "product_snapshot": product,
        "queries": queries,
        "variants": variants,
        "conditions": conditions,
        "retrieval": {"semantic_model": "sha256-token-projection-v1", "rrf_k": 60, "reasoning_set_size": 8},
        "recommendation_model": config["model"],
        "llm_policy": {"mode": "all_screen_variants" if phase == "screen" else "promoted_confirmation", "conditions": conditions, "trials": 1, "maximum_calls": len(queries) * len(conditions) + 1},
        "query_groups": len(queries),
        "trials": 1,
        "evidence_level": "directional screen" if phase == "screen" else "descriptive held-out confirmation",
        "interpretation": "controlled_catalogue_not_public_geo",
        "analysis_seed": 20260829,
    }
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO catalogues(id,content_hash) VALUES(?,?)", ("generic-marketplace-v1", stable_hash(catalogue)))
        for competitor in catalogue:
            db.execute("INSERT OR REPLACE INTO products VALUES(?,?,?)", (competitor["product_id"], "generic-marketplace-v1", canonical_json(competitor)))
            for fact in competitor["facts"]:
                db.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?)", (fact["fact_id"], competitor["product_id"], fact["predicate"], canonical_json(fact["value"]), fact.get("unit"), fact["status"]))
        db.execute("INSERT OR REPLACE INTO products VALUES(?,?,?)", (product["product_id"], "generic-marketplace-v1", canonical_json(product)))
        for fact in product["facts"]:
            db.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?)", (fact["fact_id"], product["product_id"], fact["predicate"], canonical_json(fact["value"]), fact.get("unit"), fact["status"]))
        cursor = db.execute("INSERT INTO runs(put_product_id,suite,manifest_hash,manifest_json,parent_run_id) VALUES(?,?,?,?,?)", (product["product_id"], phase, stable_hash(manifest), canonical_json(manifest), parent_run_id))
        run_id = int(cursor.lastrowid)
        db.executemany("INSERT INTO run_jobs(run_id,stage,position) VALUES(?,?,?)", [(run_id, stage, position) for position, stage in enumerate(STAGES)])
    start_worker()
    return run_id


def create_confirmation_run(parent_run_id: int) -> int:
    with connect() as db:
        row = db.execute("SELECT status,manifest_json FROM runs WHERE id=?", (parent_run_id,)).fetchone()
    if not row:
        raise ValueError("screen run not found")
    manifest = json.loads(row["manifest_json"])
    if row["status"] != "passed" or manifest.get("phase") != "screen":
        raise ValueError("confirmation requires a passed screen run")
    report = load_gap_report(parent_run_id)
    promoted = report.get("promotion_conditions", []) if report else []
    if len(promoted) != 2:
        raise ValueError("screen did not produce two promotable variants")
    return create_geo_run(submission_form(manifest["product_snapshot"]), "confirmation", parent_run_id, promoted)


def start_worker() -> None:
    if _worker_lock.locked():
        return
    threading.Thread(target=work_once, daemon=True).start()


def cancel_run(run_id: int) -> str:
    with connect() as db:
        run = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError("run not found")
        if run["status"] == "queued":
            db.execute("UPDATE runs SET cancel_requested=1,status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
            db.execute("UPDATE run_jobs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE run_id=? AND status='pending'", (run_id,))
            return "cancelled"
        if run["status"] in {"running", "cancelling"}:
            db.execute("UPDATE runs SET cancel_requested=1,status='cancelling' WHERE id=?", (run_id,))
            return "cancelling"
        return run["status"]


def cancel_requested(run_id: int) -> bool:
    with connect() as db:
        return bool(db.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()[0])


def append_log(run_id: int, stage: str, message: str) -> None:
    with connect() as db:
        db.execute("UPDATE run_jobs SET started_at=CURRENT_TIMESTAMP,log_text=CASE WHEN log_text='' THEN ? ELSE log_text||char(10)||? END WHERE run_id=? AND stage=?", (message, message, run_id, stage))


def wait_with_cancel(run_id: int, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if cancel_requested(run_id):
            return False
        time.sleep(min(0.25, remaining))
    return not cancel_requested(run_id)


def wait_for_llm_slot(run_id: int) -> bool:
    global _last_llm_call_at
    interval = max(0.0, float(os.getenv("INTENTTWIN_LLM_MIN_INTERVAL", "4.2")))
    remaining = interval - (time.monotonic() - _last_llm_call_at)
    if remaining > 0:
        append_log(run_id, "reason", f"Pacing provider calls · waiting {remaining:.1f}s")
        if not wait_with_cancel(run_id, remaining):
            return False
    _last_llm_call_at = time.monotonic()
    return True


def work_once() -> None:
    if not _worker_lock.acquire(blocking=False):
        return
    try:
        with connect() as db:
            row = db.execute("SELECT id FROM runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return
            run_id = row["id"]
            db.execute("UPDATE runs SET status='running',started_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        for position, stage in enumerate(STAGES):
            if cancel_requested(run_id):
                with connect() as db:
                    db.execute("UPDATE runs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
                    db.execute("UPDATE run_jobs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE run_id=? AND status IN ('running','pending')", (run_id,))
                return
            with connect() as db:
                db.execute("UPDATE run_jobs SET status='running',started_at=CURRENT_TIMESTAMP,log_text=? WHERE run_id=? AND stage=?", (f"Starting {stage}", run_id, stage))
            try:
                globals()[f"stage_{stage}"](run_id)
            except Exception as exc:
                with connect() as db:
                    db.execute("UPDATE run_jobs SET status='failed',completed_at=CURRENT_TIMESTAMP,error_text=? WHERE run_id=? AND stage=?", (str(exc), run_id, stage))
                    db.execute("UPDATE runs SET status='failed',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
                return
            if cancel_requested(run_id):
                with connect() as db:
                    db.execute("UPDATE run_jobs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE run_id=? AND stage=?", (run_id, stage))
                    db.execute("UPDATE runs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
                return
            with connect() as db:
                message = f"{stage} complete"
                db.execute("UPDATE run_jobs SET status='passed',completed_at=CURRENT_TIMESTAMP,log_text=CASE WHEN log_text='' THEN ? ELSE log_text||char(10)||? END WHERE run_id=? AND stage=?", (message, message, run_id, stage))
                db.execute("UPDATE runs SET progress=? WHERE id=?", (round((position + 1) / len(STAGES) * 100), run_id))
            time.sleep(0.08)
    finally:
        _worker_lock.release()
        start_worker()


def context(run_id: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    with connect() as db:
        run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        manifest = json.loads(run["manifest_json"])
        if manifest.get("experiment_version") == "geo-gap-v1":
            product_rows = manifest["competitor_snapshots"] + [manifest["product_snapshot"]]
            products = {row["product_id"]: row for row in product_rows}
            queries = manifest["queries"]
        else:
            products = {row["product_id"]: row for row in load_jsonl(catalogue_path())}
            profile = RUN_PROFILES[run["suite"]]
            queries = [json.loads(row["data_json"]) for row in db.execute("SELECT * FROM query_groups WHERE suite=? ORDER BY query_group_id", (profile["source_suite"],))]
            if profile["limit"]:
                queries = queries[:profile["limit"]]
    return run, products, queries


def run_conditions(run: dict[str, Any]) -> tuple[str, ...]:
    manifest = json.loads(run["manifest_json"])
    return tuple(manifest.get("conditions", CONDITIONS))


def stage_validate(run_id: int) -> None:
    run, products, queries = context(run_id)
    if run["put_product_id"] not in products or len(products) < 30 or not queries:
        raise ValueError("fixture contract failed")
    for query in queries:
        if check_constraint(products[run["put_product_id"]], query["hard_constraints"])[0] != "PASS":
            raise ValueError(f"generated query is not relevant to product: {query['query_group_id']}")


def stage_materialize(run_id: int) -> None:
    run, products, _ = context(run_id)
    put_id = run["put_product_id"]
    manifest = json.loads(run["manifest_json"])
    conditions = run_conditions(run)
    variant_by_condition = {variant["condition"]: variant for variant in manifest.get("variants", [])}
    presentations = []
    for condition in conditions:
        for product in products.values():
            if product["product_id"] == put_id and variant_by_condition:
                variant = variant_by_condition[condition]
                item = {
                    "presentation_id": f"{product['product_id']}:{condition}:v1",
                    "product_id": product["product_id"], "condition": condition, "title": product["name"],
                    "body": variant["body"], "exposed_fact_ids": variant["exposed_fact_ids"], "content_hash": variant["content_hash"],
                }
            elif variant_by_condition:
                item = render_treatment(product, product["original_presentation"], "original", False)
                item["condition"] = condition
                item["presentation_id"] = f"{product['product_id']}:{condition}:v1"
            else:
                item = render_treatment(product, product["original_presentation"], condition, product["product_id"] == put_id)
            errors = treatment_claim_errors(product, item)
            if errors and condition not in {"misleading_control", "misleading_control"}:
                raise ValueError(errors[0])
            if condition in {"misleading_control", "misleading_control"} and product["product_id"] == put_id and not errors:
                raise ValueError("misleading typed control escaped validation")
            presentations.append(item)
    assert_competitors_unchanged(presentations, put_id)
    originals = {item["product_id"]: item for item in presentations if item["condition"] == "original"}
    identity_condition = "identity_control" if "identity_control" in conditions else "identity_copy"
    identity = {item["product_id"]: item for item in presentations if item["condition"] == identity_condition}
    if identity and any(originals[key]["body"] != identity[key]["body"] for key in originals):
        raise ValueError("identity treatment differs")
    with connect() as db:
        db.execute("DELETE FROM presentations")
        for item in presentations:
            db.execute("INSERT OR REPLACE INTO presentations VALUES(?,?,?,?,?,?)", (item["presentation_id"], item["product_id"], item["condition"], item["body"], canonical_json(item["exposed_fact_ids"]), item["content_hash"]))
        for condition in conditions:
            table = f"fts_{condition}"
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(product_id UNINDEXED,title,body,tokenize='unicode61')")
            db.execute(f"DELETE FROM {table}")
            db.execute(f"INSERT INTO {table}(product_id,title,body) SELECT product_id,'',body FROM presentations WHERE condition=?", (condition,))


def stage_retrieve(run_id: int) -> None:
    run, products, queries = context(run_id)
    conditions = run_conditions(run)
    with connect() as db:
        for query in queries:
            for condition in conditions:
                presentations = [dict(row) for row in db.execute("SELECT * FROM presentations WHERE condition=? ORDER BY product_id", (condition,))]
                for item in presentations:
                    item["exposed_fact_ids"] = json.loads(item["exposed_json"])
                channels = channel_ranks(query, presentations, products)
                table = f"fts_{condition}"
                match = " OR ".join(tokens(query["text"]))
                lexical = db.execute(f"SELECT product_id,bm25({table},1.0,2.0) AS score FROM {table} WHERE {table} MATCH ? ORDER BY score,product_id", (match,)).fetchall()
                matched = [(row["product_id"], row["score"]) for row in lexical]
                seen = {product_id for product_id, _ in matched}
                channels["lexical"] = matched + [(item["product_id"], 0.0) for item in presentations if item["product_id"] not in seen]
                fused = fuse(channels)
                for channel, rows in {**channels, "fused": fused}.items():
                    db.executemany("INSERT OR REPLACE INTO retrieval_traces VALUES(?,?,?,?,?,?,?)", [(run_id, query["query_group_id"], condition, channel, product_id, score, rank) for rank, (product_id, score) in enumerate(rows, 1)])
                for product in products.values():
                    outcome, reasons = check_constraint(product, query["hard_constraints"])
                    db.execute("INSERT OR REPLACE INTO constraint_traces VALUES(?,?,?,?,?,?)", (run_id, query["query_group_id"], condition, product["product_id"], outcome, canonical_json(reasons)))
        original = db.execute("SELECT query_group_id,product_id,rank FROM retrieval_traces WHERE run_id=? AND condition='original' AND channel='fused' ORDER BY query_group_id,rank", (run_id,)).fetchall()
        identity_condition = "identity_control" if "identity_control" in conditions else "identity_copy"
        identity = db.execute("SELECT query_group_id,product_id,rank FROM retrieval_traces WHERE run_id=? AND condition=? AND channel='fused' ORDER BY query_group_id,rank", (run_id, identity_condition)).fetchall()
        if identity and [tuple(row) for row in original] != [tuple(row) for row in identity]:
            raise ValueError("identity-copy retrieval differs")


def stage_reason(run_id: int) -> None:
    run, products, queries = context(run_id)
    manifest = json.loads(run["manifest_json"])
    llm_conditions = tuple(manifest.get("llm_policy", {}).get("conditions", LLM_CONDITIONS))
    trials = manifest["trials"]
    eligible_queries = [query for query in queries if check_constraint(products[run["put_product_id"]], query["hard_constraints"])[0] == "PASS"]
    total = len(eligible_queries) * len(llm_conditions) * trials
    completed = 0
    model_id = manifest["recommendation_model"]
    append_log(run_id, "reason", f"{manifest['evidence_level']}: {total} calls · {len(eligible_queries)}/{len(queries)} canonically eligible groups · {trials} trial(s)")
    if not total:
        return
    for query_index, query in enumerate(eligible_queries, 1):
        with connect() as db:
            ranked = [row["product_id"] for row in db.execute("SELECT product_id FROM retrieval_traces WHERE run_id=? AND query_group_id=? AND condition='original' AND channel='fused' ORDER BY rank", (run_id, query["query_group_id"]))]
            outcomes = {row["product_id"]: row["outcome"] for row in db.execute("SELECT product_id,outcome FROM constraint_traces WHERE run_id=? AND query_group_id=? AND condition='original'", (run_id, query["query_group_id"]))}
        fixed_candidates = [product_id for product_id in ranked if outcomes[product_id] != "FAIL"][:8]
        if run["put_product_id"] not in fixed_candidates:
            fixed_candidates = [run["put_product_id"], *fixed_candidates[:7]]
        treatment_order = llm_conditions if query_index % 2 else tuple(reversed(llm_conditions))
        for trial in range(trials):
            competitors = [product_id for product_id in fixed_candidates if product_id != run["put_product_id"]]
            put_position = (query_index - 1 + trial) % len(fixed_candidates)
            paired_candidates = competitors[:put_position] + [run["put_product_id"]] + competitors[put_position:]
            for condition_index, condition in enumerate(treatment_order, 1):
                with connect() as db:
                    exposed = {row["product_id"]: set(json.loads(row["exposed_json"])) for row in db.execute("SELECT product_id,exposed_json FROM presentations WHERE condition=?", (condition,))}
                    bodies = {row["product_id"]: row["body"] for row in db.execute("SELECT product_id,body FROM presentations WHERE condition=?", (condition,))}
                if cancel_requested(run_id):
                    return
                request = {
                    "query": query,
                    "candidates": paired_candidates,
                    "candidate_presentations": [{"product_id": product_id, "presentation": bodies[product_id], "exposed_evidence_ids": sorted(exposed[product_id]), "constraint_outcome": outcomes[product_id]} for product_id in paired_candidates],
                    "execution": {"prompt_version": "1", "schema_version": "1", "temperature": 0, "trial": trial + 1},
                }
                request_hash = stable_hash(request)
                with connect() as db:
                    cached = db.execute("SELECT response_json,provider_model FROM llm_attempts WHERE request_hash=? AND condition=? AND provider_model=? AND status IN ('completed','cached') ORDER BY id LIMIT 1", (request_hash, condition, model_id)).fetchone()
                output = json.loads(cached["response_json"]) if cached else None
                model = cached["provider_model"] if cached else None
                token_count = 0
                status = "cached" if cached else "completed"
                if cached:
                    append_log(run_id, "reason", f"Query {query_index}/{len(eligible_queries)} · trial {trial + 1}/{trials} · {condition} · cache hit")
                else:
                    attempt_number = 0
                    rate_limit_count = 0
                    rate_limit_retries = max(0, int(os.getenv("INTENTTWIN_LLM_RATE_LIMIT_RETRIES", "8")))
                    while attempt_number < 3:
                        if not wait_for_llm_slot(run_id):
                            return
                        append_log(run_id, "reason", f"Query {query_index}/{len(eligible_queries)} · trial {trial + 1}/{trials} · {condition} ({condition_index}/{len(llm_conditions)}) · transport attempt {attempt_number + 1}/3")
                        try:
                            output, model, token_count = recommend(request)
                            break
                        except LLMRequestError as exc:
                            if exc.status_code == 429 and rate_limit_count < rate_limit_retries:
                                base_delay = max(0.0, float(os.getenv("INTENTTWIN_LLM_RATE_LIMIT_BASE_DELAY", "5")))
                                delay = exc.retry_after if exc.retry_after is not None else min(60.0, base_delay * (2 ** rate_limit_count))
                                rate_limit_count += 1
                                append_log(run_id, "reason", f"Rate limited ({rate_limit_count}/{rate_limit_retries}): {exc} · waiting {delay:.1f}s before resuming")
                                if not wait_with_cancel(run_id, delay):
                                    return
                                continue
                            attempt_number += 1
                            append_log(run_id, "reason", f"Attempt failed: {exc}")
                            if cancel_requested(run_id):
                                return
                            if attempt_number == 3:
                                raise
                if cancel_requested(run_id):
                    return
                with connect() as db:
                    cursor = db.execute("INSERT OR IGNORE INTO llm_attempts(run_id,query_group_id,condition,trial,request_hash,request_json,response_json,provider_model,status,latency_ms,token_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, query["query_group_id"], condition, trial, request_hash, canonical_json(request), canonical_json(output), model, status, 0, token_count))
                    if cursor.lastrowid:
                        db.execute("INSERT INTO recommendations VALUES(?,?)", (cursor.lastrowid, canonical_json(output)))
                    completed += 1
                    db.execute("UPDATE runs SET progress=? WHERE id=?", (43 + round(completed / total * 14), run_id))
                append_log(run_id, "reason", f"Completed {completed}/{total} condition calls · {token_count} tokens")


def stage_validate_outputs(run_id: int) -> None:
    run, products, _ = context(run_id)
    with connect() as db:
        attempts = db.execute("SELECT * FROM llm_attempts WHERE run_id=?", (run_id,)).fetchall()
        for attempt in attempts:
            request, output = json.loads(attempt["request_json"]), json.loads(attempt["response_json"])
            condition, query_id = attempt["condition"], attempt["query_group_id"]
            exposed = {row["product_id"]: set(json.loads(row["exposed_json"])) for row in db.execute("SELECT product_id,exposed_json FROM presentations WHERE condition=?", (condition,))}
            outcomes = {row["product_id"]: row["outcome"] for row in db.execute("SELECT product_id,outcome FROM constraint_traces WHERE run_id=? AND query_group_id=? AND condition=?", (run_id, query_id, condition))}
            errors = validate_recommendation(output, set(request["candidates"]), exposed, products, outcomes)
            put_presentation = db.execute("SELECT body,exposed_json FROM presentations WHERE product_id=? AND condition=?", (run["put_product_id"], condition)).fetchone()
            presentation = {"condition": condition, "body": put_presentation["body"], "exposed_fact_ids": json.loads(put_presentation["exposed_json"])}
            errors.extend(f"unsupported template claim: {error}" for error in treatment_claim_errors(products[run["put_product_id"]], presentation))
            db.execute("INSERT OR REPLACE INTO validations VALUES(?,?,?)", (attempt["id"], not errors, canonical_json(errors)))


def stage_analyze(run_id: int) -> None:
    run, products, queries = context(run_id)
    put_id = run["put_product_id"]
    manifest = json.loads(run["manifest_json"])
    conditions = run_conditions(run)
    with connect() as db:
        rows = [{"query_group_id": row["query_group_id"], "condition": row["condition"], **json.loads(row["response_json"])} for row in db.execute("SELECT query_group_id,condition,response_json FROM llm_attempts WHERE run_id=?", (run_id,))]
        metrics = paired_metrics(rows, put_id, conditions=conditions)
        eligible_ids = {query["query_group_id"] for query in queries if check_constraint(products[put_id], query["hard_constraints"])[0] == "PASS"}
        retrieval: dict[str, dict[str, dict[str, float]]] = {condition: {} for condition in conditions}
        for condition in conditions:
            for query_id in eligible_ids:
                retained = [row["product_id"] for row in db.execute("SELECT r.product_id FROM retrieval_traces r JOIN constraint_traces c ON c.run_id=r.run_id AND c.query_group_id=r.query_group_id AND c.condition=r.condition AND c.product_id=r.product_id WHERE r.run_id=? AND r.query_group_id=? AND r.condition=? AND r.channel='fused' AND c.outcome!='FAIL' ORDER BY r.rank", (run_id, query_id, condition))]
                rank = retained.index(put_id) + 1 if put_id in retained else None
                retrieval[condition][query_id] = {"mrr": 1 / rank if rank else 0.0, "top3": float(rank is not None and rank <= 3), "top8": float(rank is not None and rank <= 8), "rank": rank or 0}
        original_retrieval = retrieval["original"]
        validation_rows = db.execute("SELECT a.condition,v.valid,v.errors_json FROM validations v JOIN llm_attempts a ON a.id=v.attempt_id WHERE a.run_id=?", (run_id,)).fetchall()
        for metric in metrics:
            condition = metric["condition"]
            values = retrieval[condition]
            metric["eligible"] = len(eligible_ids)
            metric["included"] = int(sum(value["top8"] for value in values.values()))
            metric["inclusion_rate"] = round(sum(value["top8"] for value in values.values()) / len(values), 4) if values else 0
            metric["inclusion_delta"] = round(sum(values[qid]["top8"] - original_retrieval[qid]["top8"] for qid in eligible_ids) / len(eligible_ids), 4) if eligible_ids else 0
            metric["retrieval_top3_rate"] = round(sum(value["top3"] for value in values.values()) / len(values), 4) if values else 0
            metric["retrieval_top3_delta"] = round(sum(values[qid]["top3"] - original_retrieval[qid]["top3"] for qid in eligible_ids) / len(eligible_ids), 4) if eligible_ids else 0
            metric["retrieval_mrr"] = round(sum(value["mrr"] for value in values.values()) / len(values), 4) if values else 0
            metric["retrieval_mrr_delta"] = round(sum(values[qid]["mrr"] - original_retrieval[qid]["mrr"] for qid in eligible_ids) / len(eligible_ids), 4) if eligible_ids else 0
            condition_validations = [row for row in validation_rows if row["condition"] == condition]
            metric["valid_attempts"] = sum(row["valid"] for row in condition_validations)
            metric["attempts"] = len(condition_validations)
            metric["unsupported_template_claims"] = sum("unsupported template claim" in row["errors_json"] for row in condition_validations)
            metric["unsupported_template_claim_rate"] = round(metric["unsupported_template_claims"] / metric["attempts"], 4) if metric["attempts"] else 0
            metric["invalid_citations"] = sum("evidence" in row["errors_json"] for row in condition_validations)
            metric["invalid_citation_rate"] = round(metric["invalid_citations"] / metric["attempts"], 4) if metric["attempts"] else 0
            metric["hard_constraint_violations"] = sum("failed constraint" in row["errors_json"] for row in condition_validations)
            metric["hard_constraint_violation_rate"] = round(metric["hard_constraint_violations"] / metric["attempts"], 4) if metric["attempts"] else 0
            metric["invalid_response_rate"] = round((metric["attempts"] - metric["valid_attempts"]) / metric["attempts"], 4) if metric["attempts"] else 0
            metric["evidence_level"] = manifest["evidence_level"]
            metric["interpretation"] = "insufficient_independent_groups" if metric["groups"] < 2 else "descriptive_not_confirmatory"
        for metric in metrics:
            db.execute("INSERT OR REPLACE INTO metrics VALUES(?,?,?)", (run_id, metric["condition"], canonical_json(metric)))
    artifact_dir = ROOT / "data" / "artifacts" / str(run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / "metrics.json"
    csv_path = artifact_dir / "metrics.csv"
    json_path.write_text(json.dumps(metrics, indent=2))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=metrics[0].keys())
    writer.writeheader(); writer.writerows(metrics)
    csv_path.write_text(buffer.getvalue())
    paths = [json_path, csv_path]
    if manifest.get("experiment_version") == "geo-gap-v1":
        report = build_gap_report(run_id, run, products[put_id], queries, metrics, retrieval)
        report_path = artifact_dir / "gap_report.json"
        report_path.write_text(json.dumps(report, indent=2))
        paths.append(report_path)
    with connect() as db:
        for path in paths:
            db.execute("INSERT INTO artifacts(run_id,name,path,content_hash) VALUES(?,?,?,?)", (run_id, path.name, str(path), stable_hash(path.read_bytes())))


def build_gap_report(run_id: int, run: dict[str, Any], product: dict[str, Any], queries: list[dict[str, Any]], metrics: list[dict[str, Any]], retrieval: dict[str, dict[str, dict[str, float]]]) -> dict[str, Any]:
    candidates = [metric for metric in metrics if metric["condition"] != "original" and not controls(metric["condition"]) and metric.get("unsupported_template_claims", 0) == 0 and metric.get("valid_attempts", 0) == metric.get("attempts", 0)]
    candidates.sort(key=lambda metric: (metric.get("retrieval_mrr_delta", 0), metric.get("retrieval_top3_delta", 0), metric.get("top3_delta", 0), metric["condition"]), reverse=True)
    baseline = next(metric for metric in metrics if metric["condition"] == "original")
    best = candidates[0] if candidates else baseline
    winner = best if best.get("retrieval_mrr_delta", 0) > 0 or best.get("top3_delta", 0) > 0 or best.get("recommendation_mrr_delta", 0) > 0 else baseline
    retrieval_up = winner.get("retrieval_mrr_delta", 0) > 0
    gemini_up = winner.get("top3_delta", 0) > 0 or winner.get("recommendation_mrr_delta", 0) > 0
    if retrieval_up and gemini_up:
        evidence_status = "supported"
    elif retrieval_up:
        evidence_status = "discoverability_gap"
    elif gemini_up:
        evidence_status = "persuasion_gap"
    else:
        evidence_status = "mixed_or_inconclusive"
    improved_queries = [query["query_group_id"] for query in queries if retrieval[winner["condition"]][query["query_group_id"]]["mrr"] > retrieval["original"][query["query_group_id"]]["mrr"]]
    gap_name, action = gap_for_condition(winner["condition"])
    variants = {variant["condition"]: variant for variant in json.loads(run["manifest_json"])["variants"]}
    original_variant = variants["original"]
    winning_variant = variants.get(winner["condition"], original_variant)
    facts_by_id = {fact["fact_id"]: fact for fact in product["facts"]}
    added_ids = set(winning_variant["exposed_fact_ids"]) - set(original_variant["exposed_fact_ids"])
    removed_ids = set(original_variant["exposed_fact_ids"]) - set(winning_variant["exposed_fact_ids"])
    difference_notes = []
    if winner["condition"] == "original":
        difference_notes.append("No tested variant clearly beat the submitted listing in this run.")
    else:
        difference_notes.append(f"Presentation: {gap_name}. {action}")
        if added_ids:
            labels = [facts_by_id[fact_id].get("label", facts_by_id[fact_id]["predicate"].replace("_", " ").title()) for fact_id in sorted(added_ids)]
            difference_notes.append("Facts made explicit: " + ", ".join(labels) + ".")
        if removed_ids:
            labels = [facts_by_id[fact_id].get("label", facts_by_id[fact_id]["predicate"].replace("_", " ").title()) for fact_id in sorted(removed_ids)]
            difference_notes.append("Facts no longer explicit: " + ", ".join(labels) + ".")
        original_words = len(original_variant["body"].split())
        winning_words = len(winning_variant["body"].split())
        difference_notes.append(f"Listing length: {original_words} words → {winning_words} words.")
    report: dict[str, Any] = {
        "summary_status": "pending",
        "winner": winner["condition"],
        "evidence_status": evidence_status,
        "gap_name": gap_name,
        "action": action,
        "supporting_query_ids": improved_queries,
        "promotion_conditions": [metric["condition"] for metric in candidates[:2]],
        "winner_metrics": {key: winner.get(key) for key in ("retrieval_mrr_delta", "retrieval_top3_delta", "top1_delta", "top3_delta", "recommendation_mrr_delta")},
        "original_listing": original_variant["body"],
        "winning_listing": winning_variant["body"],
        "difference_notes": difference_notes,
        "variant_explanations": build_variant_explanations(run_id, run["put_product_id"], metrics),
        "suggested_listing": winning_variant["body"],
        "limitation": "Controlled generic-marketplace-v1 catalogue and configured model only; this does not measure public GEO ranking, indexing, conversion, or sales.",
    }
    report["human_explanation"] = build_human_explanation(report, json.loads(run["manifest_json"]))
    request = {"canonical_product": product, "original_listing": product["original_presentation"], "deterministic_report": report, "winning_variant": variants.get(winner["condition"]), "queries": queries}
    try:
        summary, model, tokens_used = summarize_gap_report(request)
        errors = validate_summary(summary, product, queries)
        if errors:
            raise ValueError(errors[0])
        report.update({"summary_status": "completed", "llm_summary": summary, "summary_model": model, "summary_tokens": tokens_used})
    except (LLMRequestError, ValueError) as exc:
        report.update({"summary_status": "failed", "summary_error": str(exc)})
        append_log(run_id, "analyze", f"Gap summary unavailable: {exc}")
    return report


def build_variant_explanations(run_id: int, product_id: str, metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Explain each variant using only paired retrieval and Gemini results from this run."""
    with connect() as db:
        retrieval_rows = db.execute(
            "SELECT query_group_id,condition,rank FROM retrieval_traces "
            "WHERE run_id=? AND channel='fused' AND product_id=?",
            (run_id, product_id),
        ).fetchall()
        attempt_rows = db.execute(
            "SELECT query_group_id,condition,response_json FROM llm_attempts "
            "WHERE run_id=? AND status IN ('completed','cached')",
            (run_id,),
        ).fetchall()

    retrieval_ranks = {(row["condition"], row["query_group_id"]): row["rank"] for row in retrieval_rows}
    gemini_scores: dict[tuple[str, str], list[float]] = {}
    for row in attempt_rows:
        ordered = json.loads(row["response_json"]).get("ordered_product_ids", [])
        rank = ordered.index(product_id) + 1 if product_id in ordered else None
        gemini_scores.setdefault((row["condition"], row["query_group_id"]), []).append(1 / rank if rank else 0)

    def paired_query_ids(condition: str, source: dict[tuple[str, str], Any]) -> tuple[list[str], list[str]]:
        query_ids = sorted(query_id for candidate, query_id in source if candidate == "original")
        improved, worsened = [], []
        for query_id in query_ids:
            original = source.get(("original", query_id))
            variant = source.get((condition, query_id))
            if original is None or variant is None:
                continue
            if source is retrieval_ranks:
                variant, original = -variant, -original  # Lower retrieval rank is better.
            else:
                original = sum(original) / len(original)
                variant = sum(variant) / len(variant)
            if variant > original:
                improved.append(query_id)
            elif variant < original:
                worsened.append(query_id)
        return improved, worsened

    explanations: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        condition = metric["condition"]
        retrieval_up, retrieval_down = paired_query_ids(condition, retrieval_ranks)
        gemini_up, gemini_down = paired_query_ids(condition, gemini_scores)
        retrieval_delta = metric.get("retrieval_mrr_delta", 0)
        gemini_delta = metric.get("top3_delta", 0)
        if condition == "original":
            summary = "Baseline submitted listing used for every paired comparison."
        elif condition == "identity_control":
            summary = "Null control: identical listing text; any Gemini difference is model variability, not a content effect."
        elif condition == "misleading_control":
            summary = "Safety control with a deliberately false claim; it is validation evidence and cannot win."
        elif retrieval_delta > 0 and gemini_delta > 0:
            summary = "Improved both retrieval discoverability and Gemini top-three selection versus the original."
        elif retrieval_delta > 0:
            summary = "Improved retrieval, but Gemini top-three selection did not improve."
        elif gemini_delta > 0:
            summary = "Improved Gemini top-three selection without a retrieval gain."
        elif retrieval_delta < 0 and gemini_delta < 0:
            summary = "Performed worse than the original in both retrieval and Gemini top-three selection."
        else:
            summary = "No clear overall gain; the retrieval and Gemini signals were tied or mixed."
        explanations[condition] = {
            "summary": summary,
            "retrieval_improved_query_ids": retrieval_up,
            "retrieval_worsened_query_ids": retrieval_down,
            "gemini_improved_query_ids": gemini_up,
            "gemini_worsened_query_ids": gemini_down,
        }
    return explanations


def build_human_explanation(report: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Translate the winning paired evidence into short, shopper-friendly conclusions."""
    winner = report["winner"]
    evidence = report.get("variant_explanations", {}).get(winner, {})
    metrics = report.get("winner_metrics", {})
    query_text = {query["query_group_id"]: query["text"] for query in manifest.get("queries", [])}
    query_count = len(query_text)

    def themes(ids: list[str]) -> str:
        found = []
        for query_id in ids:
            text = query_text.get(query_id, "").lower()
            if any(word in text for word in ("price", "cost", "under ", "sgd", "$")):
                theme = "price"
            elif any(word in text for word in ("detail", "describ", "spec")):
                theme = "product-detail"
            elif any(word in text for word in ("compare", "options", "alternative")):
                theme = "comparison"
            elif any(word in text for word in ("everyday", "regular", "daily")):
                theme = "everyday-use"
            else:
                theme = "general shopping"
            if theme not in found:
                found.append(theme)
        if not found:
            return ""
        return ", ".join(found[:-1]) + (f" and {found[-1]}" if len(found) > 1 else found[0])

    retrieval_delta = float(metrics.get("retrieval_mrr_delta") or 0)
    gemini_top3_delta = float(metrics.get("top3_delta") or 0)
    retrieval_up_ids = evidence.get("retrieval_improved_query_ids", [])
    retrieval_down_ids = evidence.get("retrieval_worsened_query_ids", [])
    gemini_up_ids = evidence.get("gemini_improved_query_ids", [])
    gemini_down_ids = evidence.get("gemini_worsened_query_ids", [])

    if winner == "original":
        return [
            {"label": "Result", "text": "The original listing was still the strongest version tested."},
            {"label": "What it means", "text": "None of the rewrites made the product clearly easier to find and more likely to be recommended."},
        ]

    if retrieval_delta > 0 and gemini_top3_delta > 0:
        result = "This version was easier to find and more likely to be recommended."
    elif retrieval_delta > 0 and gemini_top3_delta < 0:
        result = "This version was easier to find, but less likely to be recommended."
    elif retrieval_delta > 0:
        result = "This version was easier to find, but no more likely to be recommended."
    elif gemini_top3_delta > 0:
        result = "This version was harder to find, but more convincing once Gemini saw it."
    else:
        result = "This version did not clearly improve on the original listing."

    if retrieval_up_ids:
        theme_text = themes(retrieval_up_ids)
        why = f"It moved up in {len(retrieval_up_ids)} of {query_count} tested searches"
        if theme_text:
            why += f", mainly for {theme_text} searches"
        why += "."
        if retrieval_down_ids:
            why += f" It also moved down in {len(retrieval_down_ids)}, so the improvement was uneven."
    else:
        why = "The rewrite did not help the product move up in the tested searches."

    if retrieval_delta > 0 and gemini_top3_delta < 0:
        meaning = f"The wording matched shopper searches better, but Gemini ranked it lower on {len(gemini_down_ids)} of {query_count} searches. The product became more visible, but the listing was not convincing enough against competitors."
    elif retrieval_delta > 0 and gemini_top3_delta > 0:
        meaning = f"The change helped shoppers find the product and helped Gemini prefer it on {len(gemini_up_ids)} of {query_count} searches."
    elif retrieval_delta > 0:
        meaning = "The change helped discovery only. Once Gemini compared products, it found no stronger reason to choose this one."
    elif gemini_top3_delta > 0:
        meaning = "The listing was persuasive when seen, but it needs clearer search language so it can be found more reliably."
    else:
        meaning = "The tested change did not solve a clear product-listing gap."

    if winner == "intent_aligned":
        action = "Use customer search language in the title and description, but keep the concrete specifications and benefits that help the product win after it is found."
    elif winner == "normalized_same_facts":
        action = "Keep the clearer structure while preserving the specific facts that distinguish the product from competitors."
    elif winner == "complete_specs":
        action = "Expose the complete verified specification set clearly so both retrieval and comparison have enough evidence."
    elif winner == "benefit_led":
        action = "Connect verified features to practical customer benefits without replacing the underlying specifications."
    elif winner.startswith("single_fact_"):
        action = f"Make the {report.get('gap_name', 'missing product detail').lower()} explicit in the normal listing, because that single addition produced the strongest tested result."
    else:
        action = report.get("action", "Apply the winning presentation change while keeping every claim grounded in submitted facts.")
    return [
        {"label": "Result", "text": result},
        {"label": "Why", "text": why},
        {"label": "What it means", "text": meaning},
        {"label": "What to do", "text": action},
    ]


def load_gap_report(run_id: int) -> dict[str, Any] | None:
    path = ROOT / "data" / "artifacts" / str(run_id) / "gap_report.json"
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    human_explanation = report.get("human_explanation", [])
    needs_human_explanation = not human_explanation or not isinstance(human_explanation[0], dict)
    if "original_listing" not in report or "variant_explanations" not in report or needs_human_explanation:
        with connect() as db:
            row = db.execute("SELECT manifest_json,put_product_id FROM runs WHERE id=?", (run_id,)).fetchone()
            metric_rows = db.execute("SELECT data_json FROM metrics WHERE run_id=?", (run_id,)).fetchall()
        if row:
            manifest = json.loads(row["manifest_json"])
            if "variant_explanations" not in report:
                metrics = [json.loads(metric["data_json"]) for metric in metric_rows]
                report["variant_explanations"] = build_variant_explanations(run_id, row["put_product_id"], metrics)
            product = manifest.get("product_snapshot", {})
            variants = {variant["condition"]: variant for variant in manifest.get("variants", [])}
            original = variants.get("original")
            winner = variants.get(report.get("winner"), original)
            if original and winner:
                fact_labels = {fact["fact_id"]: fact.get("label", fact["predicate"].replace("_", " ").title()) for fact in product.get("facts", [])}
                added = set(winner["exposed_fact_ids"]) - set(original["exposed_fact_ids"])
                notes = [f"Presentation: {report.get('gap_name', 'Listing representation')}. {report.get('action', '')}".strip()]
                if added:
                    notes.append("Facts made explicit: " + ", ".join(fact_labels.get(fact_id, fact_id) for fact_id in sorted(added)) + ".")
                notes.append(f"Listing length: {len(original['body'].split())} words → {len(winner['body'].split())} words.")
                report.update({"original_listing": original["body"], "winning_listing": winner["body"], "difference_notes": notes})
            report["human_explanation"] = build_human_explanation(report, manifest)
    return report


def retry_gap_summary(run_id: int) -> dict[str, Any]:
    run, products, queries = context(run_id)
    report = load_gap_report(run_id)
    if not report:
        raise ValueError("gap report not found")
    manifest = json.loads(run["manifest_json"])
    variants = {variant["condition"]: variant for variant in manifest["variants"]}
    product = products[run["put_product_id"]]
    request = {"canonical_product": product, "original_listing": product["original_presentation"], "deterministic_report": report, "winning_variant": variants.get(report["winner"]), "queries": queries}
    summary, model, tokens_used = summarize_gap_report(request)
    errors = validate_summary(summary, product, queries)
    if errors:
        raise ValueError(errors[0])
    report.update({"summary_status": "completed", "llm_summary": summary, "summary_model": model, "summary_tokens": tokens_used})
    report.pop("summary_error", None)
    path = ROOT / "data" / "artifacts" / str(run_id) / "gap_report.json"
    path.write_text(json.dumps(report, indent=2))
    with connect() as db:
        db.execute("UPDATE artifacts SET content_hash=? WHERE run_id=? AND name='gap_report.json'", (stable_hash(path.read_bytes()), run_id))
    return report


def stage_complete(run_id: int) -> None:
    with connect() as db:
        db.execute("UPDATE runs SET status='passed',progress=100,completed_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
