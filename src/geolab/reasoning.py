"""Structured-output calls to the configured OpenAI-compatible provider."""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .core import canonical_json

DEFAULT_TIMEOUT_SECONDS = "30"
ERROR_DETAIL_LIMIT = 500


class LLMRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _parse_retry_after(error: urllib.error.HTTPError, detail: str) -> float | None:
    header = error.headers.get("Retry-After") if error.headers else None
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(header)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    match = re.search(r'"retryDelay"\s*:\s*"([0-9.]+)s"', detail)
    return float(match.group(1)) if match else None


def _read_http_error(error: urllib.error.HTTPError) -> tuple[str, float | None]:
    raw = error.read().decode("utf-8", errors="replace")
    detail = raw
    try:
        payload = json.loads(raw)
        detail = payload.get("error", {}).get("message") or raw
    except json.JSONDecodeError:
        pass
    detail = " ".join(detail.split())[:ERROR_DETAIL_LIMIT]
    return detail, _parse_retry_after(error, raw)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip("'\"")
        if not os.environ.get(name):
            os.environ[name] = value


def llm_config() -> dict[str, str]:
    load_env_file(Path(__file__).resolve().parents[2] / ".env")
    config = {
        "url": os.getenv("GEOLAB_LLM_URL", ""),
        "model": os.getenv("GEOLAB_LLM_MODEL", ""),
        "api_key": os.getenv("GEOLAB_LLM_API_KEY", ""),
    }
    missing = [f"GEOLAB_LLM_{name.upper()}" for name, value in config.items() if not value]
    if missing:
        raise RuntimeError("LLM not configured: " + ", ".join(missing))
    return config


def llm_configured() -> bool:
    try:
        llm_config()
        return True
    except RuntimeError:
        return False


def recommend(request_data: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
    config = llm_config()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "ordered_product_ids", "claims", "uncertainty"],
        "properties": {
            "decision": {"enum": ["recommend", "clarify", "abstain"]},
            "ordered_product_ids": {"type": "array", "items": {"type": "string"}},
            "claims": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["product_id", "text", "evidence_ids"], "properties": {"product_id": {"type": "string"}, "text": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}}},
            "uncertainty": {"enum": ["low", "medium", "high"]},
        },
    }
    body = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": "Recommend only supplied candidates. Obey hard constraints. Cite only exposed evidence IDs. Return schema-valid JSON. Do not include chain-of-thought."},
            {"role": "user", "content": canonical_json(request_data)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "recommendation", "strict": True, "schema": schema}},
        "temperature": 0,
    }
    http_request = urllib.request.Request(config["url"], data=canonical_json(body).encode(), headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(http_request, timeout=float(os.getenv("GEOLAB_LLM_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))) as response:
            payload = json.loads(response.read())
        output = json.loads(payload["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as exc:
        detail, retry_after = _read_http_error(exc)
        raise LLMRequestError(f"LLM HTTP {exc.code}: {detail or exc.reason}", status_code=exc.code, retry_after=retry_after) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(f"LLM request failed: {exc}") from exc
    return output, config["model"], int(payload.get("usage", {}).get("total_tokens", 0))


def summarize_gap_report(request_data: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
    config = llm_config()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "gaps", "suggestions", "revised_title", "revised_body", "caveat"],
        "properties": {
            "headline": {"type": "string"},
            "gaps": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "explanation", "fact_ids", "query_ids"], "properties": {
                "name": {"type": "string"}, "explanation": {"type": "string"},
                "fact_ids": {"type": "array", "items": {"type": "string"}},
                "query_ids": {"type": "array", "items": {"type": "string"}},
            }}},
            "suggestions": {"type": "array", "items": {"type": "string"}},
            "revised_title": {"type": "string"},
            "revised_body": {"type": "string"},
            "caveat": {"type": "string"},
        },
    }
    body = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": "Summarize the supplied controlled GEO experiment. Use only canonical facts and supplied evidence IDs. Do not invent performance, compatibility, safety, or usage claims. The rewrite may only contain supported product numbers. Return schema-valid JSON without chain-of-thought."},
            {"role": "user", "content": canonical_json(request_data)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "geo_gap_report", "strict": True, "schema": schema}},
        "temperature": 0,
    }
    http_request = urllib.request.Request(config["url"], data=canonical_json(body).encode(), headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(http_request, timeout=float(os.getenv("GEOLAB_LLM_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))) as response:
            payload = json.loads(response.read())
        output = json.loads(payload["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as exc:
        detail, retry_after = _read_http_error(exc)
        raise LLMRequestError(f"LLM HTTP {exc.code}: {detail or exc.reason}", status_code=exc.code, retry_after=retry_after) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(f"LLM request failed: {exc}") from exc
    return output, config["model"], int(payload.get("usage", {}).get("total_tokens", 0))
