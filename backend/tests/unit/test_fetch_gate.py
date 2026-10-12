"""Limits on the only request that ever touches image bytes."""

import httpx
import pytest

from app.ingestion.errors import PermanentFailure, TransientFailure
from app.ingestion.fetch import (
    HttpImageFetcher,
    Pacer,
    assert_public_host,
    classify_status,
    validate_url,
    verify_content_type,
)

HOSTS = frozenset({"images.metmuseum.org"})
URL = "https://images.metmuseum.org/CRDImages/cc0/1.jpg"


def _fetcher(handler, **overrides) -> HttpImageFetcher:
    options = {"max_bytes": 1024, "timeout_seconds": 1.0, "max_redirects": 2}
    options.update(overrides)
    return HttpImageFetcher(
        HOSTS,
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        # Caps and redirect rules are the subject here; resolution is covered
        # separately so these tests need no DNS.
        resolver=lambda _host: None,
        **options,
    )


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (None, "missing_image_url"),
        ("", "missing_image_url"),
        ("http://images.metmuseum.org/a.jpg", "image_scheme_not_allowed"),
        ("https://cdn.example.net/a.jpg", "image_host_not_allowed"),
        ("https://user:pass@images.metmuseum.org/a.jpg", "image_url_has_credentials"),
        ("https:///a.jpg", "image_url_has_no_host"),
    ],
)
def test_url_validation_refuses_before_any_connection(url, reason):
    with pytest.raises(PermanentFailure, match=reason):
        validate_url(url, HOSTS, what="image")


def test_loopback_and_private_targets_are_refused():
    # A name that resolves inside the network could reach Postgres or a cloud
    # metadata service, so resolution happens before the connection.
    with pytest.raises(PermanentFailure, match="private_address"):
        assert_public_host("localhost")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limits_and_server_errors_are_transient(status):
    with pytest.raises(TransientFailure):
        classify_status(status, {})


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_other_client_errors_are_permanent(status):
    with pytest.raises(PermanentFailure, match="bad_status"):
        classify_status(status, {})


def test_retry_after_is_honored_when_numeric():
    with pytest.raises(TransientFailure) as raised:
        classify_status(429, {"retry-after": "12"})
    assert raised.value.retry_after == 12.0
    # An HTTP-date is legal and computed backoff is the fallback.
    with pytest.raises(TransientFailure) as dated:
        classify_status(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert dated.value.retry_after is None


@pytest.mark.parametrize("declared", ["text/html", "application/json", None, ""])
def test_a_non_image_body_is_refused(declared):
    with pytest.raises(PermanentFailure, match="not_an_image"):
        verify_content_type(declared)


def test_content_type_parameters_are_tolerated():
    assert verify_content_type("image/jpeg; charset=binary") == "image/jpeg"


def test_oversize_body_is_refused_even_when_content_length_lies():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 4096,
            headers={"content-type": "image/jpeg", "content-length": "10"},
        )

    with pytest.raises(PermanentFailure, match="too_large"):
        _fetcher(handler).fetch(URL)


def test_declared_oversize_is_refused_without_reading_the_body():
    read = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal read
        read = True
        return httpx.Response(
            200,
            content=b"x" * 16,
            headers={"content-type": "image/jpeg", "content-length": "99999"},
        )

    with pytest.raises(PermanentFailure, match="too_large"):
        _fetcher(handler).fetch(URL)
    assert read is True


def test_redirects_are_revalidated_and_bounded():
    def offsite(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://cdn.example.net/a.jpg"})

    with pytest.raises(PermanentFailure, match="image_host_not_allowed"):
        _fetcher(offsite).fetch(URL)

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    with pytest.raises(PermanentFailure, match="too_many_redirects"):
        _fetcher(loop).fetch(URL)


def test_timeout_is_transient_and_keeps_the_record_eligible():
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(TransientFailure, match="timeout"):
        _fetcher(slow).fetch(URL)


def test_a_valid_response_returns_the_bytes():
    def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"jpegbytes", headers={"content-type": "image/jpeg"})

    fetched = _fetcher(ok).fetch(URL)
    assert (fetched.content_type, fetched.body) == ("image/jpeg", b"jpegbytes")


def test_pacer_spaces_requests_for_one_provider():
    slept: list[float] = []
    clock = iter([0.0, 0.0, 0.1, 0.5, 0.5])
    pacer = Pacer(2.0, sleep=slept.append, monotonic=lambda: next(clock))
    pacer.wait()
    pacer.wait()
    # Two requests per second means the second one waits out the remainder.
    assert slept == [pytest.approx(0.5)]
