"""Deterministic retrieval, treatment rendering, and paired-metric primitives."""

import hashlib
import json
import math
import random
import re
from collections import defaultdict
from typing import Any, Iterable

import numpy as np

SEMANTIC_DIMENSIONS = 128
RRF_K = 60
BOOTSTRAP_SAMPLES = 1000
ANALYSIS_SEED = 20260829
TOKEN_RE = re.compile(r"[a-z0-9]+")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any) -> str:
    raw = value if isinstance(value, (bytes, str)) else canonical_json(value)
    if isinstance(raw, str):
        raw = raw.encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def index_facts_by_predicate(product: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {fact["predicate"]: fact for fact in product["facts"]}


def fact_is_exposed(fact: dict[str, Any], text: str) -> bool:
    value = fact["value"]
    if isinstance(value, str):
        return value.lower() in text.lower()
    candidates = {str(value)}
    if isinstance(value, (int, float)):
        candidates.add(f"{value:,}")
        candidates.add(f"{value:.2f}")
    number = "(?:" + "|".join(re.escape(candidate) for candidate in sorted(candidates, key=len, reverse=True)) + ")"
    bounded = rf"(?<!\d){number}(?!\d)"
    patterns = {
        "capacity": rf"{bounded}\s*(?:mah|milliamp(?:ere)?[- ]?hours?)\b",
        "price": rf"(?:\bsgd\s*{bounded}|{bounded}\s*\bsgd)",
        "weight": rf"{bounded}\s*(?:g|grams?)\b",
        "ports": rf"{bounded}\s*(?:usb(?:-[a-z]+)?\s*)?ports?\b",
        "charging_power": rf"{bounded}\s*(?:w|watts?)\b",
        "warranty_months": rf"{bounded}\s*months?\b",
    }
    pattern = patterns.get(fact["predicate"])
    if pattern:
        return re.search(pattern, text, re.IGNORECASE) is not None
    unit = re.escape(str(fact.get("unit", "")))
    return bool(unit and re.search(rf"{bounded}\s*{unit}\b", text, re.IGNORECASE))


def assert_competitors_unchanged(presentations: list[dict[str, Any]], put_id: str) -> None:
    hashes: dict[str, set[str]] = defaultdict(set)
    for item in presentations:
        if item["product_id"] != put_id:
            hashes[item["product_id"]].add(item["content_hash"])
    changed = [product_id for product_id, values in hashes.items() if len(values) != 1]
    if changed:
        raise ValueError(f"competitor presentation changed: {changed[0]}")


def treatment_claim_errors(product: dict[str, Any], presentation: dict[str, Any]) -> list[str]:
    fact_ids = {fact["fact_id"] for fact in product["facts"]}
    errors = [fact_id for fact_id in presentation["exposed_fact_ids"] if fact_id not in fact_ids]
    if presentation["condition"] == "misleading_control":
        exposed = [fact for fact in product["facts"] if fact["fact_id"] in presentation["exposed_fact_ids"]]
        if any(not fact_is_exposed(fact, presentation["body"]) for fact in exposed):
            errors.append("typed claim disagrees with canonical truth")
    return errors


def check_constraint(product: dict[str, Any], constraints: list[dict[str, Any]]) -> tuple[str, list[str]]:
    facts = index_facts_by_predicate(product)
    unknown, reasons = False, []
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "=": lambda a, b: a == b}
    for rule in constraints:
        fact = facts.get(rule["predicate"])
        if not fact or fact["status"] != "verified":
            unknown = True
            reasons.append(f"{rule['predicate']} unknown")
        elif not ops[rule["operator"]](fact["value"], rule["value"]):
            return "FAIL", [f"{rule['predicate']} {fact['value']} violates {rule['operator']} {rule['value']}"]
    return ("UNKNOWN" if unknown else "PASS"), reasons


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def embed_text(text: str, dims: int = SEMANTIC_DIMENSIONS) -> np.ndarray:
    vector = np.zeros(dims)
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode()).digest()
        vector[int.from_bytes(digest[:2], "big") % dims] += 1 if digest[2] % 2 else -1
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def rank_channels(query: dict[str, Any], presentations: list[dict[str, Any]]) -> dict[str, list[tuple[str, float]]]:
    query_tokens = set(tokenize(query["text"]))
    query_vec = embed_text(query["text"])
    channels: dict[str, list[tuple[str, float]]] = {"lexical": [], "semantic": [], "attribute": []}
    for item in presentations:
        body_tokens = tokenize(item["body"])
        lexical = sum(1 for token in body_tokens if token in query_tokens) / math.sqrt(max(1, len(body_tokens)))
        semantic = float(np.dot(query_vec, embed_text(item["body"])))
        exposed = {fact_id.rsplit(":", 1)[-1] for fact_id in item["exposed_fact_ids"]}
        attribute = sum(1.0 for rule in query["hard_constraints"] if rule["predicate"] in exposed)
        for channel, score in (("lexical", lexical), ("semantic", semantic), ("attribute", attribute)):
            channels[channel].append((item["product_id"], score))
    for channel in channels:
        channels[channel].sort(key=lambda row: (-row[1], row[0]))
    return channels


