"""OAuth 2 authorization-code flow for Mailchimp, plus local credential storage.

Mailchimp access tokens do not expire (they stay valid until the user revokes
the app), so there is no refresh step. After the token exchange we call the
metadata endpoint to learn which data centre (``api_endpoint``) the account
lives in.

You need your own OAuth app, registered in your Mailchimp account under
Account -> Extras -> Registered apps, with the redirect URI
``http://127.0.0.1:<port>/oauth/callback``.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .client import USER_AGENT, Credentials

AUTHORIZE_URL = "https://login.mailchimp.com/oauth2/authorize"
TOKEN_URL = "https://login.mailchimp.com/oauth2/token"
METADATA_URL = "https://login.mailchimp.com/oauth2/metadata"
CALLBACK_PATH = "/oauth/callback"


class OAuthError(Exception):
    pass


def default_credentials_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "mailchimp-exporter" / "credentials.json"


def redirect_uri(port: int) -> str:
    # Mailchimp asks for 127.0.0.1 rather than "localhost" for local redirects.
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def build_authorize_url(client_id: str, port: int, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri(port),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def parse_callback(url_or_query: str, expected_state: str) -> str:
    """Extract the authorization code from a redirect URL, checking ``state``."""
    query = urlparse(url_or_query).query if "://" in url_or_query else url_or_query.lstrip("?")
    params = parse_qs(query)
    if "error" in params:
        raise OAuthError(f"authorization denied: {params['error'][0]}")
    if params.get("state", [None])[0] != expected_state:
        raise OAuthError("state mismatch in OAuth callback; aborting")
    code = params.get("code", [None])[0]
    if not code:
        raise OAuthError("no authorization code in callback")
    return code


def _wait_for_callback(port: int, state: str, timeout: float) -> str:
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if urlparse(self.path).path != CALLBACK_PATH:
                self.send_error(404)
                return
            try:
                result["code"] = parse_callback(self.path, state)
                body, status = b"Authorized. You can close this tab and return to the terminal.", 200
            except OAuthError as exc:
                result["error"] = str(exc)
                body, status = f"Authorization failed: {exc}".encode(), 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1.0
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while not result and not deadline.is_set():
            server.handle_request()
    finally:
        timer.cancel()
        server.server_close()
    if "error" in result:
        raise OAuthError(result["error"])
    if "code" not in result:
        raise OAuthError("timed out waiting for the OAuth redirect")
    return result["code"]


def exchange_code(
    client_id: str, client_secret: str, code: str, port: int, session: requests.Session | None = None
) -> str:
    s = session or requests.Session()
    resp = s.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(port),
            "code": code,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    if resp.status_code != 200:
        raise OAuthError(f"token exchange failed ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("access_token")
    if not token:
        raise OAuthError("token response had no access_token")
    return token


def fetch_metadata(token: str, session: requests.Session | None = None) -> dict:
    s = session or requests.Session()
    resp = s.get(
        METADATA_URL,
        headers={"Authorization": f"OAuth {token}", "User-Agent": USER_AGENT},
        timeout=60,
    )
    if resp.status_code != 200:
        raise OAuthError(f"metadata lookup failed ({resp.status_code}): {resp.text[:300]}")
    meta = resp.json()
    if not meta.get("api_endpoint"):
        raise OAuthError("metadata response had no api_endpoint")
    return meta


def authorize(
    client_id: str,
    client_secret: str,
    *,
    port: int = 8765,
    open_browser: bool = True,
    manual: bool = False,
    timeout: float = 300.0,
) -> Credentials:
    """Run the full browser flow and return credentials for the authorizing account."""
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(client_id, port, state)
    print("Authorize this exporter to read your Mailchimp account:\n\n  " + url + "\n")
    if manual:
        pasted = input("After approving, paste the full URL you were redirected to: ").strip()
        code = parse_callback(pasted, state)
    else:
        if open_browser:
            webbrowser.open(url)
        print(f"Waiting for the redirect on {redirect_uri(port)} ...")
        code = _wait_for_callback(port, state, timeout)
    token = exchange_code(client_id, client_secret, code, port)
    meta = fetch_metadata(token)
    return Credentials(
        api_endpoint=meta["api_endpoint"],
        token=token,
        scheme="oauth",
        account_name=meta.get("accountname"),
    )


def save_credentials(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "api_endpoint": creds.api_endpoint,
        "token": creds.token,
        "scheme": creds.scheme,
        "account_name": creds.account_name,
    }
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_credentials(path: Path) -> Credentials:
    data = json.loads(path.read_text())
    return Credentials(
        api_endpoint=data["api_endpoint"],
        token=data["token"],
        scheme=data.get("scheme", "oauth"),
        account_name=data.get("account_name"),
    )
