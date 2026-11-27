"""Command-line interface: ``mailchimp-exporter auth | export | resources``."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from .client import Credentials, MailchimpClient
from .exporter import Exporter, FatalExportError
from .oauth import OAuthError, authorize, default_credentials_path, load_credentials, save_credentials
from .resources import OPTIONS, top_level_names, walk

# Stages for Mailchimp's server-side account export (POST /account-exports).
# NOTE: taken from memory of the API reference, not re-verified against the live
# docs; if Mailchimp rejects one, its 400 response names the valid values and
# you can pass your own list with --account-export-stages.
DEFAULT_EXPORT_STAGES = [
    "audiences", "gallery_files", "templates", "segments", "promo_codes",
    "products", "orders", "customers", "carts", "campaigns", "reports",
]


def _resolve_credentials(args: argparse.Namespace) -> Credentials:
    api_key = getattr(args, "api_key", None) or os.environ.get("MAILCHIMP_API_KEY")
    if api_key:
        return Credentials.from_api_key(api_key)
    path = Path(args.credentials)
    if not path.exists():
        raise SystemExit(
            f"No credentials at {path}. Run `mailchimp-exporter auth` first, "
            "or set MAILCHIMP_API_KEY to use an API key instead of OAuth."
        )
    return load_credentials(path)


def cmd_auth(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get("MAILCHIMP_CLIENT_ID")
    if not client_id:
        raise SystemExit("--client-id (or MAILCHIMP_CLIENT_ID) is required")
    client_secret = os.environ.get("MAILCHIMP_CLIENT_SECRET") or getpass.getpass("OAuth client secret: ")
    try:
        creds = authorize(
            client_id, client_secret, port=args.port, open_browser=not args.no_browser, manual=args.manual
        )
    except OAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path = Path(args.credentials)
    save_credentials(creds, path)
    who = f" for account {creds.account_name!r}" if creds.account_name else ""
    print(f"Saved credentials{who} ({creds.api_endpoint}) to {path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    creds = _resolve_credentials(args)
    client = MailchimpClient(creds, page_size=args.page_size)
    only = None
    if args.only:
        only = frozenset(n.strip() for n in args.only.split(",") if n.strip())
        unknown = only - set(top_level_names())
        if unknown:
            raise SystemExit(f"unknown resource(s): {', '.join(sorted(unknown))}; see `mailchimp-exporter resources`")
    options = set(args.option or [])
    if args.everything:
        options |= set(OPTIONS)
    exporter = Exporter(
        client,
        Path(args.out),
        workers=args.workers,
        options=frozenset(options),
        only=only,
        download_files=not args.no_files,
    )
    stages = [s.strip() for s in args.account_export_stages.split(",")] if args.account_export_stages else DEFAULT_EXPORT_STAGES
    try:
        account = client.get("/", {"fields": "account_id,account_name,email"})
        creds.account_name = creds.account_name or account.get("account_name")
        print(f"Exporting account {account.get('account_name')!r} to {args.out}")
        export_id = exporter.start_account_export(stages) if args.account_export else None
        manifest = exporter.run()
        if export_id:
            print("API walk done; waiting for Mailchimp's account export archive ...")
            exporter.finish_account_export(export_id, poll_interval=args.poll_interval)
            manifest = exporter.write_manifest(exporter.started_at)
    except FatalExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.", file=sys.stderr)
        return 130

    s = manifest["summary"]
    print(
        f"Done: {s['items_written']} items in {s['units_ok']} endpoints, "
        f"{s['units_skipped']} not applicable, {s['units_failed']} failed, "
        f"{s['count_mismatches']} count mismatches. See {Path(args.out) / 'manifest.json'}"
    )
    if s["units_failed"]:
        print("Some requests failed; re-run the same command to retry just those.", file=sys.stderr)
        return 1
    return 0


def cmd_resources(args: argparse.Namespace) -> int:
    for dotted, r in walk():
        depth = dotted.count(".")
        kind = "list  " if r.is_collection else "object"
        extra = f"  [--option {r.option}]" if r.option else ""
        print(f"{'  ' * depth}{r.name:<{30 - 2 * depth}} {kind} GET {r.path}{extra}")
    print("\nOptions:")
    for name, desc in OPTIONS.items():
        print(f"  {name:<16} {desc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailchimp-exporter", description=__doc__)
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--credentials", default=str(default_credentials_path()), help="credentials file")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("auth", help="authorize with Mailchimp via OAuth 2 and save the token")
    a.add_argument("--client-id", help="OAuth client id of your registered app (or MAILCHIMP_CLIENT_ID)")
    a.add_argument("--port", type=int, default=8765, help="local port for the redirect (default 8765)")
    a.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    a.add_argument("--manual", action="store_true", help="paste the redirect URL by hand (headless machines)")
    a.set_defaults(func=cmd_auth)

    e = sub.add_parser("export", help="export all account data")
    e.add_argument("--out", default="mailchimp-export", help="output directory (default ./mailchimp-export)")
    e.add_argument("--api-key", help="use an API key instead of saved OAuth credentials (or MAILCHIMP_API_KEY)")
    e.add_argument("--workers", type=int, default=4, help="parallel requests (default 4; Mailchimp caps at 10)")
    e.add_argument("--page-size", type=int, default=1000, help="items per page (max 1000)")
    e.add_argument("--only", help="comma-separated top-level resources, e.g. lists,campaigns")
    e.add_argument("--option", action="append", choices=sorted(OPTIONS), help="enable an expensive extra")
    e.add_argument("--everything", action="store_true", help="enable every option")
    e.add_argument("--no-files", action="store_true", help="don't download File Manager assets")
    e.add_argument("--account-export", action="store_true", help="also request Mailchimp's own export ZIP")
    e.add_argument("--account-export-stages", help="comma-separated include_stages for --account-export")
    e.add_argument("--poll-interval", type=float, default=30.0, help="seconds between account export polls")
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("resources", help="list every endpoint the exporter retrieves")
    r.set_defaults(func=cmd_resources)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    return args.func(args)
