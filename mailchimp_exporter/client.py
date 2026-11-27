"""Thin HTTP client for the Mailchimp Marketing API v3.

Handles authentication (OAuth 2 access token or API key), retries with
exponential backoff (honouring ``Retry-After``), offset pagination and file
downloads. Credentials are only ever sent to the account's own API host.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import requests

from . import __version__

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_PAGE_SIZE = 1000
USER_AGENT = f"mailchimp-exporter/{__version__}"


class ApiError(Exception):
    """A request failed permanently (after retries, or with a non-retryable status)."""

    def __init__(self, status: int | None, method: str, url: str, detail: str):
        self.status = status
        self.method = method
        self.url = url
        self.detail = detail
        super().__init__(f"{method} {url} -> {status}: {detail}")


@dataclass
class Credentials:
    """How to reach one Mailchimp account.

    ``api_endpoint`` is the data-centre specific base URL, e.g.
    ``https://us6.api.mailchimp.com`` (without ``/3.0``).
    ``scheme`` is ``"oauth"`` for an OAuth 2 access token or ``"apikey"``.
    """

    api_endpoint: str
    token: str
    scheme: str = "oauth"
    account_name: str | None = None

    @classmethod
    def from_api_key(cls, api_key: str) -> "Credentials":
        key = api_key.strip()
        if "-" not in key:
            raise ValueError("Mailchimp API keys end in '-<dc>', e.g. '...-us6'")
        dc = key.rsplit("-", 1)[1]
        if not dc.isalnum():
            raise ValueError(f"invalid data centre suffix in API key: {dc!r}")
        return cls(api_endpoint=f"https://{dc}.api.mailchimp.com", token=key, scheme="apikey")

    def authorization(self) -> str:
        if self.scheme == "oauth":
            return f"Bearer {self.token}"
        if self.scheme == "apikey":
            raw = f"mailchimp-exporter:{self.token}".encode()
            return "Basic " + base64.b64encode(raw).decode()
        raise ValueError(f"unknown credential scheme {self.scheme!r}")


def _error_detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - any non-JSON body
        return (getattr(resp, "text", "") or "")[:500]
    if isinstance(body, dict):
        parts = [str(body.get("title") or ""), str(body.get("detail") or "")]
        if body.get("errors"):
            parts.append(str(body["errors"]))
        return " - ".join(p for p in parts if p) or str(body)[:500]
    return str(body)[:500]


def _guess_collection(page: dict) -> list:
    """Fallback when the collection key is unknown: the single list-valued field."""
    lists = [v for k, v in page.items() if isinstance(v, list) and not k.startswith("_")]
    return lists[0] if len(lists) == 1 else []


class MailchimpClient:
    def __init__(
        self,
        creds: Credentials,
        session: Any | None = None,
        *,
        max_retries: int = 8,
        timeout: float = 120.0,
        page_size: int = MAX_PAGE_SIZE,
        backoff_base: float = 1.0,
        backoff_cap: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.creds = creds
        self.base = creds.api_endpoint.rstrip("/") + "/3.0"
        self.api_host = urlparse(self.base).netloc.lower()
        self.session = session if session is not None else requests.Session()
        self.max_retries = max_retries
        self.timeout = timeout
        self.page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep

    # -- low level -----------------------------------------------------------------

    def _headers(self, authed: bool = True) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if authed:
            headers["Authorization"] = self.creds.authorization()
        return headers

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay: float | None = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
            delay += random.uniform(0, delay / 2)
        log.debug("backing off %.1fs (attempt %d)", delay, attempt + 1)
        self._sleep(delay)

    def _send(self, method: str, url: str, *, authed: bool = True, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                resp = self.session.request(
                    method, url, headers=self._headers(authed), timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise ApiError(None, method, url, str(exc)) from exc
                log.warning("%s %s failed (%s); retrying", method, url, exc)
                self._backoff(attempt, None)
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                log.info("%s %s -> %s; retrying", method, url, resp.status_code)
                self._backoff(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, method, url, _error_detail(resp))
            return resp

    def request(self, method: str, path: str, params: dict | None = None, json: Any = None) -> dict:
        url = path if path.startswith("https://") else self.base + path
        if urlparse(url).netloc.lower() != self.api_host:
            raise ValueError(f"refusing to send credentials to {url}")
        resp = self._send(method, url, params=params, json=json)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params=params)

    # -- pagination ------------------------------------------------------------------

    def paginate(
        self,
        path: str,
        key: str,
        params: dict | None = None,
        meta: dict | None = None,
    ) -> Iterator[dict]:
        """Yield every item of a collection endpoint, following ``offset``/``count``.

        If ``meta`` is given, ``meta["total_items"]`` is set from the API's
        reported total so callers can verify completeness.
        """
        base_params = dict(params or {})
        base_params.setdefault("exclude_fields", f"_links,{key}._links")
        offset = 0
        while True:
            page = self.get(path, {**base_params, "count": self.page_size, "offset": offset})
            items = page.get(key)
            if not isinstance(items, list):
                items = _guess_collection(page)
            total = page.get("total_items")
            if meta is not None and total is not None:
                meta["total_items"] = total
            yield from items
            offset += len(items)
            if not items:
                break
            if total is not None:
                if offset >= total:
                    break
            elif len(items) < self.page_size:
                break

    # -- downloads -------------------------------------------------------------------

    def download(self, url: str, dest: Path) -> int:
        """Stream ``url`` to ``dest`` atomically; return bytes written.

        Credentials are attached only when the URL is on the account's API host;
        hosted files (images, export archives) are fetched without them.
        """
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError(f"refusing non-HTTPS download: {url}")
        authed = parsed.netloc.lower() == self.api_host
        resp = self._send("GET", url, authed=authed, stream=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        written = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        os.replace(tmp, dest)
        return written
