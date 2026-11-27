"""In-memory stand-in for requests.Session that serves a fake Mailchimp account."""

from __future__ import annotations

import json
from urllib.parse import urlparse


class FakeResponse:
    def __init__(self, status: int, body=None, headers=None, raw: bytes | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode(errors="replace")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


class FakeMailchimp:
    """Routes: path -> list of items (paginated under ``key``) or dict (object)."""

    def __init__(self, host="us1.api.mailchimp.com"):
        self.host = host
        self.collections: dict[str, tuple[str, list]] = {}
        self.objects: dict[str, dict] = {}
        self.errors: dict[str, list[int]] = {}  # path -> statuses to return first
        self.raw: dict[str, bytes] = {}  # full URL -> bytes
        self.calls: list[tuple[str, str, dict, dict]] = []

    def collection(self, path, key, items):
        self.collections[path] = (key, items)

    def request(self, method, url, params=None, headers=None, timeout=None, json=None, stream=False):
        params = params or {}
        self.calls.append((method, url, dict(params), dict(headers or {})))
        if url in self.raw:
            return FakeResponse(200, raw=self.raw[url])
        parsed = urlparse(url)
        assert parsed.netloc == self.host, url
        path = parsed.path.removeprefix("/3.0") or "/"
        queued = self.errors.get(path)
        if queued:
            status = queued.pop(0)
            return FakeResponse(status, {"title": "err", "detail": f"status {status}"})
        if path in self.collections:
            key, items = self.collections[path]
            if "status" in params:
                items = [i for i in items if i.get("status") == params["status"]]
            elif key == "members":
                items = [i for i in items if i.get("status") != "archived"]
            off, cnt = int(params.get("offset", 0)), int(params.get("count", 10))
            return FakeResponse(200, {key: items[off : off + cnt], "total_items": len(items)})
        if (method, path) in self.objects:
            return FakeResponse(200, self.objects[(method, path)])
        if path in self.objects:
            return FakeResponse(200, self.objects[path])
        return FakeResponse(404, {"title": "Resource Not Found", "detail": path})
