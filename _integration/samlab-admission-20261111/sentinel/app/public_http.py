"""Allowlisted, DNS-pinned HTTPS reads with robots and bounded resources."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser


USER_AGENT = "CredentialSentinel/1.0"


class PublicConnectorError(ValueError):
    """Only stable error codes, never content or target-supplied messages."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


def public_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise PublicConnectorError("public_url_invalid") from None
    if (
        not isinstance(url, str) or len(url) > 4096
        or any(ord(char) <= 32 for char in url)
        or parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.fragment
        or port not in {None, 443}
    ):
        raise PublicConnectorError("public_url_must_be_https_without_userinfo")
    host = parsed.hostname.lower().rstrip(".")
    if "%" in host or "\\" in host or host in {"localhost", "localhost.localdomain"}:
        raise PublicConnectorError("private_or_local_host_blocked")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PublicConnectorError("private_or_local_host_blocked")
    return f"https://[{host}]" if ":" in host else f"https://{host}"


def _validate_public_url(url: str) -> str:
    public_origin(url)
    return url


class UrllibPublicTransport:
    """Name retained for the adapter contract; no urllib redirect/proxy state.

    Resolution is checked and the socket connects to that validated IP, while
    TLS certificate verification and SNI use the original hostname.
    """

    def __init__(
        self,
        timeout: float = 8.0,
        follow_redirects: bool = False,
        *,
        allowed_origins: tuple[str, ...] = (),
        robots_exempt_origins: tuple[str, ...] = (),
        max_bytes: int = 2 * 1024 * 1024,
        max_requests: int = 120,
        min_interval: float = 1.0,
        max_retries: int = 2,
        deadline_seconds: float = 180.0,
    ):
        self.timeout = timeout
        self.follow_redirects = follow_redirects
        self.allowed_origins = {public_origin(item) for item in allowed_origins}
        self.robots_exempt_origins = {public_origin(item) for item in robots_exempt_origins}
        self.max_bytes = max_bytes
        self.max_requests = max_requests
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.deadline = time.monotonic() + deadline_seconds
        self.calls = 0
        self._last_request = 0.0
        self._robots: dict[str, RobotFileParser] = {}

    def _scope(self, url: str) -> str:
        origin = public_origin(url)
        if origin not in self.allowed_origins:
            raise PublicConnectorError("public_origin_not_authorized")
        return origin

    def _wire_request(self, url: str, headers: dict[str, str], method: str) -> HttpResult:
        self._scope(url)
        if self.calls >= self.max_requests:
            raise PublicConnectorError("public_request_budget_exceeded")
        wait = max(0.0, self.min_interval - (time.monotonic() - self._last_request))
        if time.monotonic() + wait >= self.deadline:
            raise PublicConnectorError("public_run_timeout")
        time.sleep(wait)
        if urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http"):
            return self._proxy_request(url, headers, method)
        parsed = urlsplit(url)
        hostname = parsed.hostname
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise PublicConnectorError("public_host_resolution_failed") from None
        global_addresses = []
        for row in addresses:
            try:
                address = ipaddress.ip_address(row[4][0])
            except (IndexError, ValueError):
                continue
            if address.is_global:
                global_addresses.append(row[4][0])
        if not global_addresses:
            raise PublicConnectorError("private_or_local_host_blocked")
        timeout = min(self.timeout, max(0.1, self.deadline - time.monotonic()))
        connection = http.client.HTTPSConnection(hostname, port=443, timeout=timeout)
        self.calls += 1
        self._last_request = time.monotonic()
        try:
            sock = socket.create_connection((global_addresses[0], 443), timeout=timeout)
            try:
                connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=hostname)
            except Exception:
                sock.close()
                raise
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            try:
                length = int(response_headers.get("content-length", "0"))
            except ValueError:
                raise PublicConnectorError("public_content_length_invalid") from None
            if length > self.max_bytes:
                raise PublicConnectorError("public_response_budget_exceeded")
            chunks = []
            size = 0
            while True:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicConnectorError("public_run_timeout")
                # http.client may clear ``connection.sock`` after a complete
                # response body has been consumed.  Calling settimeout on the
                # cleared attribute turns a normal EOF into an uncaught
                # AttributeError and surfaces as HTTP 500.
                response_socket = connection.sock
                if response_socket is None:
                    response_file = getattr(response, "fp", None)
                    raw_socket = getattr(getattr(response_file, "raw", None), "_sock", None)
                    response_socket = raw_socket
                if response_socket is not None:
                    response_socket.settimeout(min(self.timeout, remaining))
                chunk = response.read(min(64 * 1024, self.max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_bytes:
                    raise PublicConnectorError("public_response_budget_exceeded")
                chunks.append(chunk)
            return HttpResult(response.status, response_headers, b"".join(chunks))
        except PublicConnectorError:
            raise
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            raise PublicConnectorError("public_connector_network_error") from None
        finally:
            connection.close()

    def _proxy_request(self, url: str, headers: dict[str, str], method: str) -> HttpResult:
        """Use an operator-configured proxy while preserving connector limits."""
        parsed = urlsplit(url)
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise PublicConnectorError("public_host_resolution_failed") from None
        if not any(ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise PublicConnectorError("private_or_local_host_blocked")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        request = urllib.request.Request(url, headers=headers, method=method)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            NoRedirect(),
        )
        self.calls += 1
        self._last_request = time.monotonic()
        timeout = min(self.timeout, max(0.1, self.deadline - time.monotonic()))
        try:
            with opener.open(request, timeout=timeout) as response:
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                try:
                    length = int(response_headers.get("content-length", "0"))
                except ValueError:
                    raise PublicConnectorError("public_content_length_invalid") from None
                if length > self.max_bytes:
                    raise PublicConnectorError("public_response_budget_exceeded")
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise PublicConnectorError("public_response_budget_exceeded")
                return HttpResult(response.status, response_headers, body)
        except urllib.error.HTTPError as error:
            response_headers = {key.lower(): value for key, value in error.headers.items()}
            body = error.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise PublicConnectorError("public_response_budget_exceeded")
            return HttpResult(error.code, response_headers, body)
        except PublicConnectorError:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            raise PublicConnectorError("public_connector_network_error") from None

    def _bounded_request(self, url: str, headers: dict[str, str], method: str) -> HttpResult:
        for attempt in range(self.max_retries + 1):
            try:
                result = self._wire_request(url, headers, method)
                if result.status == 429:
                    raise PublicConnectorError("public_rate_limited")
                if result.status not in {500, 502, 503, 504}:
                    return result
                code = "public_service_unavailable"
            except PublicConnectorError as error:
                code = str(error)
                if code != "public_connector_network_error":
                    raise
            if attempt >= self.max_retries:
                raise PublicConnectorError(code)
            delay = 2 ** attempt
            if time.monotonic() + delay >= self.deadline:
                raise PublicConnectorError("public_run_timeout")
            time.sleep(delay)
        raise PublicConnectorError("public_connector_network_error")

    def _check_robots(self, url: str) -> None:
        origin = self._scope(url)
        if origin in self.robots_exempt_origins:
            return
        if origin not in self._robots:
            response = self._bounded_request(
                origin + "/robots.txt", {"User-Agent": USER_AGENT}, "GET"
            )
            parser = RobotFileParser()
            if response.status == 404:
                parser.parse(["User-agent: *", "Disallow:"])
            elif response.status == 200:
                parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
            elif response.status == 400 and response.headers.get("x-error-status") == "404":
                parser.parse(["User-agent: *", "Disallow:"])
            else:
                raise PublicConnectorError("robots_unavailable_or_denied")
            self._robots[origin] = parser
        parser = self._robots[origin]
        if not parser.can_fetch(USER_AGENT, url):
            raise PublicConnectorError("robots_access_denied")
        delay = parser.crawl_delay(USER_AGENT)
        if delay:
            self.min_interval = max(self.min_interval, float(delay))

    def request(self, url: str, *, headers: dict[str, str] | None = None, method: str = "GET") -> HttpResult:
        current = url
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if method != "GET":
            raise PublicConnectorError("public_method_not_allowed")
        for _ in range(6):
            self._check_robots(current)
            response = self._bounded_request(current, request_headers, method)
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            if not self.follow_redirects:
                raise PublicConnectorError("public_redirect_not_allowed")
            target = urljoin(current, response.headers.get("location", ""))
            if target == current:
                raise PublicConnectorError("public_redirect_invalid")
            if self._scope(target) != self._scope(current):
                request_headers = {
                    key: value for key, value in request_headers.items()
                    if key.lower() not in {"authorization", "cookie"}
                }
            current = target
        raise PublicConnectorError("public_redirect_limit_exceeded")
