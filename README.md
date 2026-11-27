# mailchimp-exporter

Exports **all of your own Mailchimp account data** through the official
[Marketing API](https://mailchimp.com/developer/marketing/api/), using an OAuth 2
token you grant to yourself, or an API key. The output is a folder of plain
JSON / JSONL / CSV / HTML files you can keep, search, or import into another tool.

It reads only. It never changes anything in your account.

> Your export holds personal data about your contacts (emails, names, IPs,
> engagement). Store it somewhere encrypted and treat it as you would the account.

## What gets exported

Run `mailchimp-exporter resources` for the full tree. In short:

| Area | Contents |
|---|---|
| Account | account profile, Chimp Chatter activity feed |
| Audiences | every list, with all contacts (incl. archived), merge fields, groups/interests, segments & tags with their members, signup forms, webhooks, surveys, growth history, daily activity, email clients, locations, abuse reports. Plus a `members.csv` per audience, ready to import elsewhere |
| Campaigns | settings, HTML / plain-text / archive content (saved as `.html`/`.txt` as well), feedback, send checklist, campaign folders |
| Reports | per-campaign summary, per-recipient email activity, opens, clicks, sent-to, unsubscribes, abuse reports, locations, domain performance, advice, A/B sub-reports, e-commerce product activity |
| Automations | classic automations, their emails, queues and removed subscribers |
| Content | your own templates (with default content), template folders, File Manager folders, and the actual **image/file bytes** |
| Other | landing pages + content, connected sites, verified domains, batch webhooks |
| E-commerce | stores, customers, orders, products, carts, promo rules & codes |

Two expensive extras are off by default, because they cost one request per contact or per link:

* `--option member-details`: per-contact activity feed, notes and tags
* `--option click-members`: who clicked each link of each campaign (the per-recipient
  `email_activity` report already includes clicks)

`--everything` turns both on. `--account-export` also asks Mailchimp to build its
own server-side export ZIP and downloads it when the walk finishes.

## Install

```bash
pip install .
```

## Authorize

**Option A: OAuth 2 (recommended).** Register an app in your Mailchimp account
(*Account → Extras → Registered apps*) with redirect URI
`http://127.0.0.1:8765/oauth/callback`, then:

```bash
export MAILCHIMP_CLIENT_SECRET=...   # or you'll be prompted
```

```bash
mailchimp-exporter auth --client-id YOUR_CLIENT_ID
```

A browser opens. You approve, and the token is saved to
`~/.config/mailchimp-exporter/credentials.json` (mode 0600). On a headless
machine, add `--manual` and paste the URL you were redirected to.

**Option B: API key.** Create one under *Account → Extras → API keys*:

```bash
export MAILCHIMP_API_KEY=xxxxxxxx-us6
```

## Export

```bash
mailchimp-exporter -v export --out ./mailchimp-export
```

Useful flags: `--only lists,campaigns`, `--workers 4` (Mailchimp allows at most
10 simultaneous connections), `--no-files`, `--everything`, `--account-export`.

**It's resumable.** Large accounts can take hours. If it's interrupted, or some
requests fail, run the same command again: finished endpoints are skipped and only
the missing ones are fetched.

## Output

```
mailchimp-export/
  manifest.json                # what was fetched, counts vs. API totals, errors, skips
  account.json
  lists.jsonl
  lists/<list_id>/members.jsonl
  lists/<list_id>/members.csv
  lists/<list_id>/segments/<segment_id>/members.jsonl
  campaigns.jsonl
  campaigns/<campaign_id>/content.html
  reports/<campaign_id>/email_activity.jsonl
  files.jsonl
  files/<id>-<name>.png
  ...
```

Every collection is JSON Lines, one API object per line, exactly as the API returned
it (minus `_links`). `manifest.json` lists any endpoint where the number of items
written differs from the API's `total_items`, usually because data changed during the export.

Endpoints that don't apply to your account (a feature your plan lacks, a deprecated
product) come back as 4xx and are listed under `skipped`. They are not failures.

## Design

See [docs/DESIGN.md](docs/DESIGN.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
