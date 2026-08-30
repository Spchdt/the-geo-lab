"""Turn a brand submission into queries, competitors, and listing variants."""

import json
import math
import re
from typing import Any, Mapping

from .core import fact_is_exposed, stable_hash

CONTROL_CONDITIONS = {"identity_control", "misleading_control"}
PREFERENCES = {"higher", "lower", "exact"}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:40] or "attribute"


def _parse_value(raw: str) -> Any:
    cleaned = raw.strip().replace(",", "")
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return float(cleaned)
    return raw.strip()


def build_product_submission(form: Mapping[str, str]) -> dict[str, Any]:
    name = form.get("product_name", "").strip()
    category = form.get("category", "").strip() or "power bank"
    title = form.get("listing_title", "").strip()
    description = form.get("listing_description", "").strip()
    if not name or not category or not title or not description:
        raise ValueError("Product name, category, listing title, and listing description are required")
    try:
        price = float(form.get("price", ""))
    except ValueError as exc:
        raise ValueError("Price must be a valid number") from exc
    if price <= 0:
        raise ValueError("Price must be greater than zero")

    raw_attributes = json.loads(form.get("attributes_json") or "[]")
    if not isinstance(raw_attributes, list):
        raise ValueError("Attributes must be a list")
    raw_facts: list[dict[str, Any]] = [{"predicate": "price", "label": "Price", "value": price, "unit": "SGD", "preference": "lower", "status": "verified"}]
    used = {"price"}
    for raw in raw_attributes:
        label = str(raw.get("name", "")).strip()
        value_text = str(raw.get("value", "")).strip()
        if not label and not value_text:
            continue
        if not label or not value_text:
            raise ValueError("Every attribute needs both a name and value")
        predicate = _slugify(label)
        if predicate in used:
            raise ValueError(f"Duplicate attribute: {label}")
        used.add(predicate)
        preference = str(raw.get("preference", "exact")).strip().lower()
        if preference not in PREFERENCES:
            raise ValueError(f"Invalid comparison preference for {label}")
        value = _parse_value(value_text)
        if isinstance(value, str):
            preference = "exact"
        raw_facts.append({"predicate": predicate, "label": label, "value": value, "unit": str(raw.get("unit", "")).strip(), "preference": preference, "status": "verified"})

    seed = {"category": category, "name": name, "listing_title": title, "listing_description": description, "facts": raw_facts}
    product_id = "brand-" + stable_hash(seed).split(":", 1)[1][:12]
    facts = [{**fact, "fact_id": f"{product_id}:{fact['predicate']}", "source_uri": "brand-supplied"} for fact in raw_facts]
    return {"product_id": product_id, "category": category, "name": name, "facts": facts, "original_presentation": f"{title}\n{description}", "listing_title": title, "listing_description": description}


def submission_form(product: dict[str, Any]) -> dict[str, str]:
    attributes = [{"name": _format_label(fact), "value": str(fact["value"]), "unit": fact.get("unit", ""), "preference": fact.get("preference", "exact")} for fact in product["facts"] if fact["predicate"] != "price"]
    price = next(fact for fact in product["facts"] if fact["predicate"] == "price")
    return {"product_name": product["name"], "category": product["category"], "listing_title": product["listing_title"], "listing_description": product["listing_description"], "price": str(price["value"]), "attributes_json": json.dumps(attributes)}


def _format_value(fact: dict[str, Any]) -> str:
    value = fact["value"]
    if isinstance(value, float):
        value = f"{value:.2f}" if fact["predicate"] == "price" else f"{value:g}"
    elif isinstance(value, int):
        value = f"{value:,}"
    return f"{value} {fact.get('unit', '')}".strip()


def _format_label(fact: dict[str, Any]) -> str:
    return fact.get("label") or fact["predicate"].replace("_", " ").title()


def _build_constraint(fact: dict[str, Any]) -> tuple[str, str, Any, str]:
    preference = fact.get("preference", "exact")
    operator = ">=" if preference == "higher" else "<=" if preference == "lower" else "="
    return fact["predicate"], operator, fact["value"], fact.get("unit", "")


def _build_fact_query(category: str, fact: dict[str, Any], wording: int = 0) -> tuple[str, str, list[tuple[str, str, Any, str]]]:
    label, shown = _format_label(fact).lower(), _format_value(fact)
    preference = fact.get("preference", "exact")
    relation = "at least" if preference == "higher" else "no more than" if preference == "lower" else "matching"
    texts = [f"Find a {category} with {label} {relation} {shown}.", f"Which {category} offers {relation} {shown} for {label}?" ]
    return fact["predicate"], texts[wording % 2], [_build_constraint(fact)]


