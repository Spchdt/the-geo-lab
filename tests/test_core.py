
import pytest

from intenttwin.core import (
    assert_competitors_unchanged,
    check_constraint,
    compute_paired_metrics,
    fuse_ranks,
    render_treatment,
    stable_hash,
    treatment_claim_errors,
    validate_recommendation,
)


def product(pid="pb-017", capacity=20000):
    return {"product_id": pid, "name": "Test", "facts": [
        {"fact_id": f"{pid}:capacity", "predicate": "capacity", "value": capacity, "unit": "mAh", "status": "verified"},
        {"fact_id": f"{pid}:price", "predicate": "price", "value": 89.0, "unit": "SGD", "status": "verified"},
        {"fact_id": f"{pid}:weight", "predicate": "weight", "value": 300, "unit": "g", "status": "verified"},
        {"fact_id": f"{pid}:ports", "predicate": "ports", "value": 2, "unit": "count", "status": "verified"},
    ]}


def test_canonical_hash_is_stable():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_identity_treatment_is_byte_equal():
    original = "Exact\r\nbytes"
    assert render_treatment(product(), original, "identity_copy", True)["body"] == original


def test_normalized_does_not_infer_one_port_from_ten_thousand_capacity():
    p = product(pid="pb-009", capacity=10000)
    p["facts"][-1]["value"] = 1
    p["original_presentation"] = "VoltPack 9 10,000 mAh Power Bank\nSGD 89.00."
    normalized = render_treatment(p, p["original_presentation"], "normalized", True)
    assert normalized["exposed_fact_ids"] == ["pb-009:capacity", "pb-009:price"]
    assert "Ports:" not in normalized["body"]

    explicit = render_treatment(p, p["original_presentation"] + "\n1 USB port.", "normalized", True)
    assert "pb-009:ports" in explicit["exposed_fact_ids"]
    assert "Ports: 1 count" in explicit["body"]


def test_competitor_mutation_fails_run():
    rows = [{"product_id": "other", "content_hash": "a"}, {"product_id": "other", "content_hash": "b"}]
    with pytest.raises(ValueError):
        assert_competitors_unchanged(rows, "put")


def test_misleading_control_detected():
    p = product()
    item = render_treatment(p, "Test 20,000 mAh SGD 89.00", "misleading_control", True)
    assert treatment_claim_errors(p, item)


def test_constraint_pass_fail_unknown():
    p = product()
    assert check_constraint(p, [{"predicate": "capacity", "operator": ">=", "value": 20000}])[0] == "PASS"
    assert check_constraint(p, [{"predicate": "price", "operator": "<=", "value": 50}])[0] == "FAIL"
    assert check_constraint(p, [{"predicate": "color", "operator": "=", "value": "red"}])[0] == "UNKNOWN"


def test_rrf_matches_hand_calculation():
    result = dict(fuse_ranks({"a": [("p1", 5), ("p2", 1)], "b": [("p2", 7), ("p1", 2)]}))
    assert result["p1"] == result["p2"] == 1 / 61 + 1 / 62


def test_unknown_product_and_evidence_owner_rejected():
    products = {"pb-017": product()}
    output = {"decision": "recommend", "ordered_product_ids": ["bad"], "claims": [{"product_id": "pb-017", "evidence_ids": ["bad:price"]}]}
    errors = validate_recommendation(output, {"pb-017"}, {"pb-017": {"pb-017:price"}}, products, {"pb-017": "PASS"})
    assert "unknown product: bad" in errors
    assert "invalid evidence owner: bad:price" in errors


def test_bootstrap_is_seed_reproducible_and_trials_grouped():
    rows = []
    for trial in range(3):
        rows += [{"query_group_id": "q1", "condition": "original", "ordered_product_ids": []}, {"query_group_id": "q1", "condition": "normalized", "ordered_product_ids": ["put"]}]
    first = compute_paired_metrics(rows, "put", samples=50)
    assert first == compute_paired_metrics(rows, "put", samples=50)
    normalized = next(row for row in first if row["condition"] == "normalized")
    assert normalized["groups"] == 1
    assert normalized["ci_low"] is None
    assert normalized["interval_status"] == "not_estimable"
