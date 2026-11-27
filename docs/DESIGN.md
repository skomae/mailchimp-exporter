# Design

## Goal

Give an account owner a complete, offline, vendor-neutral copy of everything
their Mailchimp account holds, using only the public Marketing API v3 and
credentials they control. It must work on large accounts (millions of contacts,
thousands of campaigns), survive interruptions, and prove what it got.

## Non-goals

* Writing to Mailchimp, or migrating into a specific other platform. The output
  is plain files; importers belong elsewhere.
* Data the public API doesn't expose: new-style Customer Journeys (only classic
  automations have list endpoints), the drag-and-drop builder's internal
  representation beyond the rendered HTML, billing history, and team/user management.
  `--account-export` (Mailchimp's own ZIP) is the fallback for anything
  the API walk misses.

## Architecture

```
cli.py ──> oauth.py      authorization-code flow, metadata lookup, 0600 credential file
   │
   └──> exporter.py ──> client.py     auth headers, retries/backoff, pagination, downloads
            │
            └── resources.py          declarative endpoint tree (what to fetch)
```

### Resource tree (`resources.py`)

All coverage is data, not code. A `Resource` is an endpoint path, plus the JSON key that
holds its collection (or none, for a single object), plus a `param`. The `param` is the name under which
each item's id is passed down to child endpoints. For example
`lists` → `param="list_id"` → child `members` at `/lists/{list_id}/members`.
Adding coverage means adding one line. A test checks that every placeholder is
supplied by an ancestor.

Optional resources carry an `option` tag (e.g. `member-details`) and are
pruned unless enabled, because they multiply request counts by contact count.

### Work scheduling (`exporter.py`)

A *unit* is one endpoint at one parent (e.g. `lists/abc/members.jsonl`).
Units are tasks on a thread pool. When a unit finishes, its items' ids produce
the child tasks, which are submitted to the same pool. The driver loop waits on
`FIRST_COMPLETED`, so there are no nested pools and no deadlock. Default
concurrency is 4, under Mailchimp's 10-connection-per-account limit.

### Pagination and memory

`count=1000&offset=N`, stopping when `offset >= total_items` (or at a short page if
no total is given). Items stream straight to a `.part` file. Only resources that
have children keep their items in memory, and those are small (lists, campaigns,
segments). Member lists stream to disk without being held. `exclude_fields=_links`
roughly halves the payload.

### Reliability

* **Retries:** 429/5xx/connection errors, exponential backoff with jitter,
  `Retry-After` honoured, up to 8 attempts.
* **Atomic writes:** `.part` + `os.replace`. A unit that fails part-way leaves no
  file behind.
* **Resume:** every finished unit is appended to `.progress.jsonl`. On re-run,
  finished units are skipped, and their files are re-read to rebuild the child tasks.
* **Error classes:** 401 aborts (token revoked). 400/403/404/405/501 mean
  "not applicable": recorded as *skipped* and marked done. Anything else after
  retries is an *error*: it is not marked done, so the next run retries it, and the exit code is 1.
* **Verification:** written count vs. `total_items` per unit, reported in
  `manifest.json`.

### Security

* Tokens are stored at `~/.config/mailchimp-exporter/credentials.json` with mode 0600.
  The client secret is read from an env var or prompt, never from argv.
* OAuth `state` is random and checked. The redirect listener binds to 127.0.0.1 only.
* The `Authorization` header is attached only to requests for the account's own
  `*.api.mailchimp.com` host. File Manager assets and export archives on CDNs are
  fetched without credentials. Non-HTTPS downloads are refused.
* Downloaded filenames are sanitised. Ids are URL-quoted into paths.

### Account export ZIP

With `--account-export`, `POST /account-exports` is sent *before* the API walk,
so Mailchimp builds the archive in parallel. After the walk, the exporter polls until
`download_url` appears and downloads the archive. The export id is persisted, so a
re-run resumes waiting instead of requesting a duplicate.

## Unverified assumptions

The Mailchimp docs could not be reached while this was written. The following
come from prior knowledge of the API and should be checked against the live docs:

1. The `include_stages` enum for `POST /account-exports` (`DEFAULT_EXPORT_STAGES`
   in `cli.py`). It can be overridden with `--account-export-stages`.
2. That the default `GET /lists/{id}/members` listing omits archived contacts,
   and that `status=archived` returns them.
3. That `/account-exports/{id}`'s `download_url` doesn't need the API token
   (if it's on the API host, the token is sent automatically).
4. That there is no hard maximum `offset`. If there is one, very large audiences
   would show up as count mismatches in the manifest. The fix would be to switch
   members to the Batch Operations API.

## Possible extensions

* Batch Operations API (`/batches`) for audiences with millions of contacts:
  fewer requests, and it sidesteps any offset limits.
* `since_last_changed` incremental re-exports.
* Importers for other platforms that read this output format.
