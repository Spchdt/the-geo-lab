import json
import time

import pytest
from conftest import form

from geolab.core import treatment_claim_errors
from geolab.db import connect, fetch_all, fetch_one
from geolab.geo import build_product_submission, generate_queries, generate_variants, validate_summary
from geolab.pipeline import build_human_explanation, create_confirmation_run, create_geo_run, load_gap_report


def test_submission_queries_and_variants_are_deterministic_and_treatment_blind():
    product = build_product_submission(form())
    assert product == build_product_submission(form())
    screen = generate_queries(product, "screen")
    confirmation = generate_queries(product, "confirmation")
    assert len(screen) == 6
    assert len(confirmation) == 12
    assert {row["query_group_id"] for row in screen}.isdisjoint(row["query_group_id"] for row in confirmation)
    assert all("pocketvolt" not in row["text"].lower() for row in [*screen, *confirmation])
    assert all("variant" not in row["text"].lower() and "treatment" not in row["text"].lower() for row in [*screen, *confirmation])

    variants = generate_variants(product)
    conditions = [variant["condition"] for variant in variants]
    assert conditions[:5] == ["original", "normalized_same_facts", "complete_specs", "benefit_led", "intent_aligned"]
    assert len(conditions) == 10
    assert conditions[-2:] == ["identity_control", "misleading_control"]
    assert variants[0]["body"] == variants[-2]["body"]
    single = [variant for variant in variants if variant["condition"].startswith("single_fact_")]
    assert all(len(set(variant["exposed_fact_ids"]) - set(variants[0]["exposed_fact_ids"])) == 1 for variant in single)


def test_misleading_control_states_a_number_the_facts_do_not_support():
    product = build_product_submission(form())
    variants = {variant["condition"]: variant for variant in generate_variants(product)}
    misleading = {**variants["misleading_control"], "exposed_fact_ids": variants["misleading_control"]["exposed_fact_ids"]}
    assert treatment_claim_errors(product, misleading) == ["typed claim disagrees with canonical truth"]
    assert treatment_claim_errors(product, variants["complete_specs"]) == []


def test_product_form_validation_and_summary_evidence_validation():
    invalid = form()
    invalid["price"] = "many"
    with pytest.raises(ValueError, match="Price must be a valid number"):
        build_product_submission(invalid)
    product = build_product_submission(form())
    queries = generate_queries(product, "screen")
    summary = {"revised_title": "PocketVolt 99", "revised_body": "Unsupported", "gaps": [{"fact_ids": ["bad"], "query_ids": ["bad"]}]}
    errors = validate_summary(summary, product, queries)
    assert "summary cites an unknown fact" in errors
    assert "summary cites an unknown query" in errors
    assert "summary rewrite contains an unsupported number" in errors


def test_human_explanation_uses_query_text_not_internal_ids():
    report = {
        "winner": "intent_aligned",
        "gap_name": "Search-language alignment",
        "winner_metrics": {"retrieval_mrr_delta": 0.051, "retrieval_top3_delta": 0.333, "top3_delta": -0.167},
        "variant_explanations": {"intent_aligned": {
            "retrieval_improved_query_ids": ["q-1"], "retrieval_worsened_query_ids": [],
            "agent_improved_query_ids": [], "agent_worsened_query_ids": ["q-2"],
        }},
    }
    manifest = {"queries": [{"query_group_id": "q-1", "text": "wireless earbuds for commuting"}, {"query_group_id": "q-2", "text": "lightweight earbuds"}]}
    explanation = " ".join(item["text"] for item in build_human_explanation(report, manifest))
    assert "1 of 2 tested searches" in explanation
    assert "general shopping" in explanation
    assert "q-1" not in explanation and "q-2" not in explanation
    labels = [item["label"] for item in build_human_explanation(report, manifest)]
    assert labels == ["Result", "Why", "What it means", "What to do"]


def test_screen_reaches_every_variant_and_confirmation_promotes_two(tmp_path, monkeypatch):
    monkeypatch.setattr("geolab.db.DB_PATH", tmp_path / "geo.db")
    monkeypatch.setattr("geolab.pipeline.connect", connect)
    monkeypatch.setenv("GEOLAB_LLM_URL", "https://recorded.test/v1/chat/completions")
    monkeypatch.setenv("GEOLAB_LLM_MODEL", "recorded-test-model")
    monkeypatch.setenv("GEOLAB_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GEOLAB_LLM_MIN_INTERVAL", "0")
    calls = []

    def recorded(request):
        calls.append(request)
        picked = request["candidates"][:3]
        presented = {row["product_id"]: row for row in request["candidate_presentations"]}
        claims = [{"product_id": pid, "text": "Recorded.", "evidence_ids": presented[pid]["exposed_evidence_ids"][:1]} for pid in picked if presented[pid]["exposed_evidence_ids"]]
        return {"decision": "recommend", "ordered_product_ids": picked, "claims": claims, "uncertainty": "low"}, "recorded-test-model", 0

    def summarized(request):
        query_id = request["queries"][0]["query_group_id"]
        return {"headline": "Controlled result.", "gaps": [{"name": "Listing structure", "explanation": "A clearer structure was tested.", "fact_ids": [], "query_ids": [query_id]}], "suggestions": ["Use explicit labels."], "revised_title": "PocketVolt", "revised_body": "Verified listing details.", "caveat": "Controlled catalogue only."}, "recorded-test-model", 0

    monkeypatch.setattr("geolab.pipeline.recommend", recorded)
    monkeypatch.setattr("geolab.pipeline.summarize_gap_report", summarized)
    screen_id = create_geo_run(form())
    for _ in range(300):
        run = fetch_one("SELECT * FROM runs WHERE id=?", (screen_id,))
        if run and run["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert run["status"] == "passed"
    manifest = json.loads(run["manifest_json"])
    assert len(calls) == 6 * len(manifest["conditions"]) == 60
    assert {row["condition"] for row in fetch_all("SELECT condition FROM llm_attempts WHERE run_id=?", (screen_id,))} == set(manifest["conditions"])
    report = load_gap_report(screen_id)
    assert report["original_listing"] == manifest["product_snapshot"]["original_presentation"]
    assert report["winning_listing"]
    assert report["difference_notes"]
    assert report["variant_explanations"]["original"]["summary"].startswith("Baseline")
    assert set(report["variant_explanations"]) == set(manifest["conditions"])

    confirmation_id = create_confirmation_run(screen_id)
    for _ in range(300):
        confirmation = fetch_one("SELECT * FROM runs WHERE id=?", (confirmation_id,))
        if confirmation and confirmation["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert confirmation["status"] == "passed"
    confirmation_manifest = json.loads(confirmation["manifest_json"])
    assert confirmation_manifest["phase"] == "confirmation"
    assert len(confirmation_manifest["conditions"]) == 4
    assert len(fetch_all("SELECT * FROM llm_attempts WHERE run_id=?", (confirmation_id,))) == 48
