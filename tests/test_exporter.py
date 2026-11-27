import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake import FakeMailchimp  # noqa: E402

from mailchimp_exporter.client import ApiError, Credentials, MailchimpClient  # noqa: E402
from mailchimp_exporter.exporter import Exporter, FatalExportError  # noqa: E402
from mailchimp_exporter.oauth import OAuthError, build_authorize_url, parse_callback  # noqa: E402
from mailchimp_exporter.resources import walk  # noqa: E402

CREDS = Credentials(api_endpoint="https://us1.api.mailchimp.com", token="tok", scheme="oauth")


def make_client(fake, page_size=2):
    return MailchimpClient(CREDS, session=fake, page_size=page_size, sleep=lambda s: None, backoff_base=0)


def account():
    fake = FakeMailchimp()
    fake.objects["/"] = {"account_id": "a1", "account_name": "Acme"}
    fake.collection("/lists", "lists", [{"id": "L1", "name": "Main"}])
    fake.collection(
        "/lists/L1/members",
        "members",
        [
            {"id": "h1", "email_address": "a@x.test", "status": "subscribed",
             "merge_fields": {"FNAME": "Ann"}, "tags": [{"id": 1, "name": "vip"}]},
            {"id": "h2", "email_address": "b@x.test", "status": "unsubscribed",
             "merge_fields": {"FNAME": "Bob", "LNAME": "B"}, "tags": []},
            {"id": "h3", "email_address": "c@x.test", "status": "cleaned", "merge_fields": {}},
            {"id": "h4", "email_address": "d@x.test", "status": "archived", "merge_fields": {}},
        ],
    )
    fake.collection("/lists/L1/segments", "segments", [{"id": 7, "name": "Seg"}])
    fake.collection("/lists/L1/segments/7/members", "members", [{"id": "h1", "email_address": "a@x.test"}])
    fake.collection("/campaigns", "campaigns", [{"id": "C1", "type": "regular"}])
    fake.objects["/campaigns/C1/content"] = {"html": "<p>Hi</p>", "plain_text": "Hi"}
    fake.collection("/reports", "reports", [{"id": "C1", "type": "regular"}])
    fake.collection("/reports/C1/email-activity", "emails", [{"email_address": "a@x.test", "activity": []}])
    fake.collection("/reports/C1/click-details", "urls_clicked", [{"id": "u1", "url": "https://e.test"}])
    fake.collection("/reports/C1/click-details/u1/members", "members", [{"email_address": "a@x.test"}])
    url = "https://mcusercontent.com/abc/images/logo.png"
    fake.collection("/file-manager/files", "files", [{"id": 5, "name": "logo.png", "full_size_url": url, "size": 4}])
    fake.raw[url] = b"\x89PNG"
    return fake


def read_jsonl(p):
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def test_full_export_layout_and_manifest(tmp_path):
    fake = account()
    manifest = Exporter(make_client(fake), tmp_path, workers=3).run()

    assert json.loads((tmp_path / "account.json").read_text())["account_name"] == "Acme"
    members = read_jsonl(tmp_path / "lists/L1/members.jsonl")
    assert [m["id"] for m in members] == ["h1", "h2", "h3"]  # paginated across 2 pages
    assert [m["id"] for m in read_jsonl(tmp_path / "lists/L1/members_archived.jsonl")] == ["h4"]
    assert read_jsonl(tmp_path / "lists/L1/segments/7/members.jsonl")[0]["id"] == "h1"
    assert (tmp_path / "campaigns/C1/content.html").read_text() == "<p>Hi</p>"
    assert (tmp_path / "campaigns/C1/content.txt").read_text() == "Hi"
    assert (tmp_path / "reports/C1/email_activity.jsonl").exists()
    assert (tmp_path / "files/5-logo.png").read_bytes() == b"\x89PNG"
    # Optional per-link click members are off by default.
    assert not (tmp_path / "reports/C1/click_details/u1/members.jsonl").exists()

    rows = list(csv.reader((tmp_path / "lists/L1/members.csv").open()))
    assert rows[0][-2:] == ["FNAME", "LNAME"]
    assert rows[1][0] == "a@x.test" and "vip" in rows[1]

    assert manifest["summary"]["units_failed"] == 0
    assert manifest["summary"]["count_mismatches"] == 0
    # Endpoints the fake doesn't serve are recorded as skipped, not failures.
    assert any(s["unit"] == "automations.jsonl" for s in manifest["skipped"])


def test_credentials_never_sent_to_file_host(tmp_path):
    fake = account()
    Exporter(make_client(fake), tmp_path).run()
    for method, url, params, headers in fake.calls:
        if "mcusercontent.com" in url:
            assert "Authorization" not in headers
        else:
            assert headers["Authorization"] == "Bearer tok"


def test_options_enable_expensive_children(tmp_path):
    fake = account()
    Exporter(make_client(fake), tmp_path, options=frozenset({"click-members"})).run()
    assert (tmp_path / "reports/C1/click_details/u1/members.jsonl").exists()


