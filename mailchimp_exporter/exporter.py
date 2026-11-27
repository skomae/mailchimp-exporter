"""Walks the resource tree and writes everything to disk.

Design points:

* **Resumable.** Every fetched unit (one endpoint at one parent) is written to
  a temp file and renamed into place only when complete, then recorded in
  ``.progress.jsonl``. Re-running the same command skips finished units and
  re-reads their files to find children, so an interrupted multi-hour export
  picks up where it stopped.
* **Parallel within the API's limits.** Units run on a small thread pool
  (Mailchimp allows only a handful of simultaneous connections per account;
  the default of 4 workers stays under it).
* **Tolerant.** Endpoints that are unavailable for an account (feature not on
  the plan, deprecated, not applicable to a campaign type) are recorded as
  *skipped* rather than failing the run. Transient failures are recorded as
  *errors*, are not marked done, and are retried on the next run.
* **Verifiable.** The API's ``total_items`` is compared to what was written,
  and everything lands in ``manifest.json``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

from . import __version__
from .client import ApiError, MailchimpClient
from .resources import TREE, Resource

log = logging.getLogger(__name__)

PROGRESS_FILE = ".progress.jsonl"
MANIFEST_FILE = "manifest.json"
# Statuses that mean "this endpoint does not apply to this account/item";
# they are permanent, so the unit is marked done and not retried.
SKIP_STATUSES = frozenset({400, 403, 404, 405, 501})


class FatalExportError(Exception):
    pass


@dataclass
class Task:
    resource: Resource
    ctx: dict[str, str]
    rel_dir: Path  # directory relative to the export root

    @property
    def rel_file(self) -> Path:
        return self.rel_dir / self.resource.filename


@dataclass
class UnitResult:
    unit: str
    status: str  # "ok" | "skipped" | "error"
    count: int = 0
    total: int | None = None
    detail: str | None = None


def safe_name(value: str, limit: int = 120) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return (cleaned or "_")[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


@dataclass
class Exporter:
    client: MailchimpClient
    out_dir: Path
    workers: int = 4
    options: frozenset[str] = frozenset()
    only: frozenset[str] | None = None  # top-level resource names to include
    download_files: bool = True
    tree: list[Resource] = field(default_factory=lambda: TREE)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self._lock = threading.Lock()
        self._done: dict[str, dict] = {}
        self._results: list[UnitResult] = []
        self._extra_files: list[str] = []

    # -- progress bookkeeping --------------------------------------------------------

    def _load_progress(self) -> None:
        path = self.out_dir / PROGRESS_FILE
        if not path.exists():
            return
        for rec in _iter_jsonl(path):
            self._done[rec["unit"]] = rec

    def _mark_done(self, result: UnitResult) -> None:
        rec = {"unit": result.unit, "status": result.status, "count": result.count, "total": result.total}
        if result.detail:
            rec["detail"] = result.detail
        with self._lock:
            self._done[result.unit] = rec
            with open(self.out_dir / PROGRESS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")

    def _record(self, result: UnitResult) -> None:
        with self._lock:
            self._results.append(result)

    def _is_done(self, task: Task) -> bool:
        rec = self._done.get(task.rel_file.as_posix())
        if not rec:
            return False
        return rec["status"] == "skipped" or (self.out_dir / task.rel_file).exists()

    # -- tree traversal --------------------------------------------------------------

    def _active_children(self, r: Resource) -> list[Resource]:
        return [c for c in r.children if c.option is None or c.option in self.options]

    def _root_tasks(self) -> list[Task]:
        return [
            Task(r, {}, Path("."))
            for r in self.tree
            if (self.only is None or r.name in self.only) and (r.option is None or r.option in self.options)
        ]

    def _child_tasks(self, task: Task, items: Iterable[dict]) -> list[Task]:
        r = task.resource
        children = self._active_children(r)
        if not children or not r.param:
            return []
        tasks = []
        for item in items:
            item_id = item.get(r.id_field)
            if item_id in (None, ""):
                continue
            ctx = {**task.ctx, r.param: str(item_id)}
            rel_dir = task.rel_dir / r.name / safe_name(item_id)
            for child in children:
                if child.when is None or child.when(item):
                    tasks.append(Task(child, ctx, rel_dir))
        return tasks

    def _api_path(self, task: Task) -> str:
        return task.resource.path.format(**{k: quote(v, safe="") for k, v in task.ctx.items()})

    # -- fetching one unit -----------------------------------------------------------

    def _run_task(self, task: Task) -> list[Task]:
        r = task.resource
        unit = task.rel_file.as_posix()
        dest = self.out_dir / task.rel_file
        needs_items = bool(self._active_children(r) and r.param)

        if self._is_done(task):
            rec = self._done[unit]
            self._record(UnitResult(unit, rec["status"], rec.get("count", 0), rec.get("total"), rec.get("detail")))
            if rec["status"] != "ok" or not needs_items:
                return []
            items = list(_iter_jsonl(dest)) if r.is_collection else [json.loads(dest.read_text())]
            return self._child_tasks(task, items)

        try:
            if r.is_collection:
                result, items = self._fetch_collection(task, dest, keep_items=needs_items)
            else:
                obj = self.client.get(self._api_path(task), {"exclude_fields": "_links"})
                self._write_json(dest, obj)
                items = [obj]
                result = UnitResult(unit, "ok", 1, 1)
            if r.hook:
                getattr(self, f"_hook_{r.hook}")(task, dest, items)
        except ApiError as exc:
            if exc.status == 401:
                raise FatalExportError(
                    "Mailchimp rejected the credentials (401). The token may have been revoked; "
                    "run `mailchimp-exporter auth` again."
                ) from exc
            status = "skipped" if exc.status in SKIP_STATUSES else "error"
            result = UnitResult(unit, status, detail=f"{exc.status}: {exc.detail}")
            log.log(logging.INFO if status == "skipped" else logging.WARNING, "%s %s (%s)", status, unit, result.detail)
            self._record(result)
            if status == "skipped":
                self._mark_done(result)
            return []

        if result.total is not None and result.count != result.total:
            log.warning("%s: wrote %d items but API reported %d", unit, result.count, result.total)
        self._record(result)
        self._mark_done(result)
        log.info("ok %s (%d)", unit, result.count)
        return self._child_tasks(task, items)

    def _fetch_collection(self, task: Task, dest: Path, keep_items: bool) -> tuple[UnitResult, list[dict]]:
        r = task.resource
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        meta: dict[str, Any] = {}
        kept: list[dict] = []
        count = 0
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for item in self.client.paginate(self._api_path(task), r.key, dict(r.params), meta):
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                    count += 1
                    if keep_items:
                        kept.append(item)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return UnitResult(task.rel_file.as_posix(), "ok", count, meta.get("total_items")), kept

    @staticmethod
    def _write_json(dest: Path, obj: Any) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, dest)

    # -- hooks -----------------------------------------------------------------------

    def _hook_campaign_html(self, task: Task, dest: Path, items: list[dict]) -> None:
        """Also save the campaign body as plain .html/.txt files you can open directly."""
        content = items[0] if items else {}
        for field_name, suffix in (("html", "html"), ("archive_html", "archive.html"), ("plain_text", "txt")):
            if content.get(field_name):
                (dest.parent / f"content.{suffix}").write_text(content[field_name], encoding="utf-8")

    def _hook_members_csv(self, task: Task, dest: Path, items: list[dict]) -> None:
        """Write members.csv next to members.jsonl, ready to import into another tool."""
        merge_keys: list[str] = []
        seen: set[str] = set()
        for m in _iter_jsonl(dest):
            for k in (m.get("merge_fields") or {}):
                if k not in seen:
                    seen.add(k)
                    merge_keys.append(k)
        base = [
            "email_address", "status", "full_name", "email_type", "language", "vip",
            "source", "ip_signup", "timestamp_signup", "ip_opt", "timestamp_opt",
            "last_changed", "member_rating", "country_code", "tags",
        ]
        tmp = dest.with_name("members.csv.part")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(base + merge_keys)
            for m in _iter_jsonl(dest):
                mf = m.get("merge_fields") or {}
                row = [m.get(k, "") for k in base[:-2]]
                row.append((m.get("location") or {}).get("country_code", ""))
                row.append(";".join(t.get("name", "") for t in m.get("tags") or []))
                for k in merge_keys:
                    v = mf.get(k, "")
                    row.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                w.writerow(row)
        os.replace(tmp, dest.with_name("members.csv"))

    def _hook_download_files(self, task: Task, dest: Path, items: list[dict]) -> None:
        """Download the actual bytes of every File Manager asset (images, PDFs, ...)."""
        if not self.download_files:
            return
        files_dir = dest.parent / "files"
        for f in _iter_jsonl(dest):
            url = f.get("full_size_url")
            if not url:
                continue
            ext = Path(urlparse(url).path).suffix
            name = safe_name(f"{f.get('id')}-{f.get('name') or 'file'}")
            if ext and not name.lower().endswith(ext.lower()):
                name += ext
            target = files_dir / name
            if target.exists() and (not f.get("size") or target.stat().st_size == f["size"]):
                continue
            try:
                self.client.download(url, target)
            except (ApiError, ValueError) as exc:
                self._record(UnitResult(f"files/{name}", "error", detail=str(exc)))
                log.warning("could not download %s: %s", url, exc)

    # -- account export archive ------------------------------------------------------

    def start_account_export(self, stages: list[str]) -> str:
        """Ask Mailchimp to build its own account export archive (ZIP of CSVs).

        This complements the API walk with Mailchimp's server-side export. It can
        take hours for large accounts, so it is requested first and collected by
        :meth:`finish_account_export` after the walk. The export id is persisted
        so a re-run resumes waiting instead of requesting a second export.
        """
        state_file = self.out_dir / "account_export" / "request.json"
        if state_file.exists():
            export_id = str(json.loads(state_file.read_text())["export_id"])
            log.info("resuming account export %s", export_id)
            return export_id
        resp = self.client.request("POST", "/account-exports", json={"include_stages": stages})
        export_id = str(resp["export_id"])
        self._write_json(state_file, {"export_id": export_id, "stages": stages, "requested_at": _now()})
        return export_id

    def finish_account_export(
        self, export_id: str, poll_interval: float = 30.0, timeout: float = 6 * 3600, sleep=time.sleep
    ) -> Path:
        deadline = time.monotonic() + timeout
        while True:
            status = self.client.get(f"/account-exports/{quote(export_id, safe='')}")
            url = status.get("download_url")
            if url:
                break
            if time.monotonic() > deadline:
                raise FatalExportError(
                    f"account export {export_id} not ready after {timeout:.0f}s; re-run the same command to keep waiting"
                )
            log.info("account export %s not ready yet; waiting %.0fs", export_id, poll_interval)
            sleep(poll_interval)
        target = self.out_dir / "account_export" / f"mailchimp-account-export-{safe_name(export_id)}.zip"
        if not target.exists():
            size = self.client.download(url, target)
            log.info("downloaded account export (%d bytes) to %s", size, target)
        rel = target.relative_to(self.out_dir).as_posix()
        if rel not in self._extra_files:
            self._extra_files.append(rel)
        return target

    # -- driver ----------------------------------------------------------------------

    def run(self) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._load_progress()
        self.started_at = started = _now()
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            pending: set[Future] = {pool.submit(self._run_task, t) for t in self._root_tasks()}
            try:
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in done:
                        for child in fut.result():
                            pending.add(pool.submit(self._run_task, child))
            except BaseException:
                for fut in pending:
                    fut.cancel()
                raise
        return self.write_manifest(started)

    def write_manifest(self, started: str) -> dict:
        results = sorted(self._results, key=lambda r: r.unit)
        mismatches = [
            {"unit": r.unit, "written": r.count, "reported": r.total}
            for r in results
            if r.status == "ok" and r.total is not None and r.count != r.total
        ]
        manifest = {
            "tool": f"mailchimp-exporter {__version__}",
            "api_endpoint": self.client.base,
            "account_name": self.client.creds.account_name,
            "started_at": started,
            "finished_at": _now(),
            "options": sorted(self.options),
            "summary": {
                "units_ok": sum(r.status == "ok" for r in results),
                "units_skipped": sum(r.status == "skipped" for r in results),
                "units_failed": sum(r.status == "error" for r in results),
                "items_written": sum(r.count for r in results if r.status == "ok"),
                "count_mismatches": len(mismatches),
            },
            "count_mismatches": mismatches,
            "errors": [{"unit": r.unit, "detail": r.detail} for r in results if r.status == "error"],
            "skipped": [{"unit": r.unit, "detail": r.detail} for r in results if r.status == "skipped"],
            "extra_files": self._extra_files,
            "units": [
                {"unit": r.unit, "status": r.status, "count": r.count, "total": r.total} for r in results
            ],
        }
        self._write_json(self.out_dir / MANIFEST_FILE, manifest)
        return manifest
