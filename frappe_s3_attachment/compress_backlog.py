#!/usr/bin/env python3
"""Phase 2: shrink the PDF backlog already sitting in the bucket.

Standalone — no bench, no frappe. Run it from a droplet after cutover, while
the old AWS bucket is still intact as a rollback.

It writes **zero database rows**. Keys, extensions and formats are untouched,
so every existing file_url keeps working; only the object body changes, and a
PutObject to an existing key is atomic, so a colleague opening a file mid-run
gets either the old object or the new one.

    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
    export AWS_ENDPOINT_URL_S3=https://blr1.digitaloceanspaces.com
    python3 compress_backlog.py --bucket werkiq-assets --prefix times.werkiq/ --dry-run
    python3 compress_backlog.py --bucket werkiq-assets --prefix times.werkiq/ --limit 500
    python3 compress_backlog.py --bucket werkiq-assets --prefix times.werkiq/

Interrupt it freely: processed keys are appended to --state and skipped on the
next run, and objects already carrying the compressed=1 marker are skipped even
if the state file is lost.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import threading

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_compress import MIN_BYTES, SkipReason, compress_pdf_verbose, tools_available  # noqa: E402

MARKER = "compressed"


def s3_client():
    # endpoint + credentials come from the environment; AWS_ENDPOINT_URL_S3 is
    # what points this at Spaces instead of AWS.
    return boto3.client("s3", config=Config(signature_version="s3v4"))


def iter_pdf_keys(client, bucket, prefix, min_bytes):
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Size"] >= min_bytes and obj["Key"].lower().endswith(".pdf"):
                yield obj["Key"], obj["Size"]


def process_one(client, bucket, key, dry_run, min_bytes):
    """Returns (status, bytes_saved, bytes_read)."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError:
        return "head_failed", 0, 0
    meta = head.get("Metadata") or {}
    if meta.get(MARKER) == "1":
        return "already_done", 0, 0

    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError:
        return "get_failed", 0, 0

    smaller, reason = compress_pdf_verbose(body, min_bytes=min_bytes)
    if not smaller:
        # Mark hopeless objects so the next run does not re-download them.
        if reason in (SkipReason.SIGNED, SkipReason.NOT_SMALLER) and not dry_run:
            _mark_only(client, bucket, key, head)
        return reason, 0, len(body)

    saved = len(body) - len(smaller)
    if dry_run:
        return "would_compress", saved, len(body)

    new_meta = dict(meta)
    new_meta[MARKER] = "1"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=smaller,
        ContentType=head.get("ContentType") or "application/pdf",
        Metadata=new_meta,
    )
    return "compressed", saved, len(body)


def _mark_only(client, bucket, key, head):
    """Stamp the marker so we never download this object again.

    Server-side copy onto itself — no re-upload of the body.
    """
    new_meta = dict(head.get("Metadata") or {})
    new_meta[MARKER] = "1"
    try:
        client.copy_object(
            Bucket=bucket,
            Key=key,
            CopySource={"Bucket": bucket, "Key": key},
            ContentType=head.get("ContentType") or "application/pdf",
            Metadata=new_meta,
            MetadataDirective="REPLACE",
        )
    except ClientError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--state", default="compress_backlog.done",
                    help="keys already processed; appended to as we go")
    ap.add_argument("--min-bytes", type=int, default=MIN_BYTES,
                    help=f"size floor (default {MIN_BYTES} = 5 MiB)")
    ap.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2)))
    ap.add_argument("--limit", type=int, default=0, help="stop after N objects")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not tools_available():
        sys.exit("ghostscript and poppler-utils (pdfinfo) are required — apt install ghostscript poppler-utils")

    done = set()
    if os.path.exists(args.state):
        with open(args.state) as fh:
            done = {line.strip() for line in fh if line.strip()}
    print(f"{len(done):,} keys already recorded in {args.state}")

    client = s3_client()
    lock = threading.Lock()
    stats = {}
    saved_total = 0
    read_total = 0
    state_fh = None if args.dry_run else open(args.state, "a")

    def work(key):
        nonlocal saved_total, read_total
        status, saved, read = process_one(client, args.bucket, key, args.dry_run, args.min_bytes)
        with lock:
            stats[status] = stats.get(status, 0) + 1
            saved_total += saved
            read_total += read
            # Transient S3 errors must stay retryable — only record terminal outcomes.
            if state_fh and status not in ("head_failed", "get_failed"):
                state_fh.write(key + "\n")
                state_fh.flush()
            n = sum(stats.values())
            if n % 50 == 0:
                print(f"  {n:,} processed, {saved_total / 2**30:.2f} GiB saved", flush=True)

    keys = []
    for key, _size in iter_pdf_keys(client, args.bucket, args.prefix, args.min_bytes):
        if key in done:
            continue
        keys.append(key)
        if args.limit and len(keys) >= args.limit:
            break
    print(f"{len(keys):,} candidate objects ≥ {args.min_bytes / 2**20:.0f} MiB")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, keys))
    except KeyboardInterrupt:
        print("\ninterrupted — rerun the same command to resume")
    finally:
        if state_fh:
            state_fh.close()

    pct = (100 * saved_total / read_total) if read_total else 0
    print(
        f"\n{'DRY RUN — ' if args.dry_run else ''}saved {saved_total / 2**30:.2f} GiB"
        f" of {read_total / 2**30:.2f} GiB examined ({pct:.1f}%)"
    )
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {v:>7,}  {k}")


if __name__ == "__main__":
    main()
