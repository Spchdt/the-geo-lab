"""Regenerate committed deterministic research fixtures."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
catalogue = []
for index in range(1, 31):
    product_id = f"pb-{index:03d}"
    capacity = [10000, 15000, 20000, 25000][(index - 1) % 4]
    price = float(39 + index * 3)
    weight = 180 + index * 9
    ports = 1 + index % 3
    name = f"VoltPack {index} {capacity:,} mAh Power Bank"
    facts = []
    for predicate, value, unit in (("price", price, "SGD"), ("capacity", capacity, "mAh"), ("weight", weight, "g"), ("ports", ports, "count")):
        facts.append({"fact_id": f"{product_id}:{predicate}", "predicate": predicate, "value": value, "unit": unit, "status": "verified", "source_uri": f"https://example.test/manufacturer/{product_id}"})
    catalogue.append({"product_id": product_id, "category": "power_bank", "name": name, "facts": facts, "original_presentation": f"{name}\nSGD {price:.2f}."})

intent_templates = [
    ("a dependable everyday power bank", []),
    ("a power bank under SGD 100", [("price", "<=", 100, "SGD")]),
    ("at least 10,000 mAh of capacity", [("capacity", ">=", 10000, "mAh")]),
    ("a portable model below 350 g", [("weight", "<=", 350, "g")]),
    ("at least one charging port", [("ports", ">=", 1, "count")]),
    ("10,000 mAh or more under SGD 100", [("capacity", ">=", 10000, "mAh"), ("price", "<=", 100, "SGD")]),
    ("a model below 350 g and SGD 100", [("weight", "<=", 350, "g"), ("price", "<=", 100, "SGD")]),
    ("10,000 mAh or more with at least one port", [("capacity", ">=", 10000, "mAh"), ("ports", ">=", 1, "count")]),
]
phrases = {
    "diagnostic": ["Check whether you would recommend", "Compare the catalogue for", "Which option best matches"],
    "demand": ["I am shopping for", "Please recommend", "What should I buy if I need", "Help me choose", "Show me a good option for"],
    "sealed": ["A customer needs", "Find the strongest match for", "From this catalogue choose", "I want advice on", "Select a suitable product for"],
}
suite_sizes = {"diagnostic": 24, "demand": 40, "sealed": 40}


def build_suite(suite, size):
    rows = []
    for index in range(size):
        intent, constraints = intent_templates[index % len(intent_templates)]
        phrase = phrases[suite][index // len(intent_templates)]
        rows.append({
            "query_group_id": f"{suite[:2]}-{index + 1:04d}",
            "text": f"{phrase} {intent}.",
            "hard_constraints": [{"predicate": p, "operator": op, "value": value, "unit": unit} for p, op, value, unit in constraints],
            "suite": suite,
            "stratum": "preference_led" if not constraints else "hard_constraint",
            "weight": 1.0,
        })
    return rows

(root / "data" / "suites").mkdir(parents=True, exist_ok=True)
(root / "data" / "catalogue.jsonl").write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in catalogue) + "\n")
for suite, size in suite_sizes.items():
    rows = build_suite(suite, size)
    (root / "data" / "suites" / f"{suite}.jsonl").write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
