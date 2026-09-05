"""
Coverage for the Cloudflare-in-front detection that backs the chunk-size warning in Settings.

The important distinction under test is three-valued: True / False / None. "Could not determine"
(offline, DNS failure, TLS rejection) must never be reported to the user as "you are not behind
Cloudflare", because acting on that would mean silently withdrawing a warning that is still true.
"""
import requests
from requests.structures import CaseInsensitiveDict

from fireshare_agent.uploaders import cloudflare


class _FakeResponse:
    def __init__(self, headers: dict, status_code: int = 200) -> None:
        self.headers = CaseInsensitiveDict(headers)
        self.status_code = status_code

    def close(self) -> None:
        pass


def test_cf_ray_header_identifies_cloudflare():
    assert cloudflare.has_cloudflare_markers(CaseInsensitiveDict({"CF-RAY": "8a1b2c3d4e5f-LHR"})) is True


def test_server_header_identifies_cloudflare():
    assert cloudflare.has_cloudflare_markers(CaseInsensitiveDict({"Server": "cloudflare"})) is True


def test_header_matching_is_case_insensitive():
    # requests normalises header case, but the value casing is the server's choice.
    assert cloudflare.has_cloudflare_markers(CaseInsensitiveDict({"server": "CloudFlare"})) is True


def test_a_direct_nginx_server_is_not_cloudflare():
    headers = CaseInsensitiveDict({"Server": "nginx/1.25.3", "Content-Type": "text/html"})

    assert cloudflare.has_cloudflare_markers(headers) is False


def test_detection_works_on_an_unauthenticated_error_response(monkeypatch):
    # The whole point of probing by header: Cloudflare stamps cf-ray on the 401 an anonymous
    # request gets back, so no login is needed to find out.
    monkeypatch.setattr(
        requests, "head", lambda *a, **k: _FakeResponse({"cf-ray": "abc-LHR"}, status_code=401)
    )

    assert cloudflare.is_behind_cloudflare("https://fireshare.example.com") is True


def test_falls_back_to_get_when_the_server_refuses_head(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda *a, **k: _FakeResponse({}, status_code=405))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse({"cf-ray": "abc-LHR"}))

    assert cloudflare.is_behind_cloudflare("https://fireshare.example.com") is True


def test_unreachable_server_is_undetermined_not_false(monkeypatch):
    def _boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "head", _boom)

    # None, not False: the UI must keep any existing warning rather than concluding the server is
    # not behind Cloudflare just because it could not be reached.
    assert cloudflare.is_behind_cloudflare("https://fireshare.example.com") is None


def test_empty_url_is_undetermined_without_a_network_call(monkeypatch):
    def _fail(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made for an empty URL")

    monkeypatch.setattr(requests, "head", _fail)

    assert cloudflare.is_behind_cloudflare("   ") is None


def test_safe_chunk_sits_below_the_documented_cloudflare_cap():
    # The multipart envelope (boundaries, field names, filename) rides on top of the chunk itself,
    # so the advised ceiling has to leave headroom rather than sit exactly on the limit.
    assert cloudflare.SAFE_CHUNK_MB < cloudflare.MAX_UPLOAD_MB


def test_chunk_size_threshold_helper():
    mb = 1024 * 1024
    assert cloudflare.chunk_size_exceeds_safe_limit(cloudflare.SAFE_CHUNK_MB * mb) is False
    assert cloudflare.chunk_size_exceeds_safe_limit((cloudflare.SAFE_CHUNK_MB + 1) * mb) is True