def test_retry_on_429_then_success(tmp_path):
    fake = account()
    fake.errors["/campaigns"] = [429, 503]
    manifest = Exporter(make_client(fake), tmp_path, only=frozenset({"campaigns"})).run()
    assert manifest["summary"]["units_failed"] == 0
    assert (tmp_path / "campaigns.jsonl").exists()


def test_resume_skips_completed_units_and_retries_failures(tmp_path):
    fake = account()
    client = MailchimpClient(CREDS, session=fake, page_size=2, sleep=lambda s: None, max_retries=0)
    fake.errors["/lists/L1/segments"] = [500]
    first = Exporter(client, tmp_path, only=frozenset({"lists"})).run()
    assert [e["unit"] for e in first["errors"]] == ["lists/L1/segments.jsonl"]
    assert not (tmp_path / "lists/L1/segments.jsonl").exists()

    fake.calls.clear()
    second = Exporter(client, tmp_path, only=frozenset({"lists"})).run()
    fetched = {urlparse_path(u) for _, u, _, _ in fake.calls}
    assert "/3.0/lists/L1/segments" in fetched
    assert "/3.0/lists/L1/segments/7/members" in fetched
    assert "/3.0/lists" not in fetched and "/3.0/lists/L1/members" not in fetched
    assert second["summary"]["units_failed"] == 0


def urlparse_path(u):
    from urllib.parse import urlparse

    return urlparse(u).path


def test_401_is_fatal(tmp_path):
    fake = account()
    fake.errors["/"] = [401]
    with pytest.raises(FatalExportError):
        Exporter(make_client(fake), tmp_path, only=frozenset({"account"})).run()


def test_partial_pagination_failure_leaves_no_file(tmp_path):
    fake = account()

    client = MailchimpClient(CREDS, session=fake, page_size=1, sleep=lambda s: None, max_retries=0)
    orig = fake.request

    def request(method, url, params=None, **kw):
        if url.endswith("/lists/L1/members") and (params or {}).get("offset") == 1:
            from fake import FakeResponse

            return FakeResponse(502, {"title": "bad gateway"})
        return orig(method, url, params=params, **kw)

    fake.request = request
    manifest = Exporter(client, tmp_path, only=frozenset({"lists"})).run()
    assert not (tmp_path / "lists/L1/members.jsonl").exists()
    assert not (tmp_path / "lists/L1/members.jsonl.part").exists()
    assert any(e["unit"] == "lists/L1/members.jsonl" for e in manifest["errors"])


def test_client_refuses_foreign_host():
    client = make_client(FakeMailchimp())
    with pytest.raises(ValueError):
        client.get("https://evil.test/3.0/lists")


def test_api_key_credentials():
    c = Credentials.from_api_key("abc123-us19")
    assert c.api_endpoint == "https://us19.api.mailchimp.com"
    assert c.authorization().startswith("Basic ")
    with pytest.raises(ValueError):
        Credentials.from_api_key("nodatacenter")


def test_account_export_flow(tmp_path):
    fake = account()
    fake.objects[("POST", "/account-exports")] = {"export_id": 42}
    zip_url = "https://exports.mailchimp.test/42.zip"
    fake.objects["/account-exports/42"] = {"export_id": 42, "finished": True, "download_url": zip_url}
    fake.raw[zip_url] = b"PK\x03\x04"
    ex = Exporter(make_client(fake), tmp_path, only=frozenset({"account"}))
    export_id = ex.start_account_export(["audiences"])
    assert export_id == "42"
    assert ex.start_account_export(["audiences"]) == "42"  # resumes, no second POST
    assert sum(1 for m, *_ in fake.calls if m == "POST") == 1
    ex.run()
    target = ex.finish_account_export(export_id, sleep=lambda s: None)
    assert target.read_bytes() == b"PK\x03\x04"
    assert ex.write_manifest(ex.started_at)["extra_files"] == [target.relative_to(tmp_path).as_posix()]


def test_oauth_callback_parsing():
    url = build_authorize_url("cid", 8765, "st")
    assert url.startswith("https://login.mailchimp.com/oauth2/authorize?")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback" in url
    assert parse_callback("http://127.0.0.1:8765/oauth/callback?code=abc&state=st", "st") == "abc"
    with pytest.raises(OAuthError):
        parse_callback("http://127.0.0.1:8765/oauth/callback?code=abc&state=other", "st")
    with pytest.raises(OAuthError):
        parse_callback("?error=access_denied&state=st", "st")


def test_every_child_placeholder_is_provided_by_an_ancestor():
    import string

    def check(resources, available):
        for r in resources:
            fields = {f for _, f, _, _ in string.Formatter().parse(r.path) if f}
            assert fields <= available, (r.name, fields - available)
            check(r.children, available | ({r.param} if r.param else set()))

    from mailchimp_exporter.resources import TREE

    check(TREE, set())
    assert len(list(walk())) > 50