def fuse_ranks(channels: dict[str, list[tuple[str, float]]], k: int = RRF_K) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for rows in channels.values():
        for rank, (product_id, _score) in enumerate(rows, 1):
            scores[product_id] += 1 / (k + rank)
    return sorted(scores.items(), key=lambda row: (-row[1], row[0]))


def validate_recommendation(output: dict[str, Any], candidates: set[str], exposed: dict[str, set[str]], products: dict[str, dict[str, Any]], outcomes: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if output.get("decision") not in {"recommend", "clarify", "abstain"}:
        errors.append("invalid decision")
    for product_id in output.get("ordered_product_ids", []):
        if product_id not in products:
            errors.append(f"unknown product: {product_id}")
        elif product_id not in candidates:
            errors.append(f"product not in candidate set: {product_id}")
        elif outcomes.get(product_id) == "FAIL":
            errors.append(f"failed constraint: {product_id}")
    for claim in output.get("claims", []):
        product_id = claim.get("product_id")
        if not claim.get("evidence_ids"):
            errors.append("claim missing evidence")
        valid_ids = {fact["fact_id"] for fact in products.get(product_id, {}).get("facts", [])}
        for evidence_id in claim.get("evidence_ids", []):
            if evidence_id not in valid_ids:
                errors.append(f"invalid evidence owner: {evidence_id}")
            elif evidence_id not in exposed.get(product_id, set()):
                errors.append(f"evidence not exposed: {evidence_id}")
    return errors


def compute_paired_metrics(rows: Iterable[dict[str, Any]], put_id: str, seed: int = ANALYSIS_SEED, samples: int = BOOTSTRAP_SAMPLES, conditions: Iterable[str] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[dict[str, float]]]] = defaultdict(lambda: defaultdict(list))
    seen_conditions: list[str] = []
    for row in rows:
        condition = row["condition"]
        if condition not in seen_conditions:
            seen_conditions.append(condition)
        ordered = row.get("ordered_product_ids", [])
        rank = ordered.index(put_id) + 1 if put_id in ordered else None
        grouped[row["query_group_id"]][condition].append({
            "any": float(rank is not None),
            "top1": float(rank == 1),
            "top3": float(rank is not None and rank <= 3),
            "mrr": (1 / rank) if rank else 0.0,
        })

    def average(values: list[dict[str, float]], key: str) -> float:
        return float(np.mean([value[key] for value in values]))

    original = {qid: {key: average(values["original"], key) for key in ("any", "top1", "top3", "mrr")} for qid, values in grouped.items() if values.get("original")}
    rng = random.Random(seed)
    result = []
    ordered_conditions = list(conditions) if conditions is not None else seen_conditions
    for condition in ordered_conditions:
        pairs = [(original[qid], {key: average(values[condition], key) for key in ("any", "top1", "top3", "mrr")}) for qid, values in grouped.items() if qid in original and values.get(condition)]
        deltas = [b["any"] - a["any"] for a, b in pairs]
        top3_deltas = [b["top3"] - a["top3"] for a, b in pairs]
        boot: list[float] = []
        if len(top3_deltas) >= 2:
            for _ in range(samples):
                boot.append(sum(rng.choice(top3_deltas) for _ in top3_deltas) / len(top3_deltas))
        result.append({
            "condition": condition,
            "groups": len(pairs),
            "recommended": round(sum(pair[1]["any"] for pair in pairs), 3),
            "rate": round(float(np.mean([pair[1]["any"] for pair in pairs])) if pairs else 0, 4),
            "delta": round(float(np.mean(deltas)) if deltas else 0, 4),
            "top1_rate": round(float(np.mean([pair[1]["top1"] for pair in pairs])) if pairs else 0, 4),
            "top1_delta": round(float(np.mean([pair[1]["top1"] - pair[0]["top1"] for pair in pairs])) if pairs else 0, 4),
            "top3_rate": round(float(np.mean([pair[1]["top3"] for pair in pairs])) if pairs else 0, 4),
            "top3_delta": round(float(np.mean(top3_deltas)) if top3_deltas else 0, 4),
            "recommendation_mrr": round(float(np.mean([pair[1]["mrr"] for pair in pairs])) if pairs else 0, 4),
            "recommendation_mrr_delta": round(float(np.mean([pair[1]["mrr"] - pair[0]["mrr"] for pair in pairs])) if pairs else 0, 4),
            "ci_low": round(float(np.quantile(boot, 0.025)), 4) if boot else None,
            "ci_high": round(float(np.quantile(boot, 0.975)), 4) if boot else None,
            "interval_status": "estimated" if boot else "not_estimable",
        })
    return result