def generate_queries(product: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    category = product["category"]
    price, attributes = product["facts"][0], product["facts"][1:]
    step = 1 if price["value"] <= 10 else 5 if price["value"] <= 50 else 25 if price["value"] <= 250 else 50
    ceiling = math.ceil(float(price["value"]) / step) * step
    price_fact = {**price, "value": int(ceiling) if float(ceiling).is_integer() else ceiling}
    screen: list[tuple[str, str, list[tuple[str, str, Any, str]]]] = [
        ("broad", f"Which {category} is a strong everyday choice?", []),
        ("price", f"Recommend a {category} under SGD {_format_value(price_fact).replace(' SGD', '')}.", [_build_constraint(price_fact)]),
    ]
    screen.extend(_build_fact_query(category, fact) for fact in attributes[:4])
    fillers = [
        ("details", f"Which {category} clearly explains its important product details?", []),
        ("comparison", f"Help me compare options when shopping for a {category}.", []),
        ("use_case", f"Recommend a well-described {category} for regular use.", []),
        ("choice", f"What {category} should a shopper consider?", []),
    ]
    screen.extend(fillers[: 6 - len(screen)])

    confirm: list[tuple[str, str, list[tuple[str, str, Any, str]]]] = [
        ("broad", f"Help me choose a dependable {category}.", []),
        ("broad", f"What is a good {category} for normal use?", []),
        ("price", f"Show me {category} options costing no more than SGD {_format_value(price_fact).replace(' SGD', '')}.", [_build_constraint(price_fact)]),
        ("price", f"I have a budget of SGD {_format_value(price_fact).replace(' SGD', '')} for a {category}.", [_build_constraint(price_fact)]),
    ]
    for fact in attributes[:4]:
        confirm.extend([_build_fact_query(category, fact, 0), _build_fact_query(category, fact, 1)])
    confirm_fillers = [
        ("details", f"Find a {category} with useful listing details.", []),
        ("comparison", f"Compare the available {category} products for me.", []),
        ("use_case", f"Select a clearly described {category} for everyday use.", []),
        ("choice", f"Which {category} looks like the strongest match?", []),
        ("information", f"Recommend a {category} with enough information to make a decision.", []),
        ("shopping", f"I am shopping for a {category}; what should I consider?", []),
        ("broad", f"Choose a suitable {category} from this marketplace.", []),
        ("broad", f"What {category} would you recommend from these options?", []),
    ]
    confirm.extend(confirm_fillers[: 12 - len(confirm)])
    source = screen if phase == "screen" else confirm[:12]
    prefix = stable_hash({"product": product["product_id"], "phase": phase}).split(":", 1)[1][:8]
    return [{"query_group_id": f"{prefix}-{phase[:2]}-{index:02d}", "text": text, "hard_constraints": [{"predicate": p, "operator": op, "value": value, "unit": unit} for p, op, value, unit in constraints], "suite": phase, "stratum": intent, "weight": 1.0} for index, (intent, text, constraints) in enumerate(source, 1)]


def generate_competitors(product: dict[str, Any], count: int = 30) -> list[dict[str, Any]]:
    seed = stable_hash({"category": product["category"], "facts": [{key: fact.get(key) for key in ("predicate", "value", "unit", "preference")} for fact in product["facts"]]}).split(":", 1)[1][:8]
    competitors = []
    for index in range(1, count + 1):
        product_id = f"cmp-{seed}-{index:02d}"
        facts = []
        for position, source in enumerate(product["facts"]):
            value = source["value"]
            if isinstance(value, (int, float)):
                factor = 0.72 + ((index * 7 + position * 3) % 17) * 0.04
                value = round(float(value) * factor, 2)
                if isinstance(source["value"], int):
                    value = max(1, int(round(value)))
            elif index % 3:
                value = source["value"]
            else:
                value = f"Alternative {1 + index % 4}"
            facts.append({**source, "fact_id": f"{product_id}:{source['predicate']}", "value": value, "source_uri": "deterministic-benchmark"})
        name = f"Marketplace {product['category'].title()} {index:02d}"
        shown = [fact for offset, fact in enumerate(facts) if (index + offset) % 3 != 0]
        body = "\n".join([name, *[f"{_format_label(fact)}: {_format_value(fact)}" for fact in shown]])
        competitors.append({"product_id": product_id, "category": product["category"], "name": name, "facts": facts, "original_presentation": body})
    return competitors


def _format_spec_lines(facts: list[dict[str, Any]], prefix: str = "") -> list[str]:
    return [f"{prefix}{_format_label(fact)}: {_format_value(fact)}" for fact in facts]


def generate_variants(product: dict[str, Any]) -> list[dict[str, Any]]:
    original = product["original_presentation"]
    exposed = [fact for fact in product["facts"] if fact_is_exposed(fact, original)]
    facts = [fact for fact in product["facts"] if fact["status"] == "verified"]
    intent_order = sorted(facts, key=lambda fact: (fact["predicate"] == "price", fact.get("preference") == "exact", fact["predicate"]))
    variants: list[tuple[str, str, list[dict[str, Any]], str | None]] = [
        ("original", original, exposed, None),
        ("normalized_same_facts", "\n".join([product["name"], *_format_spec_lines(exposed)]), exposed, "clarity_structure"),
        ("complete_specs", "\n".join([product["name"], "Complete product details", *_format_spec_lines(facts)]), facts, "detail_completeness"),
        ("benefit_led", "\n".join([product["name"], "What shoppers can compare", *_format_spec_lines(facts, "Product detail — ")]), facts, "benefit_communication"),
        ("intent_aligned", "\n".join([f"{product['name']} | {product['category']}", "Searchable shopping details", *_format_spec_lines(intent_order)]), facts, "search_language_alignment"),
    ]
    for fact in [fact for fact in facts if fact not in exposed][:3]:
        condition = f"single_fact_{fact['predicate']}"
        variants.append((condition, original + "\n" + _format_spec_lines([fact])[0], [*exposed, fact], f"missing_{fact['predicate']}"))
    variants.extend([("identity_control", original, exposed, None), ("misleading_control", _build_misleading_body(product, facts), facts, None)])
    return [{"condition": condition, "body": body, "exposed_fact_ids": [fact["fact_id"] for fact in exposed_facts], "gap_code": gap_code, "content_hash": stable_hash(body)} for condition, body, exposed_facts, gap_code in variants]


def _build_misleading_body(product: dict[str, Any], facts: list[dict[str, Any]]) -> str:
    body = "\n".join([product["name"], *_format_spec_lines(facts)])
    target = next(fact for fact in facts if isinstance(fact["value"], (int, float)))
    increment = max(1, abs(float(target["value"])) * 0.25)
    false_value: int | float = float(target["value"]) + increment
    if isinstance(target["value"], int):
        false_value = int(round(false_value))
    return body.replace(_format_value(target), _format_value({**target, "value": false_value}), 1)


def is_control(condition: str) -> bool:
    return condition in CONTROL_CONDITIONS


def describe_gap(condition: str) -> tuple[str, str]:
    fixed = {
        "normalized_same_facts": ("Clarity and structure", "Reformat details already present so retrieval and recommendation systems can parse them consistently."),
        "complete_specs": ("Detail completeness", "Expose all supplied product facts in the listing."),
        "benefit_led": ("Benefit communication", "Present verified specifications using customer-oriented labels."),
        "intent_aligned": ("Search-language alignment", "Use the vocabulary customers use in relevant category searches."),
    }
    if condition.startswith("single_fact_"):
        label = condition.removeprefix("single_fact_").replace("_", " ")
        return f"Missing {label}", f"Add the verified {label} detail explicitly."
    return fixed.get(condition, ("No clear listing gap", "Keep the original listing until stronger evidence appears."))


def validate_summary(summary: dict[str, Any], product: dict[str, Any], queries: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    fact_ids = {fact["fact_id"] for fact in product["facts"]}
    query_ids = {query["query_group_id"] for query in queries}
    for gap in summary.get("gaps", []):
        if not set(gap.get("fact_ids", [])) <= fact_ids:
            errors.append("summary cites an unknown fact")
        if not set(gap.get("query_ids", [])) <= query_ids:
            errors.append("summary cites an unknown query")
    allowed_numbers = set(re.findall(r"\d+(?:[.,]\d+)*", product["name"] + " " + product["original_presentation"]))
    for fact in product["facts"]:
        allowed_numbers.update(re.findall(r"\d+(?:[.,]\d+)*", _format_value(fact)))
    used_numbers = set(re.findall(r"\d+(?:[.,]\d+)*", summary.get("revised_title", "") + " " + summary.get("revised_body", "")))
    if not used_numbers <= allowed_numbers:
        errors.append("summary rewrite contains an unsupported number")
    return errors
