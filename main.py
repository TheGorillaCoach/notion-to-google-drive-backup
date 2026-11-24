#!/usr/bin/env python3
"""Notion → Google Drive incremental Markdown backup.

Examples
  python main.py --dry-run            # export changed pages to ./dist only
  python main.py                      # export changed pages, then upload to Drive
  python main.py --dry-run --limit 25 # quick test on 25 pages
  python main.py --upload-only        # finish an interrupted upload, no export
  python main.py --full               # re-render every page (e.g. after an upgrade)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from notion_backup.config import ConfigError, load_config
from notion_backup.notion_exporter import MANIFEST_NAME, Manifest, NotionExporter

log = logging.getLogger("backup")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Back up Notion to Google Drive as Markdown.")
    p.add_argument("--dry-run", action="store_true",
                   help="export to the local output folder only; never touch Google Drive")
    p.add_argument("--full", action="store_true",
                   help="re-export every page, not just ones edited since the last run")
    p.add_argument("--limit", type=int, metavar="N",
                   help="export at most N pages this run (for testing)")
    p.add_argument("--time-budget", type=float, metavar="MINUTES",
                   help="stop starting new page exports after this many minutes; "
                        "the rest are picked up next run")
    p.add_argument("--upload-only", action="store_true",
                   help="skip the Notion export and just sync pending files to Drive")
    p.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = p.parse_args(argv)
    if args.dry_run and args.upload_only:
        p.error("--dry-run and --upload-only cannot be combined")
    return args


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def write_ci_summary(lines: list[str]) -> None:
    """Show the run summary on the GitHub Actions run page."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    started = time.monotonic()

    try:
        config = load_config(args.env_file)
        uploader = None
        if not args.dry_run:
            from notion_backup.drive_uploader import DriveUploader  # only needed for uploads
            uploader = DriveUploader(config)
            uploader.pull_manifest()
    except ConfigError as e:
        log.error("Configuration problem: %s", e)
        return 2

    summary = ["## Notion backup", ""]
    exit_code = 0

    if not args.upload_only:
        budget = args.time_budget * 60 if args.time_budget else None
        result = NotionExporter(config).run(full=args.full, limit=args.limit,
                                            time_budget_s=budget)
        log.info("Export: %d pages, %d databases | %d files written, %d unchanged, "
                 "%d moved, %d removed from Notion, %d failed, %d left for next run "
                 "| %d Notion requests in %.0fs",
                 result.total_pages, result.total_databases, len(result.written),
                 result.unchanged, len(result.moved), len(result.removed),
                 len(result.failed), result.remaining, result.requests, result.seconds)
        for title, err in result.failed[:20]:
            log.warning("  failed: %s — %s", title, err)
        summary += [
            f"- **Pages:** {result.total_pages} · **Databases:** {result.total_databases}",
            f"- **Exported (changed):** {len(result.written)} · **Unchanged:** {result.unchanged}",
            f"- **Moved/renamed:** {len(result.moved)} · **Removed from Notion:** {len(result.removed)}",
            f"- **Failed:** {len(result.failed)} · **Left for next run:** {result.remaining}",
        ]
        # A handful of failures retry automatically next run; many means trouble.
        if result.failed and len(result.failed) > max(10, result.total_pages // 50):
            exit_code = 1

    if uploader:
        stats = uploader.sync(Manifest(config.output_dir / MANIFEST_NAME))
        log.info("Drive: %d created, %d updated, %d moved, %d failed (%d Drive calls)",
                 stats["created"], stats["updated"], stats["moved"], stats["failed"],
                 uploader.calls)
        if stats["missing_local"]:
            log.warning("%d changed files were not on disk; run with --full to rebuild them.",
                        stats["missing_local"])
        summary.append(f"- **Drive:** {stats['created']} created, {stats['updated']} updated, "
                       f"{stats['moved']} moved, {stats['failed']} failed")
        if stats["failed"]:
            exit_code = 1
    else:
        log.info("Dry run: nothing uploaded. Output is in %s", config.output_dir.resolve())
        summary.append("- **Dry run:** nothing uploaded")

    minutes = (time.monotonic() - started) / 60
    log.info("Done in %.1f minutes.", minutes)
    summary.append(f"- **Duration:** {minutes:.1f} min")
    write_ci_summary(summary)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
