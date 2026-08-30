from geolab.db import ROOT
from geolab.web import decorate_metrics


def test_metric_classes_flag_controls_and_uncertainty():
    metrics = [
        {"condition": "original", "inclusion_delta": 0, "delta": 0, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "intent_aligned", "inclusion_delta": 0.25, "delta": -0.1, "ci_low": -0.3, "ci_high": 0.1, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "identity_control", "inclusion_delta": 0, "delta": 0.05, "attempts": 12, "valid_attempts": 12, "unsupported_template_claims": 0},
        {"condition": "misleading_control", "inclusion_delta": 0.2, "delta": 0.2, "attempts": 12, "valid_attempts": 0, "unsupported_template_claims": 12},
    ]
    rows = {row["condition"]: row for row in decorate_metrics(metrics)}
    assert rows["intent_aligned"]["inclusion_class"] == "metric-good"
    assert rows["intent_aligned"]["recommendation_class"] == "metric-bad"
    assert rows["intent_aligned"]["interval_class"] == "metric-warning"
    assert rows["misleading_control"]["unsupported_class"] == "metric-good"
    assert rows["identity_control"]["recommendation_class"] == "metric-bad"


def test_input_item_javascript_is_cache_busted_and_contains_handler():
    base = (ROOT / "src" / "geolab" / "templates" / "base.html").read_text()
    runs = (ROOT / "src" / "geolab" / "templates" / "runs.html").read_text()
    script = (ROOT / "src" / "geolab" / "static" / "app.js").read_text()
    assert "/static/app.js?v=" in base
    assert "#toggle-product-form" in script
    for sample in ("strong", "weak", "shoes", "chair", "serum", "backpack"):
        assert f'data-sample="{sample}"' in runs
        assert f"  {sample}: {{" in script


def test_gap_report_keeps_summary_compact_and_details_collapsed():
    template = (ROOT / "src" / "geolab" / "templates" / "run_detail.html").read_text()
    assert "Bottom line" in template
    assert "Recommended action" in template
    assert '<details class="report-details">' in template
    assert "Optional model rewrite <small>not tested</small>" in template
    assert "gap_report.llm_summary.headline" not in template
    assert '<details class="panel"><summary>Validation and safety findings</summary>' in template
    assert '<details class="panel"><summary>Artifacts</summary>' in template
    base = (ROOT / "src" / "geolab" / "templates" / "base.html").read_text()
    assert "Closed-catalogue research runner" not in base
