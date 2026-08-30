from io import BytesIO
from urllib.error import HTTPError

from intenttwin.reasoning import _http_error_detail


def test_http_429_preserves_provider_detail_and_retry_after():
    body = b'{"error":{"message":"Quota exceeded for requests per minute"}}'
    error = HTTPError("https://example.test", 429, "Too Many Requests", {"Retry-After": "7"}, BytesIO(body))
    detail, retry_after = _http_error_detail(error)
    assert detail == "Quota exceeded for requests per minute"
    assert retry_after == 7
