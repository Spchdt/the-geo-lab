from intenttwin.web import build_metric_conclusion, decorate_metrics
from intenttwin.db import ROOT


def test_metric_conclusion_explains_stage_difference_and_controls():
    metrics = [
        {"condition": "original", "inclusion_delta": 0, "delta": 0, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "normalized", "inclusion_delta": 0.1, "delta": 0, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "grounded_enriched", "inclusion_delta": 0.25, "delta": -0.1, "ci_low": -0.3, "ci_high": 0.1, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "identity_copy", "inclusion_delta": 0, "delta": 0.05, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "misleading_control", "inclusion_delta": 0.2, "delta": 0.2, "attempts": 12, "valid_attempts": 0, "unsupported_template_claims": 12},
    ]
    conclusion = build_metric_conclusion(metrics)
    assert "not automatically a contradiction" in conclusion["relationship"]
    assert "Identity control passed for retrieval" in conclusion["controls"]
    assert "detected in every attempt" in conclusion["controls"]
    assert "uncertain" in conclusion["headline"]

    rows = {row["condition"]: row for row in decorate_metrics(metrics)}
    assert rows["grounded_enriched"]["inclusion_class"] == "metric-good"
    assert rows["grounded_enriched"]["recommendation_class"] == "metric-bad"
    assert rows["grounded_enriched"]["interval_class"] == "metric-warning"
    assert rows["misleading_control"]["unsupported_class"] == "metric-good"
    assert rows["identity_copy"]["recommendation_class"] == "metric-bad"


def test_input_item_javascript_is_cache_busted_and_contains_handler():
    base = (ROOT / "src" / "intenttwin" / "templates" / "base.html").read_text()
    runs = (ROOT / "src" / "intenttwin" / "templates" / "runs.html").read_text()
    script = (ROOT / "src" / "intenttwin" / "static" / "app.js").read_text()
    assert "/static/app.js?v=" in base
    assert "#toggle-product-form" in script
    for sample in ("strong", "weak", "shoes", "chair", "serum", "backpack"):
        assert f'data-sample="{sample}"' in runs
        assert f"  {sample}: {{" in script


def test_gap_report_keeps_summary_compact_and_details_collapsed():
    template = (ROOT / "src" / "intenttwin" / "templates" / "run_detail.html").read_text()
    assert "Bottom line" in template
    assert "Recommended action" in template
    assert '<details class="report-details">' in template
    assert "Optional Gemini rewrite <small>not tested</small>" in template
    assert "gap_report.llm_summary.headline" not in template
    assert '<details class="panel"><summary>Validation and safety findings</summary>' in template
    assert '<details class="panel"><summary>Artifacts</summary>' in template
    assert 'class="notice"' not in template
    base = (ROOT / "src" / "intenttwin" / "templates" / "base.html").read_text()
    assert "Closed-catalogue research runner" not in base
