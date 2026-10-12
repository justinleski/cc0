"""The fetch gate: the only place image bytes enter the system.

Reaching this module means the rights gate already recorded explicit CC0
evidence. Every limit here is about the bytes, not the rights: host allowlist,
public-address check on each hop, size cap, timeout, redirect limit, and
content-type verification.

A refusal here does not change a rights decision. It fails the job.
"""

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.ingestion.errors import PermanentFailure, TransientFailure

# Content types we are willing to decode, and the extension used in the key.
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


@dataclass(frozen=True)
class FetchedImage:
    url: str
    content_type: str
    body: bytes


class ImageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedImage: ...


def validate_url(url: str | None, allowed_hosts: frozenset[str], *, what: str) -> str:
    """Scheme, credential, and host checks. Raises PermanentFailure.

    Pure and I/O free so the rules are testable without a socket, and shared by
    the recorded-fixture path so fixtures cannot smuggle in an unlisted host.
    """
    if not url or not url.strip():
        raise PermanentFailure(f"missing_{what}_url")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise PermanentFailure(f"{what}_scheme_not_allowed", parts.scheme or "none")
    if parts.username or parts.password:
        raise PermanentFailure(f"{what}_url_has_credentials")
    host = (parts.hostname or "").lower()
    if not host:
        raise PermanentFailure(f"{what}_url_has_no_host")
    if host not in allowed_hosts:
        raise PermanentFailure(f"{what}_host_not_allowed", host)
    return url.strip()


def assert_public_host(host: str) -> None:
    """Reject loopback, private, link-local, and reserved targets.

    Resolution happens here rather than inside the HTTP client so a DNS name
    that points at the metadata service or at Postgres is refused before any
    connection is opened.
    """
    try:
        resolved = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransientFailure("dns_failure", type(exc).__name__) from exc
    if not resolved:
        raise TransientFailure("dns_failure", "no_addresses")
    for info in resolved:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise PermanentFailure("private_address", host)


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        # An HTTP-date is valid here. Computed backoff is a safe substitute.
        return None


def classify_status(status_code: int, headers: Any) -> None:
    """Map a response status onto the retry decision. 2xx returns."""
    if 200 <= status_code < 300:
        return
    if status_code == 429:
        raise TransientFailure("rate_limited", str(status_code), _retry_after(headers))
    if status_code >= 500:
        raise TransientFailure("server_error", str(status_code))
    raise PermanentFailure("bad_status", str(status_code))


def verify_content_type(content_type: str | None) -> str:
    """The declared type must be one we decode. An HTML error page stops here."""
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_IMAGE_TYPES:
        raise PermanentFailure("not_an_image", declared or "none")
    return declared


class Pacer:
    """Time-spacing for one provider, shared by its metadata and image clients.

    Spacing is enough for a single worker. Worker replicas would need a shared
    token bucket, because each process would otherwise pace only itself.
    """

    def __init__(
        self,
        requests_per_second: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._last is not None:
            remaining = self._interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last = now


class HttpImageFetcher:
    """Streams bytes with a hard cap, following redirects one checked hop at a time."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        max_bytes: int,
        timeout_seconds: float,
        max_redirects: int,
        pacer: Pacer | None = None,
        client: httpx.Client | None = None,
        resolver: Callable[[str], None] = assert_public_host,
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._pacer = pacer
        # Injectable so unit tests exercise the caps and the redirect rules
        # without a DNS lookup, which also keeps CI off the network.
        self._resolver = resolver
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        # follow_redirects stays off: each hop is re-validated against the
        # allowlist and re-resolved, which httpx cannot do for us.
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"user-agent": "cc0-ingest/0.1 (+local)"},
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> FetchedImage:
        current = validate_url(url, self._allowed_hosts, what="image")
        for _ in range(self._max_redirects + 1):
            response, location = self._attempt(current)
            if location is None:
                return response
            current = validate_url(location, self._allowed_hosts, what="image")
        raise PermanentFailure("too_many_redirects", str(self._max_redirects))

    def _attempt(self, url: str) -> tuple[FetchedImage | None, str | None]:
        self._resolver(urlsplit(url).hostname or "")
        if self._pacer is not None:
            self._pacer.wait()
        try:
            with self._client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise PermanentFailure("redirect_without_location")
                    return None, location
                classify_status(response.status_code, response.headers)
                content_type = verify_content_type(response.headers.get("content-type"))
                body = self._read_capped(response)
        except httpx.TimeoutException as exc:
            raise TransientFailure("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise TransientFailure("connection_error", type(exc).__name__) from exc
        return FetchedImage(url=url, content_type=content_type, body=body), None

    def _read_capped(self, response: httpx.Response) -> bytes:
        # Declared length is only a hint; the stream is still counted, so a
        # lying or absent Content-Length cannot exhaust memory.
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            raise PermanentFailure("too_large", declared)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                raise PermanentFailure("too_large", str(self._max_bytes))
            chunks.append(chunk)
        if total == 0:
            raise PermanentFailure("empty_body")
        return b"".join(chunks)


class HttpJsonSource:
    """Metadata requests. A separate allowlist from the image hosts."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        timeout_seconds: float,
        pacer: Pacer | None = None,
        client: httpx.Client | None = None,
        resolver: Callable[[str], None] = assert_public_host,
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._pacer = pacer
        self._resolver = resolver
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"user-agent": "cc0-ingest/0.1 (+local)"},
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str) -> Any:
        target = validate_url(url, self._allowed_hosts, what="metadata")
        self._resolver(urlsplit(target).hostname or "")
        if self._pacer is not None:
            self._pacer.wait()
        try:
            response = self._client.get(target)
            classify_status(response.status_code, response.headers)
            return response.json()
        except httpx.TimeoutException as exc:
            raise TransientFailure("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise TransientFailure("connection_error", type(exc).__name__) from exc
        except ValueError as exc:
            raise PermanentFailure("metadata_not_json", type(exc).__name__) from exc
