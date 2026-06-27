# Copyright (c) 2026, TimesTX and contributors
# License: MIT. See LICENSE
"""S3 storage & bandwidth analytics APIs.

Production-ready, whitelisted endpoints consumed by the admin dashboard (and,
later, by billing). All AWS configuration is read dynamically from the
``S3 File Attachment`` DocType via the existing :class:`S3Operations` client —
nothing here hardcodes bucket, region, keys, prefix or endpoint.

Design notes:
    * The existing ``frappe_s3_attachment.controller.S3Operations`` is reused as
      the single S3 client factory (it reloads settings from the DocType on every
      instantiation), so we never duplicate client/config logic.
    * Storage usage is computed with a ``list_objects_v2`` paginator and lazy
      iteration — O(number_of_objects), constant memory — so it scales to buckets
      with millions of objects. Results are cached briefly so repeated dashboard
      refreshes don't re-scan the bucket or duplicate AWS calls.
    * Transfer (bandwidth) statistics use CloudWatch S3 request metrics
      (``BytesDownloaded`` / ``BytesUploaded``) — the AWS-supported way to get
      transfer volume without proxying every byte. If those metrics are not
      available the API degrades gracefully to zeros (never crashes).
    * Every entry point validates the connection first and, on failure, retries
      once after recreating the client and reloading configuration.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional, Tuple

import boto3
import frappe
from botocore.exceptions import (
	BotoCoreError,
	ClientError,
	EndpointConnectionError,
	NoCredentialsError,
)
from frappe import _
from frappe.utils import cint

from frappe_s3_attachment.controller import S3Operations

BYTES_PER_MB: int = 1024 * 1024
BYTES_PER_GB: int = 1024 * 1024 * 1024

# Short TTL: keeps the dashboard snappy and avoids re-scanning huge buckets on
# every refresh, while staying fresh enough for near-real-time cards.
STORAGE_CACHE_TTL: int = 300
_STORAGE_CACHE_PREFIX: str = "s3_storage_stats"


def _logger():
	return frappe.logger("s3_storage", allow_site=True, file_count=10)


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------
def bytes_to_mb(num_bytes: float | int | None) -> float:
	"""Convert bytes to mebibytes, rounded to 2 dp."""
	return round((num_bytes or 0) / BYTES_PER_MB, 2)


def bytes_to_gb(num_bytes: float | int | None) -> float:
	"""Convert bytes to gibibytes, rounded to 4 dp."""
	return round((num_bytes or 0) / BYTES_PER_GB, 4)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def _check_permission() -> None:
	"""Restrict analytics to admins (mirrors the Admin Dashboard workspace role)."""
	if frappe.session.user == "Administrator":
		return
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Not permitted to view S3 storage statistics."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Client + connection
# ---------------------------------------------------------------------------
def get_s3_client() -> S3Operations:
	"""Return a fresh :class:`S3Operations` (reloads settings from the DocType).

	Reuses the project's existing S3 client; never builds a parallel one.
	"""
	return S3Operations()


def validate_connection(s3op: S3Operations) -> Tuple[bool, str]:
	"""Cheaply verify the configured bucket is reachable via ``head_bucket``.

	Returns ``(ok, message)`` and maps common AWS failures to readable messages.
	"""
	try:
		s3op.S3_CLIENT.head_bucket(Bucket=s3op.BUCKET)
		return True, "Connected"
	except NoCredentialsError:
		return False, _("Invalid or missing AWS credentials.")
	except EndpointConnectionError:
		return False, _("S3 endpoint unavailable. Check region/network settings.")
	except ClientError as exc:
		code = str(exc.response.get("Error", {}).get("Code", "")) if getattr(exc, "response", None) else ""
		if code in ("404", "NoSuchBucket"):
			return False, _("Bucket '{0}' not found.").format(s3op.BUCKET)
		if code in ("403", "AccessDenied", "Forbidden"):
			return False, _("Access denied to the S3 bucket.")
		if code in ("401", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
			return False, _("Invalid AWS credentials.")
		return False, _("S3 error: {0}").format(code or "unknown")
	except (BotoCoreError, Exception) as exc:  # noqa: BLE001 - never crash the dashboard
		return False, _("Unable to connect to S3: {0}").format(str(exc))


def get_validated_s3(max_retries: int = 1) -> Tuple[Optional[S3Operations], str]:
	"""Build + validate an S3 client, retrying after a full client/config reload.

	On each attempt a brand-new :class:`S3Operations` is created (which reloads
	the ``S3 File Attachment`` settings), so a transient failure or a settings
	change is recovered on retry. Returns ``(s3op, message)`` or ``(None, error)``.
	"""
	last_message = _("Unable to connect to S3.")
	for attempt in range(max_retries + 1):
		try:
			s3op = get_s3_client()
			ok, message = validate_connection(s3op)
			if ok:
				return s3op, message
			last_message = message
		except Exception as exc:  # noqa: BLE001 - config/build errors are retried
			last_message = str(exc)
			_logger().error(f"S3 connect attempt {attempt + 1}/{max_retries + 1} failed: {exc}")
	return None, last_message


# ---------------------------------------------------------------------------
# Storage usage
# ---------------------------------------------------------------------------
def _normalized_prefix(s3op: S3Operations) -> str:
	prefix = (s3op.folder_name or "").strip().strip("/")
	return f"{prefix}/" if prefix else ""


def calculate_storage_usage(s3op: S3Operations, *, refresh: bool = False) -> dict[str, Any]:
	"""Compute storage occupied under the configured backup prefix.

	Uses a ``list_objects_v2`` paginator with lazy iteration so memory stays flat
	regardless of object count. Tracks total size, object count and the newest
	object's timestamp (``last_backup``). Cached for ``STORAGE_CACHE_TTL`` seconds.
	"""
	prefix = _normalized_prefix(s3op)
	cache = frappe.cache()
	cache_key = f"{_STORAGE_CACHE_PREFIX}::{s3op.BUCKET}::{prefix}"
	if not refresh:
		cached = cache.get_value(cache_key)
		if cached:
			return cached

	paginate_kwargs: dict[str, Any] = {"Bucket": s3op.BUCKET}
	if prefix:
		paginate_kwargs["Prefix"] = prefix

	paginator = s3op.S3_CLIENT.get_paginator("list_objects_v2")

	total_files = 0
	total_bytes = 0
	last_modified: Optional[datetime.datetime] = None
	for page in paginator.paginate(**paginate_kwargs):
		for obj in page.get("Contents", []) or []:
			total_files += 1
			total_bytes += int(obj.get("Size") or 0)
			modified = obj.get("LastModified")
			if modified and (last_modified is None or modified > last_modified):
				last_modified = modified

	result = {
		"bucket_name": s3op.BUCKET,
		"backup_prefix": prefix or (s3op.folder_name or ""),
		"total_files": total_files,
		"storage_bytes": total_bytes,
		"storage_mb": bytes_to_mb(total_bytes),
		"storage_gb": bytes_to_gb(total_bytes),
		"last_backup": last_modified.isoformat() if last_modified else None,
	}
	cache.set_value(cache_key, result, expires_in_sec=STORAGE_CACHE_TTL)
	return result


# ---------------------------------------------------------------------------
# Transfer (bandwidth) usage — CloudWatch S3 request metrics
# ---------------------------------------------------------------------------
def _current_month_window() -> Tuple[datetime.datetime, datetime.datetime]:
	now = datetime.datetime.utcnow()
	start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
	return start, now


def _cloudwatch_client(s3op: S3Operations):
	"""Build a CloudWatch client from the same dynamically-loaded S3 settings."""
	settings = s3op.s3_settings_doc
	kwargs: dict[str, Any] = {"region_name": settings.region_name}
	if settings.aws_key and settings.aws_secret:
		kwargs["aws_access_key_id"] = settings.aws_key
		kwargs["aws_secret_access_key"] = settings.aws_secret
	return boto3.client("cloudwatch", **kwargs)


def _sum_cloudwatch_metric(
	cw, bucket: str, metric_name: str, start: datetime.datetime, end: datetime.datetime
) -> float:
	"""Sum a single AWS/S3 request metric over the window; 0.0 if unavailable."""
	try:
		response = cw.get_metric_statistics(
			Namespace="AWS/S3",
			MetricName=metric_name,
			Dimensions=[
				{"Name": "BucketName", "Value": bucket},
				{"Name": "FilterId", "Value": "EntireBucket"},
			],
			StartTime=start,
			EndTime=end,
			Period=86400,
			Statistics=["Sum"],
			Unit="Bytes",
		)
		return float(sum(dp.get("Sum", 0) for dp in response.get("Datapoints", []) or []))
	except Exception as exc:  # noqa: BLE001 - request metrics may be disabled
		_logger().info(f"CloudWatch metric {metric_name} unavailable: {exc}")
		return 0.0


def calculate_transfer_usage(s3op: S3Operations, period: str = "Current Month") -> dict[str, Any]:
	"""Return upload/download bytes for the current month from CloudWatch.

	Requires S3 *request metrics* (``EntireBucket`` filter) to be enabled on the
	bucket; otherwise it returns zeros and flags ``source = "unavailable"`` so the
	caller/billing can decide how to treat it.
	"""
	start, end = _current_month_window()
	download_bytes = 0.0
	upload_bytes = 0.0
	source = "cloudwatch"
	try:
		cw = _cloudwatch_client(s3op)
		download_bytes = _sum_cloudwatch_metric(cw, s3op.BUCKET, "BytesDownloaded", start, end)
		upload_bytes = _sum_cloudwatch_metric(cw, s3op.BUCKET, "BytesUploaded", start, end)
	except Exception as exc:  # noqa: BLE001 - never crash on metrics
		_logger().info(f"CloudWatch transfer stats unavailable: {exc}")
		source = "unavailable"

	return {
		"download_bytes": int(download_bytes),
		"upload_bytes": int(upload_bytes),
		"download_mb": bytes_to_mb(download_bytes),
		"upload_mb": bytes_to_mb(upload_bytes),
		"period": period,
		"source": source,
	}


# ---------------------------------------------------------------------------
# Period-aware statistics (month / year filtered) — for the dashboard cards
# ---------------------------------------------------------------------------
def _period_window(
	year: int | str | None, month: int | str | None
) -> Tuple[datetime.datetime, datetime.datetime, str, bool]:
	"""Return ``(start, end, label, is_current)`` for the selected month/year.

	``month`` empty/None means the whole year. ``end`` is capped at "now" so we
	never ask CloudWatch for the future. ``is_current`` flags that the window
	contains today (used to allow a live listing fallback for storage/files).
	"""
	now = datetime.datetime.utcnow()
	year_int = cint(year) or now.year
	month_int = cint(month)

	if 1 <= month_int <= 12:
		start = datetime.datetime(year_int, month_int, 1)
		nxt = (
			datetime.datetime(year_int + 1, 1, 1)
			if month_int == 12
			else datetime.datetime(year_int, month_int + 1, 1)
		)
		label = start.strftime("%B %Y")
	else:
		start = datetime.datetime(year_int, 1, 1)
		nxt = datetime.datetime(year_int + 1, 1, 1)
		label = str(year_int)

	is_current = start <= now < nxt
	end = min(nxt, now) if start <= now else nxt
	if end <= start:
		end = start + datetime.timedelta(days=1)
	return start, end, label, is_current


def _latest_cloudwatch_metric(
	cw,
	bucket: str,
	metric_name: str,
	storage_type: str,
	start: datetime.datetime,
	end: datetime.datetime,
) -> float:
	"""Most recent daily datapoint of a free AWS/S3 storage metric in the window.

	``BucketSizeBytes`` / ``NumberOfObjects`` are point-in-time daily values, so
	for a period we report the latest reading inside it. Returns 0.0 if the metric
	has no datapoints (new bucket, latency, or metric disabled).
	"""
	try:
		response = cw.get_metric_statistics(
			Namespace="AWS/S3",
			MetricName=metric_name,
			Dimensions=[
				{"Name": "BucketName", "Value": bucket},
				{"Name": "StorageType", "Value": storage_type},
			],
			StartTime=start,
			EndTime=end,
			Period=86400,
			Statistics=["Average"],
		)
		datapoints = response.get("Datapoints", []) or []
		if not datapoints:
			return 0.0
		datapoints.sort(key=lambda dp: dp.get("Timestamp"))
		return float(datapoints[-1].get("Average", 0.0))
	except Exception as exc:  # noqa: BLE001 - metric may be unavailable
		_logger().info(f"CloudWatch metric {metric_name} unavailable: {exc}")
		return 0.0


def calculate_period_usage(
	s3op: S3Operations,
	start: datetime.datetime,
	end: datetime.datetime,
	label: str,
	is_current: bool,
) -> dict[str, Any]:
	"""Collect upload/download/storage/files for the period from CloudWatch.

	Transfer (``BytesUploaded`` / ``BytesDownloaded``) is summed over the window;
	storage (``BucketSizeBytes``) and object count (``NumberOfObjects``) take the
	latest in-window reading. For the *current* period, if CloudWatch has no
	storage snapshot yet, fall back to a live ``list_objects_v2`` scan of the
	configured backup prefix so the cards are never blank on day one.
	"""
	upload_bytes = 0.0
	download_bytes = 0.0
	storage_bytes = 0.0
	total_files = 0.0
	transfer_source = "cloudwatch"
	storage_source = "cloudwatch"

	try:
		cw = _cloudwatch_client(s3op)
		upload_bytes = _sum_cloudwatch_metric(cw, s3op.BUCKET, "BytesUploaded", start, end)
		download_bytes = _sum_cloudwatch_metric(cw, s3op.BUCKET, "BytesDownloaded", start, end)
		storage_bytes = _latest_cloudwatch_metric(cw, s3op.BUCKET, "BucketSizeBytes", "StandardStorage", start, end)
		total_files = _latest_cloudwatch_metric(cw, s3op.BUCKET, "NumberOfObjects", "AllStorageTypes", start, end)
	except Exception as exc:  # noqa: BLE001
		_logger().info(f"CloudWatch period stats unavailable: {exc}")
		transfer_source = "unavailable"
		storage_source = "unavailable"

	if not upload_bytes and not download_bytes:
		transfer_source = "unavailable"

	# Live fallback for the current period (per backup prefix) when CloudWatch has
	# no storage snapshot. Historical months cannot be reconstructed this way.
	if (not storage_bytes and not total_files) and is_current:
		try:
			snapshot = calculate_storage_usage(s3op)
			storage_bytes = snapshot["storage_bytes"]
			total_files = snapshot["total_files"]
			storage_source = "listing"
		except Exception as exc:  # noqa: BLE001
			_logger().info(f"Storage listing fallback failed: {exc}")

	return {
		"bucket_name": s3op.BUCKET,
		"backup_prefix": _normalized_prefix(s3op) or (s3op.folder_name or ""),
		"upload_bytes": int(upload_bytes),
		"upload_mb": bytes_to_mb(upload_bytes),
		"download_bytes": int(download_bytes),
		"download_mb": bytes_to_mb(download_bytes),
		"storage_bytes": int(storage_bytes),
		"storage_mb": bytes_to_mb(storage_bytes),
		"storage_gb": bytes_to_gb(storage_bytes),
		"total_files": int(total_files),
		"period": label,
		"transfer_source": transfer_source,
		"storage_source": storage_source,
		"connection": {"status": True, "message": "Connected"},
	}


def collect_period_statistics(
	year: int | str | None = None, month: int | str | None = None
) -> dict[str, Any]:
	"""Period statistics with connection handling — callable in-process.

	No permission gate here: callers (e.g. the Pricing Dashboard) enforce their
	own access rules. Use :func:`get_period_statistics` for the whitelisted entry.
	"""
	start, end, label, is_current = _period_window(year, month)
	s3op, message = get_validated_s3()
	if not s3op:
		return {
			"bucket_name": None,
			"backup_prefix": None,
			"upload_bytes": 0,
			"upload_mb": 0,
			"download_bytes": 0,
			"download_mb": 0,
			"storage_bytes": 0,
			"storage_mb": 0,
			"storage_gb": 0,
			"total_files": 0,
			"period": label,
			"transfer_source": "unavailable",
			"storage_source": "unavailable",
			"connection": {"status": False, "message": message},
		}
	return calculate_period_usage(s3op, start, end, label, is_current)


# ---------------------------------------------------------------------------
# Whitelisted APIs
# ---------------------------------------------------------------------------
def _empty_storage(status: str, message: str) -> dict[str, Any]:
	return {
		"bucket_name": None,
		"backup_prefix": None,
		"total_files": 0,
		"storage_bytes": 0,
		"storage_mb": 0,
		"storage_gb": 0,
		"last_backup": None,
		"status": status,
		"message": message,
	}


@frappe.whitelist()
def get_storage_statistics(refresh: int | str = 0) -> dict[str, Any]:
	"""API 1 — actual storage occupied under the configured backup prefix."""
	_check_permission()
	s3op, message = get_validated_s3()
	if not s3op:
		return _empty_storage("Disconnected", message)
	try:
		stats = calculate_storage_usage(s3op, refresh=bool(cint(refresh)))
	except Exception as exc:  # noqa: BLE001
		_logger().error(f"get_storage_statistics failed: {frappe.get_traceback()}")
		return _empty_storage("Error", str(exc))
	stats["status"] = "Connected"
	return stats


@frappe.whitelist()
def get_transfer_statistics() -> dict[str, Any]:
	"""API 2 — current-month upload/download volume from CloudWatch."""
	_check_permission()
	s3op, message = get_validated_s3()
	if not s3op:
		return {
			"download_mb": 0,
			"upload_mb": 0,
			"download_bytes": 0,
			"upload_bytes": 0,
			"period": "Current Month",
			"source": "unavailable",
			"status": "Disconnected",
			"message": message,
		}
	stats = calculate_transfer_usage(s3op)
	stats["status"] = "Connected"
	return stats


@frappe.whitelist()
def get_period_statistics(year: int | str | None = None, month: int | str | None = None) -> dict[str, Any]:
	"""Month/year-filtered usage (upload, download, storage, files)."""
	_check_permission()
	return collect_period_statistics(year, month)


@frappe.whitelist()
def get_dashboard_statistics(refresh: int | str = 0) -> dict[str, Any]:
	"""API 3 — lightweight, combined payload for the dashboard cards.

	Validates the connection once and reuses the same client for both storage and
	transfer, avoiding duplicate AWS client creation / calls.
	"""
	_check_permission()
	s3op, message = get_validated_s3()
	if not s3op:
		return {
			"storage": {"bytes": 0, "mb": 0, "gb": 0},
			"transfer": {"upload_mb": 0, "download_mb": 0},
			"files": {"count": 0},
			"connection": {"status": False, "message": message},
		}

	try:
		storage = calculate_storage_usage(s3op, refresh=bool(cint(refresh)))
	except Exception as exc:  # noqa: BLE001
		_logger().error(f"get_dashboard_statistics storage failed: {frappe.get_traceback()}")
		return {
			"storage": {"bytes": 0, "mb": 0, "gb": 0},
			"transfer": {"upload_mb": 0, "download_mb": 0},
			"files": {"count": 0},
			"connection": {"status": False, "message": str(exc)},
		}

	transfer = calculate_transfer_usage(s3op)

	return {
		"bucket_name": storage.get("bucket_name"),
		"backup_prefix": storage.get("backup_prefix"),
		"last_backup": storage.get("last_backup"),
		"storage": {
			"bytes": storage["storage_bytes"],
			"mb": storage["storage_mb"],
			"gb": storage["storage_gb"],
		},
		"transfer": {
			"upload_mb": transfer["upload_mb"],
			"download_mb": transfer["download_mb"],
			"source": transfer["source"],
		},
		"files": {"count": storage["total_files"]},
		"connection": {"status": True, "message": "Connected"},
	}
