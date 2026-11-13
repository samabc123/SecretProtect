"""Public-channel and authorized-artifact connectors for the unified backend.

The connectors in this module deliberately return the existing masked event
contract instead of introducing a second content-item pipeline. Network access
is opt-in at the caller, and tests can inject a transport so no real request is
needed to verify parsing, budgets, and failure handling.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import re
import struct
import tarfile
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .content_detection import detect_content
from .public_http import HttpResult, PublicConnectorError, UrllibPublicTransport, _validate_public_url
from .time_provenance import merge_time_provenance, time_entry
from .time_utils import timestamp_precision


class ConnectorEvents(list):
    """List-compatible connector result with a safe scanned-item counter."""

    def __init__(
        self,
        *items,
        items_seen: int = 0,
        content_items: list[dict[str, Any]] | None = None,
    ):
        super().__init__(*items)
        self.items_seen = items_seen
        self.content_items = list(content_items or [])


def _json(result: HttpResult, code: str) -> Any:
    if result.status != 200:
        raise PublicConnectorError(code)
    try:
        return json.loads(result.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PublicConnectorError(f"{code}_invalid_json") from error


def _request_json(
    transport: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    code: str,
    method: str = "GET",
) -> tuple[Any, HttpResult]:
    result = transport.request(url, headers=headers, method=method)
    return _json(result, code), result


def _parse_timestamp(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_weibo_timestamp(value: Any) -> str | None:
    """Parse the English RFC-style timestamp returned by the Weibo API."""
    parsed = _parse_timestamp(value)
    if parsed:
        return parsed
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed_dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None
    return parsed_dt.astimezone(timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _published_time_metadata(source: str, confidence: str, value: str | None) -> dict[str, Any]:
    source = str(source or "").strip()
    confidence = str(confidence or "none").strip() or "none"
    low_reference_sources = {
        "mediawiki_revision_timestamp",
        "github_commit",
        "gitee_commit",
        "stackoverflow_creation_date",
        "stackoverflow_answer_creation_date",
        "sitemap_lastmod",
        "weibo_page_metadata",
        "image_config_created",
        "pypi_upload_time",
    }
    if value and source:
        eligible = confidence in {"high", "medium"} and source not in low_reference_sources
        return {
            "meaning": (
                "来源公开或内容发布时间，可证明内容在该时间已公开；"
                "不等于凭据首次泄露时间。"
            ),
            "role": "public_content_time" if eligible else "content_revision_reference",
            "eligible_for_latency": eligible,
            "unknown_reason": "",
        }
    return {
        "meaning": "来源没有提供可验证的公开或内容时间。",
        "role": "unknown",
        "eligible_for_latency": False,
        "unknown_reason": "connector_not_provided",
    }


def _text_from_html(value: str) -> str:
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", "\n", value)
    value = html.unescape(value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _event_records(
    text: str,
    *,
    source_kind: str,
    channel: str,
    platform: str,
    locator: str,
    hmac_key: str,
    published_at: str | None = None,
    published_source: str = "",
    published_confidence: str = "none",
    published_meaning: str = "",
    published_evidence: str = "",
    published_role: str = "",
    published_eligible_for_latency: bool | None = None,
    published_unknown_reason: str = "",
    source_updated_at: str | None = None,
    source_updated_source: str = "",
    source_updated_confidence: str = "none",
    source_updated_meaning: str = "",
    source_updated_evidence: str = "",
    version_id: str | None = None,
    version_kind: str = "none",
    content_path: str | None = None,
    fetched_at: str | None = None,
    first_observed_at: str | None = None,
    detected_at: str | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    # The body has already been obtained by the caller. Capture acquisition
    # completion before detection starts; detection completion is measured by
    # detect_content after the rules have actually run.
    fetched_at = fetched_at or _now()
    first_observed_at = first_observed_at or fetched_at
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    records = detect_content(
        text,
        source_kind=source_kind,
        channel=channel,
        platform=platform,
        source_locator=locator,
        hmac_key=hmac_key,
        first_observed_at=first_observed_at,
        detected_at=detected_at,
        include_internal=True,
        enabled_rule_ids=enabled_rule_ids,
    )
    published_metadata = _published_time_metadata(published_source, published_confidence, published_at)
    published_meaning = published_meaning or published_metadata["meaning"]
    published_role = published_role or published_metadata["role"]
    if published_eligible_for_latency is None:
        published_eligible_for_latency = bool(published_metadata["eligible_for_latency"])
    published_unknown_reason = published_unknown_reason or published_metadata["unknown_reason"]
    for record in records:
        record["_published_at"] = published_at
        record["_published_source"] = published_source
        record["_published_confidence"] = published_confidence
        record["_published_evidence"] = published_evidence
        record["_published_meaning"] = published_meaning
        record["_published_role"] = published_role
        record["_published_eligible_for_latency"] = bool(published_eligible_for_latency)
        record["_published_unknown_reason"] = published_unknown_reason
        record["_source_updated_at"] = source_updated_at
        record["_source_updated_source"] = source_updated_source
        record["_source_updated_confidence"] = source_updated_confidence
        record["_source_updated_meaning"] = source_updated_meaning
        record["_source_updated_evidence"] = source_updated_evidence
        record["_version_id"] = version_id or content_hash
        record["_version_kind"] = version_kind
        record["_content_hash"] = content_hash
        record["_content_path"] = content_path
        record["_fetched_at"] = fetched_at
        record["published_at"] = published_at
        record["published_source"] = published_source
        record["published_confidence"] = published_confidence
        record["published_meaning"] = published_meaning
        record["published_evidence"] = published_evidence
        record["public_time_status"] = (
            "known"
            if published_at and bool(published_eligible_for_latency)
            else "reference_only" if published_at else "unknown"
        )
        record["public_time_unknown_reason"] = published_unknown_reason
        record["source_updated_at"] = source_updated_at
        record["source_updated_source"] = source_updated_source
        record["source_updated_confidence"] = source_updated_confidence
        record["source_updated_meaning"] = source_updated_meaning
        record["source_updated_evidence"] = source_updated_evidence
        record["time_provenance"] = merge_time_provenance(
            record.get("time_provenance"),
            {
                "published_at": time_entry(
                    published_at,
                    source=published_source or "not_provided",
                    meaning=published_meaning,
                    confidence=published_confidence,
                    evidence=published_evidence,
                    role=published_role,
                    eligible_for_latency=bool(published_eligible_for_latency),
                    unknown_reason=published_unknown_reason,
                    precision=timestamp_precision(published_at),
                    evidence_scope=locator,
                    content_version=version_id or content_hash,
                ),
                "fetched_at": time_entry(
                    fetched_at,
                    source="connector_content_read",
                    meaning="连接器完成读取当前内容的时间。",
                    confidence="high",
                    evidence=locator,
                    role="acquisition_completion",
                    eligible_for_latency=True,
                    precision="millisecond",
                    evidence_scope=locator,
                    content_version=version_id or content_hash,
                ),
                **(
                    {
                        "source_updated_at": time_entry(
                            source_updated_at,
                            source=source_updated_source or "not_provided",
                            meaning=source_updated_meaning or "来源未提供内容版本更新时间。",
                            confidence=source_updated_confidence,
                            evidence=source_updated_evidence,
                            role="content_revision_reference",
                        )
                    }
                    if source_updated_at or source_updated_source
                    else {}
                ),
            },
        )
    return records


def _content_item(
    text: str,
    *,
    platform: str,
    channel: str,
    locator: str,
    origin_url: str,
    published_at: str | None,
    published_source: str,
    published_confidence: str,
    published_evidence: str,
    fetched_at: str | None = None,
    version_id: str | None = None,
    version_kind: str = "content_hash",
    content_path: str | None = None,
) -> dict[str, Any]:
    """Return safe source-version metadata without retaining fetched content."""
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fetched_at = fetched_at or _now()
    metadata = _published_time_metadata(published_source, published_confidence, published_at)
    return {
        "platform": platform,
        "channel": channel,
        "origin_url": origin_url or locator,
        "path": content_path or locator,
        "version_id": version_id or content_hash,
        "version_kind": version_kind,
        "content_hash": content_hash,
        "size": len(text.encode("utf-8")),
        "published_at": published_at,
        "published_source": published_source,
        "published_confidence": published_confidence,
        "published_evidence": published_evidence,
        "published_meaning": metadata["meaning"],
        "published_role": metadata["role"],
        "published_eligible_for_latency": bool(metadata["eligible_for_latency"]),
        "published_unknown_reason": metadata["unknown_reason"],
        "fetched_at": fetched_at,
        "time_provenance": merge_time_provenance(
            {},
            {
                "published_at": time_entry(
                    published_at,
                    source=published_source or "not_provided",
                    meaning=metadata["meaning"],
                    confidence=published_confidence,
                    evidence=published_evidence,
                    role=metadata["role"],
                    eligible_for_latency=bool(metadata["eligible_for_latency"]),
                    unknown_reason=metadata["unknown_reason"],
                    precision=timestamp_precision(published_at),
                    evidence_scope=origin_url or locator,
                    content_version=version_id or content_hash,
                ),
                "fetched_at": time_entry(
                    fetched_at,
                    source="connector_content_read",
                    meaning="连接器完成读取当前内容的时间。",
                    confidence="high",
                    evidence=locator,
                    role="acquisition_completion",
                    eligible_for_latency=True,
                    precision="millisecond",
                    evidence_scope=origin_url or locator,
                    content_version=version_id or content_hash,
                ),
            },
        ),
    }


def _rate_limited(result: HttpResult) -> bool:
    return result.status in {403, 429, 503} or result.headers.get("x-ratelimit-remaining") == "0"


def _check_budget(config: dict[str, Any], key: str, default: int, low: int = 1, high: int = 500) -> int:
    try:
        value = int(config.get(key) or default)
    except (TypeError, ValueError) as error:
        raise PublicConnectorError(f"{key}_invalid") from error
    if value < low or value > high:
        raise PublicConnectorError(f"{key}_out_of_range")
    return value


def collect_mediawiki(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    transport: Any | None = None,
    checkpoint_store: Any | None = None,
    defer_checkpoints: bool = False,
    pending_checkpoints: list[tuple[str, str]] | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    api_url = str(config.get("api_url") or "").strip().rstrip("/")
    _validate_public_url(api_url)
    max_items = _check_budget(config, "max_items", 50, 1, 200)
    transport = transport or UrllibPublicTransport(
        timeout_seconds,
        allowed_origins=(api_url,),
        max_bytes=2 * 1024 * 1024,
    )
    target = f"mediawiki:{api_url}"
    continue_value = None
    if checkpoint_store is not None:
        raw = checkpoint_store.get_checkpoint(target)
        if raw:
            try:
                continue_value = json.loads(raw).get("rccontinue")
            except (TypeError, json.JSONDecodeError):
                continue_value = raw
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcprop": "title|ids|timestamp",
        "rcnamespace": str(config.get("namespaces") or "0"),
        "rclimit": str(min(50, max_items)),
        "format": "json",
        "formatversion": "2",
    }
    if continue_value:
        params["rccontinue"] = continue_value
    url = f"{api_url}?{urllib.parse.urlencode(params)}"
    data, response = _request_json(
        transport,
        url,
        headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="mediawiki_request_failed",
    )
    if _rate_limited(response):
        return []
    changes = (data.get("query") or {}).get("recentchanges") if isinstance(data, dict) else None
    if not isinstance(changes, list):
        raise PublicConnectorError("mediawiki_invalid_response")
    pages: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict) or change.get("type") not in {"edit", "new"}:
            continue
        pageid = change.get("pageid")
        if pageid is None:
            continue
        pages.append(change)
        if len(pages) >= max_items:
            break
    next_cursor = (data.get("continue") or {}).get("rccontinue") if isinstance(data, dict) else None
    if checkpoint_store is not None and next_cursor:
        value = json.dumps({"rccontinue": next_cursor}, ensure_ascii=False)
        if defer_checkpoints and pending_checkpoints is not None:
            pending_checkpoints.append((target, value))
        else:
            checkpoint_store.set_checkpoint(target, value)
    if not pages:
        return []
    pageids = "|".join(str(item["pageid"]) for item in pages)
    revision_params = {
        "action": "query",
        "prop": "revisions",
        "pageids": pageids,
        "rvprop": "content|timestamp",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2",
    }
    revisions, revision_response = _request_json(
        transport,
        f"{api_url}?{urllib.parse.urlencode(revision_params)}",
        headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="mediawiki_revision_request_failed",
    )
    if _rate_limited(revision_response):
        return []
    page_rows = (revisions.get("query") or {}).get("pages") if isinstance(revisions, dict) else None
    if not isinstance(page_rows, list):
        raise PublicConnectorError("mediawiki_invalid_revision_response")
    by_page = {str(item.get("pageid")): item for item in page_rows if isinstance(item, dict)}
    host = urllib.parse.urlparse(api_url).netloc
    results: list[dict[str, Any]] = []
    for change in pages:
        page = by_page.get(str(change["pageid"])) or {}
        revision = (page.get("revisions") or [{}])[0]
        slots = revision.get("slots") or {}
        main = slots.get("main") or {}
        content = main.get("content") or revision.get("*") or revision.get("content") or ""
        if not isinstance(content, str) or not content:
            continue
        locator = f"mediawiki:{host}:page:{change['pageid']}"
        results.extend(
            _event_records(
                content,
                source_kind="真实检测结果",
                channel="Wiki",
                platform="MediaWiki",
                locator=locator,
                hmac_key=hmac_key,
                published_at=_parse_timestamp(revision.get("timestamp") or change.get("timestamp")),
                published_source="mediawiki_revision_timestamp",
                published_confidence="medium",
                published_evidence=f"{api_url}#page={change['pageid']}",
                published_meaning=(
                    "MediaWiki 匹配页面的修订时间，仅作为内容版本参考；"
                    "不等于凭据首次公开或首次泄露时间。"
                ),
                published_role="content_revision_reference",
                published_eligible_for_latency=False,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
    return results


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_child(element: ElementTree.Element, names: set[str]) -> str:
    for child in list(element):
        if _xml_local(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def _xml_link(element: ElementTree.Element) -> str:
    for child in list(element):
        if _xml_local(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
            if child.text:
                return child.text.strip()
    return ""


def collect_generic_web(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    feed_url = str(config.get("feed_url") or "").strip()
    sitemap_url = str(config.get("sitemap_url") or "").strip()
    single_url = str(config.get("url") or "").strip()
    targets = [value for value in (feed_url, sitemap_url, single_url) if value]
    if not targets:
        raise PublicConnectorError("generic_web_target_required")
    for target in targets:
        _validate_public_url(target)
    max_items = _check_budget(config, "max_items", 50, 1, 200)
    transport = transport or UrllibPublicTransport(
        timeout_seconds,
        allowed_origins=tuple(targets),
        max_bytes=max_file_bytes,
    )
    results: list[dict[str, Any]] = []
    if feed_url:
        response = transport.request(feed_url, headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"})
        if _rate_limited(response):
            return []
        if response.status != 200:
            raise PublicConnectorError("feed_request_failed")
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as error:
            raise PublicConnectorError("feed_invalid_xml") from error
        entries = [
            item for item in root.iter()
            if _xml_local(item.tag) in {"item", "entry"}
        ][:max_items]
        host = urllib.parse.urlparse(feed_url).netloc
        for index, item in enumerate(entries):
            title = _xml_child(item, {"title"})
            link = _xml_link(item) or feed_url
            body = _xml_child(item, {"encoded", "content", "summary", "description"})
            text = _text_from_html(f"{title}\n{body}".strip())
            if not text:
                continue
            published = _parse_timestamp(
                _xml_child(item, {"published", "pubdate", "date", "updated"})
            )
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="知识分享",
                    platform=host or "RSS/Atom",
                    locator=f"rss:{host}:{hashlib.sha256((link or str(index)).encode()).hexdigest()[:16]}",
                    hmac_key=hmac_key,
                    published_at=published,
                    published_source="rss_published" if published else "",
                    published_confidence="medium" if published else "none",
                    published_evidence=link if published else "",
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
    if sitemap_url:
        response = transport.request(sitemap_url, headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"})
        if _rate_limited(response):
            return results
        if response.status != 200:
            raise PublicConnectorError("sitemap_request_failed")
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as error:
            raise PublicConnectorError("sitemap_invalid_xml") from error
        urls: list[tuple[str, str | None]] = []
        for item in root.iter():
            if _xml_local(item.tag) != "url":
                continue
            loc = _xml_child(item, {"loc"})
            lastmod = _xml_child(item, {"lastmod"})
            if loc:
                _validate_public_url(loc)
                urls.append((loc, lastmod or None))
        for loc, lastmod in urls[:max_items]:
            page = transport.request(loc, headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"})
            if page.status != 200 or len(page.body) > max_file_bytes:
                continue
            text = _text_from_html(page.body.decode("utf-8", errors="replace"))
            if not text:
                continue
            host = urllib.parse.urlparse(loc).netloc
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="知识分享",
                    platform=host or "Sitemap",
                    locator=f"url:{loc}",
                    hmac_key=hmac_key,
                    published_at=_parse_timestamp(lastmod),
                    published_source="sitemap_lastmod" if lastmod else "",
                    published_confidence="low" if lastmod else "none",
                    published_evidence=sitemap_url if lastmod else "",
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
    if single_url:
        response = transport.request(single_url, headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"})
        if response.status == 200 and len(response.body) <= max_file_bytes:
            text = _text_from_html(response.body.decode("utf-8", errors="replace"))
            host = urllib.parse.urlparse(single_url).netloc
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="知识分享",
                    platform=host or "Web",
                    locator=f"url:{single_url}",
                    hmac_key=hmac_key,
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
    return results


def collect_stackoverflow(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    keyword = str(config.get("q") or config.get("keyword") or "").strip()
    raw_queries = config.get("queries")
    if raw_queries is None:
        raw_queries = [keyword]
    if not isinstance(raw_queries, list):
        raise PublicConnectorError("stackoverflow_queries_invalid")
    queries = []
    for raw_query in raw_queries:
        value = str(raw_query or "").strip()
        if value and value not in queries:
            queries.append(value)
    if not queries:
        raise PublicConnectorError("stackoverflow_keyword_required")
    max_items = _check_budget(config, "max_items", 30, 1, 50)
    answer_max_items = _check_budget(config, "answer_max_items", 50, 1, 100)
    include_answers = config.get("include_answers", True) is not False
    site = str(config.get("site") or "stackoverflow").strip()
    transport = transport or UrllibPublicTransport(
        timeout_seconds,
        allowed_origins=("https://api.stackexchange.com",),
        max_bytes=2 * 1024 * 1024,
    )
    active_queries = queries[:5]
    # Keep the request bounded while ensuring a multi-keyword run does not
    # stop after the first keyword fills the whole result budget.
    question_budget = min(50, max_items * len(active_queries))
    questions_by_id: dict[int, dict[str, Any]] = {}
    for query in active_queries:
        params = {
            "order": "desc",
            "sort": "activity",
            "site": site,
            "pagesize": str(max_items),
            "q": query,
        }
        if config.get("tagged"):
            params["tagged"] = str(config["tagged"])
        search_url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode(params)
        search, response = _request_json(
            transport,
            search_url,
            headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"},
            code="stackoverflow_search_failed",
        )
        if _rate_limited(response):
            return []
        questions = search.get("items") if isinstance(search, dict) else None
        if not isinstance(questions, list):
            raise PublicConnectorError("stackoverflow_invalid_search_response")
        for item in questions:
            if not isinstance(item, dict) or not item.get("question_id"):
                continue
            questions_by_id[int(item["question_id"])] = item
            if len(questions_by_id) >= question_budget:
                break
        if len(questions_by_id) >= question_budget:
            break
    questions = list(questions_by_id.values())[:question_budget]
    if not questions:
        return []
    ids = ";".join(str(item["question_id"]) for item in questions)
    detail_url = f"https://api.stackexchange.com/2.3/questions/{ids}?" + urllib.parse.urlencode(
        {"site": site, "filter": "withbody"}
    )
    detail, detail_response = _request_json(
        transport,
        detail_url,
        headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="stackoverflow_detail_failed",
    )
    if _rate_limited(detail_response):
        return []
    by_id = {
        item.get("question_id"): item
        for item in (detail.get("items") or [])
        if isinstance(item, dict)
    }
    results = ConnectorEvents()
    items_seen = 0
    for question in questions:
        item = by_id.get(question.get("question_id"), question)
        text = _text_from_html(f"{item.get('title', '')}\n{item.get('body', '')}")
        if not text:
            continue
        items_seen += 1
        locator = f"stackoverflow:q:{item.get('question_id')}"
        origin_url = str(item.get("link") or detail_url)
        published_at = _parse_timestamp(item.get("creation_date"))
        results.extend(
            _event_records(
                text,
                source_kind="真实检测结果",
                channel="知识分享",
                platform="Stack Overflow",
                locator=locator,
                hmac_key=hmac_key,
                published_source="stackoverflow_creation_date",
                published_confidence="medium",
                published_at=published_at,
                published_meaning=(
                    "Stack Overflow 问题创建时间只能证明该问题页面已存在；"
                    "当前正文可能后续编辑，不能证明凭据在创建时已出现。"
                ),
                published_role="content_revision_reference",
                published_eligible_for_latency=False,
                published_evidence=origin_url,
                version_id=str(item.get("question_id") or ""),
                version_kind="stackoverflow_question",
                content_path=locator,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
        fetched_at = next(
            (
                record.get("_fetched_at")
                for record in results
                if record.get("_content_hash") == hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            None,
        )
        results.content_items.append(
            _content_item(
                text,
                platform="Stack Overflow",
                channel="知识分享",
                locator=locator,
                origin_url=origin_url,
                published_at=published_at,
                published_source="stackoverflow_creation_date",
                published_confidence="medium",
                published_evidence=origin_url,
                fetched_at=fetched_at,
                version_id=str(item.get("question_id") or ""),
                version_kind="stackoverflow_question",
            )
        )
    if include_answers:
        answer_url = (
            f"https://api.stackexchange.com/2.3/questions/{ids}/answers?"
            + urllib.parse.urlencode({
                "site": site,
                "filter": "withbody",
                "pagesize": str(answer_max_items),
                "sort": "votes",
                "order": "desc",
            })
        )
        answer_data, answer_response = _request_json(
            transport,
            answer_url,
            headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"},
            code="stackoverflow_answer_request_failed",
        )
        if _rate_limited(answer_response):
            return []
        answers = answer_data.get("items") if isinstance(answer_data, dict) else None
        if not isinstance(answers, list):
            raise PublicConnectorError("stackoverflow_invalid_answer_response")
        for answer in answers[:answer_max_items]:
            if not isinstance(answer, dict) or not answer.get("answer_id"):
                continue
            text = _text_from_html(f"{answer.get('body', '')}")
            if not text:
                continue
            items_seen += 1
            question_id = answer.get("question_id")
            locator = f"stackoverflow:a:{answer['answer_id']}:q:{question_id}"
            origin_url = str(answer.get("link") or answer_url)
            published_at = _parse_timestamp(answer.get("creation_date"))
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="知识分享",
                    platform="Stack Overflow",
                    locator=locator,
                    hmac_key=hmac_key,
                    published_source="stackoverflow_answer_creation_date",
                    published_confidence="medium",
                    published_at=published_at,
                    published_meaning=(
                        "Stack Overflow 回答创建时间只能证明该回答页面已存在；"
                        "当前正文可能后续编辑，不能证明凭据在创建时已出现。"
                    ),
                    published_role="content_revision_reference",
                    published_eligible_for_latency=False,
                    published_evidence=origin_url,
                    version_id=str(answer.get("answer_id") or ""),
                    version_kind="stackoverflow_answer",
                    content_path=locator,
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
            fetched_at = next(
                (
                    record.get("_fetched_at")
                    for record in results
                    if record.get("_content_hash") == hashlib.sha256(text.encode("utf-8")).hexdigest()
                ),
                None,
            )
            results.content_items.append(
                _content_item(
                    text,
                    platform="Stack Overflow",
                    channel="知识分享",
                    locator=locator,
                    origin_url=origin_url,
                    published_at=published_at,
                    published_source="stackoverflow_answer_creation_date",
                    published_confidence="medium",
                    published_evidence=origin_url,
                    fetched_at=fetched_at,
                    version_id=str(answer.get("answer_id") or ""),
                    version_kind="stackoverflow_answer",
                )
            )
    results.items_seen = items_seen
    return results


def collect_codeberg_repository(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    checkpoint_store: Any | None = None,
    defer_checkpoints: bool = False,
    pending_checkpoints: list[tuple[str, str]] | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> ConnectorEvents:
    """Collect bounded text files from an explicitly selected Codeberg repo.

    The adapter reads the public Forgejo API and raw file content only. It
    keeps the repository tree SHA as a content-version identifier, while
    leaving the public credential time unknown because a tree entry alone does
    not prove when a credential was first introduced.
    """
    repository = str(config.get("repository") or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PublicConnectorError("codeberg_repository_required")
    revision = str(config.get("revision") or "main").strip()
    if not revision or len(revision) > 200 or any(char in revision for char in "\r\n"):
        raise PublicConnectorError("codeberg_revision_invalid")
    max_items = _check_budget(config, "max_items", 20, 1, 100)
    base = "https://codeberg.org"
    transport = transport or _package_transport(
        None,
        timeout_seconds=timeout_seconds,
        origins=(base,),
        max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
        follow_redirects=True,
    )
    tree_url = (
        f"{base}/api/v1/repos/{urllib.parse.quote(repository, safe='/')}"
        f"/git/trees/{urllib.parse.quote(revision, safe='')}?recursive=true"
    )
    tree, _ = _request_json(
        transport,
        tree_url,
        headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="codeberg_tree_request_failed",
    )
    entries = tree.get("tree") if isinstance(tree, dict) else None
    if not isinstance(entries, list):
        raise PublicConnectorError("codeberg_tree_invalid")
    tree_sha = str((tree or {}).get("sha") or "")
    checkpoint_target = f"codeberg:{repository}:{revision}"
    if checkpoint_store is not None and tree_sha:
        previous = checkpoint_store.get_checkpoint(checkpoint_target)
        if previous:
            try:
                previous_sha = str(json.loads(previous).get("tree_sha") or "")
            except (TypeError, json.JSONDecodeError):
                previous_sha = str(previous)
            if previous_sha and previous_sha == tree_sha:
                empty = ConnectorEvents(items_seen=0)
                empty.checkpoint_status = "unchanged"
                return empty
        checkpoint_value = json.dumps(
            {"tree_sha": tree_sha, "revision": revision},
            ensure_ascii=False,
        )
        if defer_checkpoints and pending_checkpoints is not None:
            pending_checkpoints.append((checkpoint_target, checkpoint_value))
        else:
            checkpoint_store.set_checkpoint(checkpoint_target, checkpoint_value)
    allowed_suffixes = {
        ".env", ".ini", ".cfg", ".conf", ".txt", ".yaml", ".yml",
        ".json", ".toml", ".pem", ".properties", ".xml", ".md",
        ".js", ".ts", ".html",
    }
    selected = [
        item for item in entries
        if isinstance(item, dict)
        and str(item.get("type") or "blob") == "blob"
        and Path(str(item.get("path") or "")).suffix.lower() in allowed_suffixes
    ][:max_items]
    results = ConnectorEvents()
    results.checkpoint_status = "updated" if tree_sha else "not_applicable"
    for entry in selected:
        path = str(entry.get("path") or "").replace("\\", "/").lstrip("/")
        if not path:
            continue
        raw_url = (
            f"{base}/{urllib.parse.quote(repository, safe='/')}/raw/"
            f"{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(path, safe='/')}"
        )
        data = _package_download(
            transport,
            raw_url,
            allowed_hosts={"codeberg.org"},
            code="codeberg_file",
        )
        if len(data) > max_file_bytes * 4 or b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        if not text.strip():
            continue
        fetched_at = _now()
        version_id = str(entry.get("sha") or tree_sha or hashlib.sha256(data).hexdigest())
        locator = f"codeberg:{repository}@{revision}:{path}"
        results.items_seen += 1
        results.extend(
            _event_records(
                text,
                source_kind="真实检测结果",
                channel="代码托管",
                platform="Codeberg",
                locator=locator,
                hmac_key=hmac_key,
                published_meaning=(
                    "Codeberg 当前内容版本的公开时间未由树接口提供；"
                    "当前记录只保留版本标识和内容定位。"
                ),
                published_role="unknown",
                published_eligible_for_latency=False,
                published_unknown_reason="connector_not_provided",
                version_id=version_id,
                version_kind="repository_tree",
                content_path=path,
                fetched_at=fetched_at,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
        results.content_items.append(
            _content_item(
                text,
                platform="Codeberg",
                channel="代码托管",
                locator=locator,
                origin_url=raw_url,
                published_at=None,
                published_source="",
                published_confidence="none",
                published_evidence="",
                fetched_at=fetched_at,
                version_id=version_id,
                version_kind="repository_tree",
                content_path=path,
            )
        )
    return results


WEIBO_AUTHORIZED_API_BASE = "https://open.weibo.com/cli/api"
WEIBO_AUTHORIZED_API_ORIGIN = "https://open.weibo.com"
WEIBO_AUTHORIZED_CLIENT_VERSION = "0.9.8"
_WEIBO_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)


class _WeiboAuthorizedTransport:
    """Small fixed-host POST transport for the official OAuth API gateway.

    The host and path are constants. Source configuration cannot redirect this
    connector to an arbitrary endpoint, and the access token is sent only in
    the request header.
    """

    def __init__(self, timeout_seconds: float, max_bytes: int = 2 * 1024 * 1024):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "POST",
        body: dict[str, Any] | None = None,
    ) -> HttpResult:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "open.weibo.com"
            or parsed.path != "/cli/api/cli/invoke"
            or parsed.query
            or parsed.fragment
        ):
            raise PublicConnectorError("weibo_authorized_api_endpoint_invalid")
        if method != "POST" or not isinstance(body, dict):
            raise PublicConnectorError("weibo_authorized_api_method_invalid")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "SecretRadar-weibo-authorized-api/1.0",
                **(headers or {}),
            },
            method="POST",
        )
        try:
            # The official CLI uses a direct Node HTTP client. Do the same for
            # this fixed official endpoint so a generic workstation proxy
            # cannot change the Weibo API's regional authorization decision.
            with _WEIBO_DIRECT_OPENER.open(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise PublicConnectorError("weibo_authorized_api_response_too_large")
                return HttpResult(response.status, response_headers, data)
        except urllib.error.HTTPError as error:
            data = error.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise PublicConnectorError("weibo_authorized_api_response_too_large")
            response_headers = {
                key.lower(): value for key, value in error.headers.items()
            }
            return HttpResult(error.code, response_headers, data)
        except PublicConnectorError:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            raise PublicConnectorError("weibo_authorized_api_network_error") from None


def _weibo_authorized_api_request(
    transport: Any,
    *,
    token: str,
    action: str,
    args: dict[str, str],
) -> tuple[dict[str, Any], HttpResult]:
    response = transport.request(
        f"{WEIBO_AUTHORIZED_API_BASE}/cli/invoke",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": f"weibo-cli/{WEIBO_AUTHORIZED_CLIENT_VERSION}",
            "X-Weibo-Agent-Source": "unknown",
            "X-Weibo-Agent-Host": "unknown",
            "X-Weibo-Source-Method": "default",
        },
        body={
            "clientVersion": WEIBO_AUTHORIZED_CLIENT_VERSION,
            "source": {
                "name": "unknown",
                "host": "unknown",
                "method": "default",
            },
            "method": "POST",
            "token": token,
            "group": "statuses",
            "action": action,
            "args": args,
        },
    )
    error_by_status = {
        401: "weibo_authorized_api_unauthorized",
        403: "weibo_authorized_api_forbidden",
        404: "weibo_authorized_api_not_found",
        429: "weibo_authorized_api_rate_limited",
    }
    if response.status in error_by_status:
        raise PublicConnectorError(error_by_status[response.status])
    if response.status != 200:
        raise PublicConnectorError("weibo_authorized_api_request_failed")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise PublicConnectorError("weibo_authorized_api_invalid_json") from None
    if not isinstance(payload, dict):
        raise PublicConnectorError("weibo_authorized_api_invalid_response")
    return payload, response


def _weibo_statuses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if isinstance(result, dict):
        payload = result
    raw = payload.get("statuses")
    if raw is None and isinstance(payload.get("data"), dict):
        raw = payload["data"].get("statuses")
    if raw is None and isinstance(payload.get("data"), list):
        raw = payload["data"]
    if not isinstance(raw, list):
        raise PublicConnectorError("weibo_authorized_api_invalid_response")
    return [item for item in raw if isinstance(item, dict)]


def _weibo_status_text(status: dict[str, Any]) -> str:
    value = (
        status.get("longTextContent")
        or status.get("long_text")
        or status.get("text")
        or ""
    )
    return _text_from_html(str(value))


def _weibo_status_id(status: dict[str, Any]) -> str:
    value = status.get("idstr") or status.get("id")
    return str(value or "").strip()


def _weibo_status_user_id(status: dict[str, Any]) -> str:
    user = status.get("user")
    if not isinstance(user, dict):
        return ""
    return str(user.get("idstr") or user.get("id") or "").strip()


def collect_weibo_authorized_api(
    config: dict[str, Any],
    *,
    access_token: str,
    hmac_key: str,
    timeout_seconds: float,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect explicitly authorized Weibo statuses through the OAuth API.

    The source configuration identifies a post or an owned account. It never
    carries the access token; the token is read from encrypted runtime
    settings by the pipeline.
    """
    if not str(access_token or "").strip():
        raise PublicConnectorError("weibo_authorized_token_required")
    raw_uid = config.get("uid") or config.get("user_id")
    uid = str(raw_uid or "").strip()
    if uid and not re.fullmatch(r"\d{1,32}", uid):
        raise PublicConnectorError("weibo_authorized_uid_invalid")
    raw_ids = config.get("post_ids")
    if raw_ids is None and config.get("post_id") is not None:
        raw_ids = [config.get("post_id")]
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            raise PublicConnectorError("weibo_authorized_post_ids_invalid")
        post_ids = [str(value).strip() for value in raw_ids if str(value).strip()]
        if not post_ids or any(not re.fullmatch(r"\d{6,32}", value) for value in post_ids):
            raise PublicConnectorError("weibo_authorized_post_ids_invalid")
    else:
        post_ids = []
    max_items = _check_budget(config, "max_items", 10, 1, 20)
    include_long_text = config.get("include_long_text", True)
    if not isinstance(include_long_text, bool):
        raise PublicConnectorError("weibo_authorized_include_long_text_invalid")
    if not post_ids and not uid:
        raise PublicConnectorError("weibo_authorized_target_required")
    transport = transport or _WeiboAuthorizedTransport(timeout_seconds)
    if post_ids:
        action = "show_batch/biz"
        request_args = {
            "ids": ",".join(post_ids[:max_items]),
            "isGetLongText": "1" if include_long_text else "0",
        }
    else:
        action = "user_timeline/other"
        request_args = {
            "uid": uid,
            "count": str(max_items),
            "page": "1",
            "isGetLongText": "1" if include_long_text else "0",
        }
    payload, _ = _weibo_authorized_api_request(
        transport,
        token=str(access_token).strip(),
        action=action,
        args=request_args,
    )
    statuses = _weibo_statuses(payload)
    if uid:
        statuses = [
            item for item in statuses
            if _weibo_status_user_id(item) in {"", uid}
        ]
    statuses = statuses[:max_items]
    if not statuses:
        raise PublicConnectorError("weibo_authorized_api_no_matching_status")
    results = ConnectorEvents(items_seen=len(statuses))
    for status in statuses:
        status_id = _weibo_status_id(status)
        text = _weibo_status_text(status)
        if not status_id or not text:
            continue
        status_uid = _weibo_status_user_id(status) or uid
        origin_url = f"https://weibo.com/{status_uid}/{status_id}" if status_uid else (
            f"https://weibo.com/statuses/{status_id}"
        )
        locator = f"weibo-api:status:{status_id}"
        published_at = _parse_weibo_timestamp(status.get("created_at"))
        results.extend(
            _event_records(
                text,
                source_kind="真实检测结果",
                channel="微博",
                platform="微博（官方 OAuth API）",
                locator=locator,
                hmac_key=hmac_key,
                published_at=published_at,
                published_source="weibo_api_created_at" if published_at else "",
                published_confidence="medium" if published_at else "none",
                published_evidence=f"{WEIBO_AUTHORIZED_API_BASE}/cli/invoke#status={status_id}",
                published_meaning=(
                    "微博官方 OAuth API 返回的微博创建时间，证明该微博内容在该时间已创建；"
                    "不等于凭据首次公开或首次泄露时间。"
                    if published_at else ""
                ),
                published_role="content_revision_reference" if published_at else "",
                published_eligible_for_latency=False,
                version_id=f"weibo:{status_id}",
                version_kind="weibo_status",
                content_path=locator,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
        fetched_at = next(
            (
                record.get("_fetched_at")
                for record in results
                if record.get("_content_hash") == hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            None,
        )
        results.content_items.append(
            _content_item(
                text,
                platform="微博（官方 OAuth API）",
                channel="微博",
                locator=locator,
                origin_url=origin_url,
                published_at=published_at,
                published_source="weibo_api_created_at" if published_at else "",
                published_confidence="medium" if published_at else "none",
                published_evidence=f"{WEIBO_AUTHORIZED_API_BASE}/cli/invoke#status={status_id}",
                fetched_at=fetched_at,
                version_id=f"weibo:{status_id}",
                version_kind="weibo_status",
                content_path=locator,
            )
        )
    return results


_IMAGE_REF = re.compile(
    r"^(?:(?P<registry>[^/]+\.[^/]+)/)?(?P<repo>[A-Za-z0-9._/-]+?)(?::(?P<tag>[\w.-]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)


def _parse_image_ref(value: str) -> dict[str, str | None]:
    raw = str(value or "").strip()
    if raw.startswith("docker://"):
        raw = raw[9:]
    match = _IMAGE_REF.fullmatch(raw)
    if not match:
        raise PublicConnectorError("oci_image_reference_invalid")
    registry = match.group("registry") or "docker.io"
    repo = match.group("repo")
    if registry == "docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    digest = match.group("digest")
    tag = match.group("tag") or ("latest" if digest is None else None)
    return {"registry": registry, "repo": repo, "tag": tag, "digest": digest}


def _tar_text_entries(
    blob: bytes,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
) -> list[tuple[str, str]]:
    try:
        raw = gzip.decompress(blob)
    except (OSError, gzip.BadGzipFile):
        raw = blob
    if len(raw) > max_total_bytes:
        raise PublicConnectorError("oci_layer_budget_exceeded")
    entries: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        total = 0
        for member in archive:
            if not member.isfile() or member.issym() or member.islnk() or member.isdev():
                continue
            name = member.name.replace("\\", "/").lstrip("./")
            if name.rsplit("/", 1)[-1].startswith(".wh."):
                continue
            if member.size <= 0 or member.size > max_member_bytes:
                continue
            total += member.size
            if total > max_total_bytes:
                raise PublicConnectorError("oci_layer_budget_exceeded")
            handle = archive.extractfile(member)
            if handle is None:
                continue
            data = handle.read(max_member_bytes + 1)
            if len(data) > max_member_bytes or b"\x00" in data[:8192]:
                continue
            text = data.decode("utf-8", errors="replace")
            if text.strip():
                entries.append((name, text))
    return entries


def collect_oci_registry(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    ref = _parse_image_ref(str(config.get("image") or ""))
    registry = str(ref["registry"])
    repo = str(ref["repo"])
    if registry == "docker.io":
        base = "https://registry-1.docker.io"
        auth_url = "https://auth.docker.io/token"
        auth_params = {"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
    else:
        base = _validate_public_url(f"https://{registry}")
        auth_url = f"{base}/v2/"
        auth_params = {}
    allowed_origins = [base]
    robots_exempt_origins: tuple[str, ...] = ()
    if registry == "docker.io":
        allowed_origins.append("https://auth.docker.io")
        # Docker Hub serves manifest blobs through a short-lived signed
        # CloudFront URL. The redirect is part of the official registry flow,
        # while the CDN endpoint does not expose the repository robots policy.
        allowed_origins.append("https://production.cloudfront.docker.com")
        robots_exempt_origins = ("https://production.cloudfront.docker.com",)
    transport = transport or UrllibPublicTransport(
        timeout_seconds,
        follow_redirects=True,
        allowed_origins=tuple(allowed_origins),
        robots_exempt_origins=robots_exempt_origins,
        max_bytes=max(2 * 1024 * 1024, max_file_bytes * 8),
    )
    token = None
    if registry == "docker.io":
        token_result = transport.request(auth_url + "?" + urllib.parse.urlencode(auth_params))
        token_payload = _json(token_result, "oci_auth_failed")
        token = token_payload.get("token")
    else:
        probe = transport.request(auth_url)
        if probe.status == 401:
            challenge = probe.headers.get("www-authenticate", "")
            realm = re.search(r'realm="([^"]+)"', challenge)
            service = re.search(r'service="([^"]+)"', challenge)
            if realm:
                params = {"scope": f"repository:{repo}:pull"}
                if service:
                    params["service"] = service.group(1)
                auth_result = transport.request(realm.group(1) + "?" + urllib.parse.urlencode(params))
                token = _json(auth_result, "oci_auth_failed").get("token")
        elif probe.status not in {200, 204}:
            raise PublicConnectorError("oci_registry_unavailable")
    auth_headers = {"Authorization": f"Bearer {token}"} if token else {}
    reference = str(ref["digest"] or ref["tag"])
    manifest_result = transport.request(
        f"{base}/v2/{repo}/manifests/{urllib.parse.quote(reference, safe=':@')}",
        headers={**auth_headers, "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json"},
    )
    manifest = _json(manifest_result, "oci_manifest_failed")
    if "manifests" in manifest:
        manifests = manifest.get("manifests") or []
        chosen = next(
            (item for item in manifests if (item.get("platform") or {}).get("architecture") == "amd64"),
            manifests[0] if manifests else None,
        )
        if not chosen or not chosen.get("digest"):
            raise PublicConnectorError("oci_manifest_index_invalid")
        child = transport.request(
            f"{base}/v2/{repo}/manifests/{chosen['digest']}",
            headers={**auth_headers, "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"},
        )
        manifest = _json(child, "oci_manifest_failed")
    config_digest = (manifest.get("config") or {}).get("digest")
    if not config_digest:
        raise PublicConnectorError("oci_manifest_config_missing")
    created_at = None
    config_result = transport.request(f"{base}/v2/{repo}/blobs/{config_digest}", headers=auth_headers)
    if config_result.status == 200:
        try:
            created_at = _parse_timestamp(json.loads(config_result.body.decode("utf-8")).get("created"))
        except (UnicodeError, json.JSONDecodeError):
            created_at = None
    max_total = int(config.get("max_layer_bytes") or 512 * 1024 * 1024)
    if max_total < 1024 or max_total > 2 * 1024 * 1024 * 1024:
        raise PublicConnectorError("oci_layer_budget_invalid")
    layers = manifest.get("layers") or []
    if sum(int(item.get("size") or 0) for item in layers) > max_total:
        raise PublicConnectorError("oci_layer_budget_exceeded")
    results = ConnectorEvents(items_seen=0)
    for layer in layers:
        digest = str(layer.get("digest") or "")
        if not digest:
            continue
        blob = transport.request(f"{base}/v2/{repo}/blobs/{digest}", headers=auth_headers)
        if _rate_limited(blob):
            return results
        if blob.status != 200:
            continue
        for path, text in _tar_text_entries(
            blob.body,
            max_member_bytes=max_file_bytes * 4,
            max_total_bytes=max_total,
        ):
            results.items_seen += 1
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="容器镜像",
                    platform=f"OCI Registry（{registry}）",
                    locator=f"oci:{registry}/{repo}@{digest}:{path}",
                    hmac_key=hmac_key,
                    published_at=created_at,
                    published_source="image_config_created" if created_at else "",
                    published_confidence="low" if created_at else "none",
                    published_evidence=f"{base}/v2/{repo}/blobs/{config_digest}" if created_at else "",
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
    return results


def _archive_text_records(
    data: bytes,
    *,
    source_kind: str,
    channel: str,
    platform: str,
    locator_prefix: str,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    published_at: str | None = None,
    published_source: str = "",
    published_confidence: str = "none",
    published_evidence: str = "",
    published_meaning: str = "",
    published_role: str = "",
    published_eligible_for_latency: bool | None = None,
    version_id: str | None = None,
    version_kind: str = "archive",
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    results = ConnectorEvents(items_seen=0)
    for path, text in _tar_text_entries(
        data,
        max_member_bytes=max_file_bytes * 4,
        max_total_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
    ):
        results.items_seen += 1
        results.extend(
            _event_records(
                text,
                source_kind=source_kind,
                channel=channel,
                platform=platform,
                locator=f"{locator_prefix}:{path}",
                hmac_key=hmac_key,
                published_at=published_at,
                published_source=published_source,
                published_confidence=published_confidence,
                published_evidence=published_evidence,
                published_meaning=published_meaning,
                published_role=published_role,
                published_eligible_for_latency=published_eligible_for_latency,
                version_id=version_id,
                version_kind=version_kind,
                content_path=path,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
    return results


def _zip_text_records(
    data: bytes,
    *,
    source_kind: str,
    channel: str,
    platform: str,
    locator_prefix: str,
    hmac_key: str,
    max_file_bytes: int,
    published_at: str | None = None,
    published_source: str = "",
    published_confidence: str = "none",
    published_evidence: str = "",
    published_meaning: str = "",
    published_role: str = "",
    published_eligible_for_latency: bool | None = None,
    version_id: str | None = None,
    version_kind: str = "archive",
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan bounded text members from a ZIP archive without executing files."""
    max_total = max(4 * 1024 * 1024, max_file_bytes * 32)
    if len(data) > max_total:
        raise PublicConnectorError("source_archive_too_large")
    allowed_suffixes = {
        ".env", ".ini", ".cfg", ".conf", ".txt", ".yaml", ".yml",
        ".json", ".toml", ".pem", ".properties", ".xml", ".md",
        ".js", ".ts", ".html",
    }
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise PublicConnectorError("source_archive_invalid") from error
    results = ConnectorEvents(items_seen=0)
    total = 0
    with archive:
        members = archive.infolist()
        if len(members) > 20_000:
            raise PublicConnectorError("source_archive_member_budget_exceeded")
        for member in members:
            name = member.filename.replace("\\", "/").lstrip("./")
            if member.is_dir() or Path(name).suffix.lower() not in allowed_suffixes:
                continue
            if member.file_size <= 0 or member.file_size > max_file_bytes * 4:
                continue
            total += member.file_size
            if total > max_total:
                raise PublicConnectorError("source_archive_budget_exceeded")
            raw = archive.read(member)
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():
                continue
            results.items_seen += 1
            results.extend(
                _event_records(
                    text,
                    source_kind=source_kind,
                    channel=channel,
                    platform=platform,
                    locator=f"{locator_prefix}:{name}",
                    hmac_key=hmac_key,
                    published_at=published_at,
                    published_source=published_source,
                    published_confidence=published_confidence,
                    published_evidence=published_evidence,
                    published_meaning=published_meaning,
                    published_role=published_role,
                    published_eligible_for_latency=published_eligible_for_latency,
                    version_id=version_id,
                    version_kind=version_kind,
                    content_path=name,
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
    return results


def _package_download(
    transport: Any,
    url: str,
    *,
    allowed_hosts: set[str],
    code: str,
) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in allowed_hosts:
        raise PublicConnectorError(f"{code}_url_not_allowed")
    response = transport.request(url, headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"})
    if response.status != 200:
        raise PublicConnectorError(f"{code}_download_failed")
    return response.body


def collect_npm_registry(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    package = str(config.get("package") or "").strip()
    if not package or len(package) > 214:
        raise PublicConnectorError("npm_package_required")
    if package.startswith("@") and "/" not in package:
        raise PublicConnectorError("npm_package_invalid")
    base = "https://registry.npmjs.org"
    transport = transport or UrllibPublicTransport(
        timeout_seconds,
        allowed_origins=(base,),
        max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
    )
    metadata_url = f"{base}/{urllib.parse.quote(package, safe='@/')}"
    metadata, _ = _request_json(
        transport,
        metadata_url,
        headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="npm_metadata_request_failed",
    )
    if not isinstance(metadata, dict):
        raise PublicConnectorError("npm_metadata_invalid")
    version = str(config.get("version") or (metadata.get("dist-tags") or {}).get("latest") or "").strip()
    versions = metadata.get("versions") or {}
    version_data = versions.get(version) if isinstance(versions, dict) else None
    if not version or not isinstance(version_data, dict):
        raise PublicConnectorError("npm_version_not_found")
    dist = version_data.get("dist") or {}
    tarball = str(dist.get("tarball") or "")
    if not tarball:
        raise PublicConnectorError("npm_tarball_missing")
    data = _package_download(
        transport,
        tarball,
        allowed_hosts={"registry.npmjs.org"},
        code="npm",
    )
    if len(data) > max(4 * 1024 * 1024, max_file_bytes * 32):
        raise PublicConnectorError("npm_package_too_large")
    published_at = None
    times = metadata.get("time") or {}
    if isinstance(times, dict):
        published_at = _parse_timestamp(times.get(version))
    package_hash = hashlib.sha256(data).hexdigest()[:32]
    return _archive_text_records(
        data,
        source_kind="真实检测结果",
        channel="包仓库",
        platform="npm Registry",
        locator_prefix=f"npm:{package}@{version}:{package_hash}",
        hmac_key=hmac_key,
        timeout_seconds=timeout_seconds,
        max_file_bytes=max_file_bytes,
        published_at=published_at,
        published_source="npm_registry_version_time" if published_at else "",
        published_confidence="high" if published_at else "none",
        published_evidence=f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='')}" if published_at else "",
        published_meaning=(
            "npm Registry 版本发布时间，可证明该包版本在该时间已公开；"
            "不等于凭据首次泄露时间。"
            if published_at else ""
        ),
        published_role="public_content_time" if published_at else "",
        published_eligible_for_latency=True if published_at else False,
        version_id=f"npm:{package}@{version}",
        version_kind="package_version",
        enabled_rule_ids=enabled_rule_ids,
    )


def collect_pypi_registry(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    package = str(config.get("package") or "").strip()
    if not package or len(package) > 200:
        raise PublicConnectorError("pypi_package_required")
    base = "https://pypi.org"
    transport = transport or UrllibPublicTransport(
        timeout_seconds,
        allowed_origins=(base, "https://files.pythonhosted.org"),
        max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
    )
    version = str(config.get("version") or "").strip()
    suffix = f"/{urllib.parse.quote(version, safe='')}" if version else ""
    metadata_url = f"{base}/pypi/{urllib.parse.quote(package, safe='')}{suffix}/json"
    metadata, _ = _request_json(
        transport,
        metadata_url,
        headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="pypi_metadata_request_failed",
    )
    if not isinstance(metadata, dict):
        raise PublicConnectorError("pypi_metadata_invalid")
    info = metadata.get("info") or {}
    resolved_version = str(info.get("version") or version).strip()
    releases = metadata.get("urls") or []
    candidates = [
        item for item in releases
        if isinstance(item, dict)
        and str(item.get("url") or "").endswith((".tar.gz", ".zip", ".whl"))
    ]
    if not candidates:
        raise PublicConnectorError("pypi_distribution_missing")
    preferred = next(
        (item for item in candidates if str(item.get("packagetype") or "") == "sdist"),
        candidates[0],
    )
    url = str(preferred.get("url") or "")
    data = _package_download(
        transport,
        url,
        allowed_hosts={"pypi.org", "files.pythonhosted.org"},
        code="pypi",
    )
    if len(data) > max(4 * 1024 * 1024, max_file_bytes * 32):
        raise PublicConnectorError("pypi_package_too_large")
    package_hash = hashlib.sha256(data).hexdigest()[:32]
    published_at = _parse_timestamp(preferred.get("upload_time_iso_8601"))
    if url.endswith(".whl"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = []
                total = 0
                for info_item in archive.infolist():
                    if info_item.is_dir() or info_item.file_size <= 0 or info_item.file_size > max_file_bytes * 4:
                        continue
                    total += info_item.file_size
                    if total > max(4 * 1024 * 1024, max_file_bytes * 32):
                        raise PublicConnectorError("pypi_package_budget_exceeded")
                    raw = archive.read(info_item)
                    if b"\x00" in raw[:8192]:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    if text.strip():
                        entries.append((info_item.filename.replace("\\", "/"), text))
        except zipfile.BadZipFile as error:
            raise PublicConnectorError("pypi_distribution_invalid") from error
        results = ConnectorEvents(items_seen=0)
        for path, text in entries:
            results.items_seen += 1
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="包仓库",
                    platform="PyPI",
                    locator=f"pypi:{package}@{resolved_version}:{package_hash}:{path}",
                    hmac_key=hmac_key,
                    published_at=published_at,
                    published_source="pypi_upload_time" if published_at else "",
                    published_confidence="low" if published_at else "none",
                    published_evidence=url if published_at else "",
                    published_meaning=(
                        "PyPI 分发文件上传时间，仅作为内容发布时间参考；"
                        "不等于凭据首次泄露时间。"
                        if published_at else ""
                    ),
                    published_role="content_revision_reference" if published_at else "",
                    published_eligible_for_latency=False,
                    version_id=f"pypi:{package}@{resolved_version}",
                    version_kind="package_version",
                    content_path=info_item.filename.replace("\\", "/"),
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
        return results
    return _archive_text_records(
        data,
        source_kind="真实检测结果",
        channel="包仓库",
        platform="PyPI",
        locator_prefix=f"pypi:{package}@{resolved_version}:{package_hash}",
        hmac_key=hmac_key,
        timeout_seconds=timeout_seconds,
        max_file_bytes=max_file_bytes,
        published_at=published_at,
        published_source="pypi_upload_time" if published_at else "",
        published_confidence="low" if published_at else "none",
        published_evidence=url if published_at else "",
        published_meaning=(
            "PyPI 分发文件上传时间，仅作为内容发布时间参考；不等于凭据首次泄露时间。"
            if published_at else ""
        ),
        published_role="content_revision_reference" if published_at else "",
        published_eligible_for_latency=False,
        version_id=f"pypi:{package}@{resolved_version}",
        version_kind="package_version",
        enabled_rule_ids=enabled_rule_ids,
    )


def _package_transport(
    transport: Any | None,
    *,
    timeout_seconds: float,
    origins: tuple[str, ...],
    max_bytes: int,
    follow_redirects: bool = False,
) -> Any:
    return transport or UrllibPublicTransport(
        timeout_seconds,
        follow_redirects=follow_redirects,
        allowed_origins=origins,
        max_bytes=max_bytes,
    )


def collect_huggingface_repo(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> ConnectorEvents:
    """Scan bounded text files from a public Hugging Face model/dataset repo."""
    repository = str(config.get("repository") or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PublicConnectorError("huggingface_repository_required")
    revision = str(config.get("revision") or "main").strip()
    if not revision or len(revision) > 200 or any(char in revision for char in "\r\n"):
        raise PublicConnectorError("huggingface_revision_invalid")
    base = "https://huggingface.co"
    transport = _package_transport(
        transport,
        timeout_seconds=timeout_seconds,
        origins=(base,),
        max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
        follow_redirects=True,
    )
    metadata, _ = _request_json(
        transport,
        f"{base}/api/models/{urllib.parse.quote(repository, safe='/')}",
        headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="huggingface_metadata_request_failed",
    )
    siblings = metadata.get("siblings") if isinstance(metadata, dict) else None
    if not isinstance(siblings, list):
        raise PublicConnectorError("huggingface_metadata_invalid")
    allowed_suffixes = {
        ".env", ".ini", ".cfg", ".conf", ".txt", ".yaml", ".yml",
        ".json", ".toml", ".pem", ".properties", ".xml", ".md",
    }
    max_items = _check_budget(config, "max_items", 50, 1, 200)
    selected = []
    for item in siblings:
        path = str(item.get("rfilename") or "") if isinstance(item, dict) else ""
        if path and Path(path).suffix.lower() in allowed_suffixes:
            selected.append(path)
        if len(selected) >= max_items:
            break
    results = ConnectorEvents()
    for path in selected:
        raw_url = (
            f"{base}/{urllib.parse.quote(repository, safe='/')}/resolve/"
            f"{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(path, safe='/')}"
        )
        data = _package_download(
            transport,
            raw_url,
            allowed_hosts={"huggingface.co"},
            code="huggingface_file",
        )
        if len(data) > max_file_bytes * 4 or b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        if text.strip():
            results.items_seen += 1
            results.extend(
                _event_records(
                    text,
                    source_kind="真实检测结果",
                    channel="AI 模型与数据",
                    platform="Hugging Face Hub",
                    locator=f"huggingface:{repository}@{revision}:{path}",
                    hmac_key=hmac_key,
                    enabled_rule_ids=enabled_rule_ids,
                )
            )
    return results


def collect_crates_registry(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan a crates.io source archive without compiling or executing it."""
    package = str(config.get("package") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", package):
        raise PublicConnectorError("crates_package_required")
    base = "https://crates.io"
    if transport is None:
        # crates.io serves downloads through static.crates.io with a 302.
        # Keep the redirect bounded to the two official public origins.
        transport = UrllibPublicTransport(
            timeout_seconds,
            follow_redirects=True,
            allowed_origins=(base, "https://static.crates.io"),
            robots_exempt_origins=("https://static.crates.io",),
            max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
        )
    metadata, _ = _request_json(
        transport,
        f"{base}/api/v1/crates/{urllib.parse.quote(package, safe='')}",
        headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="crates_metadata_request_failed",
    )
    crate = metadata.get("crate") if isinstance(metadata, dict) else None
    version = str(config.get("version") or (crate or {}).get("max_version") or "").strip()
    if not version:
        raise PublicConnectorError("crates_version_missing")
    url = f"{base}/api/v1/crates/{urllib.parse.quote(package, safe='')}/{urllib.parse.quote(version, safe='')}/download"
    data = _package_download(
        transport,
        url,
        allowed_hosts={"crates.io"},
        code="crates",
    )
    if len(data) > max(4 * 1024 * 1024, max_file_bytes * 32):
        raise PublicConnectorError("crates_package_too_large")
    return _archive_text_records(
        data,
        source_kind="真实检测结果",
        channel="包仓库",
        platform="crates.io",
        locator_prefix=f"crates:{package}@{version}",
        hmac_key=hmac_key,
        timeout_seconds=timeout_seconds,
        max_file_bytes=max_file_bytes,
        enabled_rule_ids=enabled_rule_ids,
    )


def collect_nuget_registry(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan a bounded NuGet nupkg archive without running package code."""
    package = str(config.get("package") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", package):
        raise PublicConnectorError("nuget_package_required")
    base = "https://api.nuget.org"
    transport = _package_transport(
        transport,
        timeout_seconds=timeout_seconds,
        origins=(base,),
        max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
    )
    version = str(config.get("version") or "").strip().lower()
    if not version:
        metadata, _ = _request_json(
            transport,
            f"{base}/v3-flatcontainer/{urllib.parse.quote(package, safe='')}/index.json",
            headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
            code="nuget_versions_request_failed",
        )
        versions = metadata.get("versions") if isinstance(metadata, dict) else None
        if not isinstance(versions, list) or not versions:
            raise PublicConnectorError("nuget_version_missing")
        version = str(versions[-1]).strip().lower()
    url = (
        f"{base}/v3-flatcontainer/{urllib.parse.quote(package, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/{urllib.parse.quote(package, safe='')}.{urllib.parse.quote(version, safe='')}.nupkg"
    )
    data = _package_download(
        transport,
        url,
        allowed_hosts={"api.nuget.org"},
        code="nuget",
    )
    return _zip_text_records(
        data,
        source_kind="真实检测结果",
        channel="包仓库",
        platform="NuGet",
        locator_prefix=f"nuget:{package}@{version}",
        hmac_key=hmac_key,
        max_file_bytes=max_file_bytes,
        enabled_rule_ids=enabled_rule_ids,
    )


def _rubygem_text_records(
    data: bytes,
    *,
    package: str,
    version: str,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    enabled_rule_ids: set[str] | frozenset[str] | None,
) -> list[dict[str, Any]]:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            payload = next(
                (
                    member
                    for member in archive
                    if member.isfile() and member.name.replace("\\", "/").lstrip("./") == "data.tar.gz"
                ),
                None,
            )
            if payload is None:
                raise PublicConnectorError("rubygems_data_missing")
            handle = archive.extractfile(payload)
            nested = handle.read() if handle else b""
    except (tarfile.TarError, OSError) as error:
        raise PublicConnectorError("rubygems_package_invalid") from error
    return _archive_text_records(
        nested,
        source_kind="真实检测结果",
        channel="包仓库",
        platform="RubyGems",
        locator_prefix=f"rubygems:{package}@{version}",
        hmac_key=hmac_key,
        timeout_seconds=timeout_seconds,
        max_file_bytes=max_file_bytes,
        enabled_rule_ids=enabled_rule_ids,
    )


def collect_rubygems_registry(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    package = str(config.get("package") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", package):
        raise PublicConnectorError("rubygems_package_required")
    base = "https://rubygems.org"
    transport = _package_transport(
        transport,
        timeout_seconds=timeout_seconds,
        origins=(base,),
        max_bytes=max(4 * 1024 * 1024, max_file_bytes * 32),
    )
    metadata, _ = _request_json(
        transport,
        f"{base}/api/v1/gems/{urllib.parse.quote(package, safe='')}.json",
        headers={"Accept": "application/json", "User-Agent": "ccs-cloud-credential-sentinel/1.0"},
        code="rubygems_metadata_request_failed",
    )
    version = str(config.get("version") or (metadata or {}).get("version") or "").strip()
    if not version:
        raise PublicConnectorError("rubygems_version_missing")
    data = _package_download(
        transport,
        f"{base}/gems/{urllib.parse.quote(package, safe='')}-{urllib.parse.quote(version, safe='')}.gem",
        allowed_hosts={"rubygems.org"},
        code="rubygems",
    )
    return _rubygem_text_records(
        data,
        package=package,
        version=version,
        hmac_key=hmac_key,
        timeout_seconds=timeout_seconds,
        max_file_bytes=max_file_bytes,
        enabled_rule_ids=enabled_rule_ids,
    )


def _dex_strings(data: bytes, cap: int = 200_000) -> str:
    chunks = [match.group(0).decode("ascii", errors="ignore") for match in re.finditer(rb"[\x20-\x7e]{8,}", data)]
    text = "\n".join(chunks)
    return text[:cap]


def _artifact_bytes(
    config: dict[str, Any],
    *,
    suffix: str,
    timeout_seconds: float,
    transport: Any | None,
) -> tuple[bytes, str]:
    path = str(config.get("path") or "").strip()
    url = str(config.get("url") or "").strip()
    if bool(path) == bool(url):
        raise PublicConnectorError(f"{suffix}_path_or_url_required")
    if path:
        candidate = Path(path).expanduser().resolve()
        try:
            return candidate.read_bytes(), candidate.name
        except OSError as error:
            raise PublicConnectorError(f"{suffix}_file_unavailable") from error
    _validate_public_url(url)
    if transport is None:
        origins = (url,) if url else ()
        transport = UrllibPublicTransport(
            timeout_seconds,
            follow_redirects=True,
            allowed_origins=origins,
            max_bytes=512 * 1024 * 1024,
        )
    result = transport.request(url, headers={"User-Agent": "ccs-cloud-credential-sentinel/1.0"})
    if result.status != 200:
        raise PublicConnectorError(f"{suffix}_download_failed")
    return result.body, url.rsplit("/", 1)[-1] or f"download.{suffix}"


def collect_apk(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    data, source_name = _artifact_bytes(config, suffix="apk", timeout_seconds=timeout_seconds, transport=transport)
    if len(data) < 4 or data[:2] != b"PK":
        raise PublicConnectorError("apk_not_zip")
    apk_sha = hashlib.sha256(data).hexdigest()
    results: list[dict[str, Any]] = []
    max_member = max_file_bytes * 8
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = 0
        if len(archive.infolist()) > 20_000:
            raise PublicConnectorError("apk_member_budget_exceeded")
        for info in archive.infolist():
            if info.is_dir() or info.file_size <= 0 or info.file_size > max_member:
                continue
            total += info.file_size
            if total > 512 * 1024 * 1024:
                raise PublicConnectorError("apk_uncompressed_budget_exceeded")
            name = info.filename.replace("\\", "/")
            if not (
                name.startswith(("assets/", "res/raw/", "META-INF/"))
                or name.lower().endswith((".json", ".xml", ".properties", ".txt", ".yml", ".yaml", ".ini", ".cfg", ".conf", ".md", ".js", ".ts", ".html", ".csv", ".env", ".pem", ".sql"))
            ):
                continue
            raw = archive.read(info)
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            if text.strip():
                results.extend(
                    _event_records(
                        text,
                        source_kind="真实检测结果",
                        channel="App",
                        platform="Android APK",
                        locator=f"apk:{source_name}@{apk_sha[:32]}:{name}",
                        hmac_key=hmac_key,
                        enabled_rule_ids=enabled_rule_ids,
                    )
                )
        for info in archive.infolist():
            if re.fullmatch(r"classes\d*\.dex", info.filename.rsplit("/", 1)[-1]) and info.file_size <= max_member:
                text = _dex_strings(archive.read(info))
                if text:
                    results.extend(
                        _event_records(
                            text,
                            source_kind="真实检测结果",
                            channel="App",
                            platform="Android APK",
                            locator=f"apk:{source_name}@{apk_sha[:32]}:{info.filename}#strings",
                            hmac_key=hmac_key,
                            enabled_rule_ids=enabled_rule_ids,
                        )
                    )
    return results


def _parse_wxapkg(data: bytes) -> list[tuple[str, bytes]]:
    if len(data) < 14 or data[0] != 0xBE:
        raise PublicConnectorError("wxapkg_encrypted_or_invalid")
    index_len, body_len, marker, count = struct.unpack(">IIBI", data[1:14])
    if marker != 0xED or count > 20_000 or len(data) < 14 + index_len + body_len:
        raise PublicConnectorError("wxapkg_header_invalid")
    pos = 14
    body_start = 14 + index_len
    entries: list[tuple[str, bytes]] = []
    for _ in range(count):
        if pos + 4 > len(data):
            raise PublicConnectorError("wxapkg_index_truncated")
        (name_len,) = struct.unpack(">I", data[pos:pos + 4])
        pos += 4
        if name_len > 4096 or pos + name_len + 8 > len(data):
            raise PublicConnectorError("wxapkg_entry_invalid")
        name = data[pos:pos + name_len].decode("utf-8", errors="replace")
        pos += name_len
        offset, size = struct.unpack(">II", data[pos:pos + 8])
        pos += 8
        if offset + size > body_len or body_start + offset + size > len(data):
            raise PublicConnectorError("wxapkg_entry_out_of_bounds")
        entries.append((name.replace("\\", "/").lstrip("/"), data[body_start + offset:body_start + offset + size]))
    if not entries:
        raise PublicConnectorError("wxapkg_empty")
    return entries


def collect_wxapkg(
    config: dict[str, Any],
    *,
    hmac_key: str,
    timeout_seconds: float,
    max_file_bytes: int,
    transport: Any | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    data, source_name = _artifact_bytes(config, suffix="wxapkg", timeout_seconds=timeout_seconds, transport=transport)
    package_sha = hashlib.sha256(data).hexdigest()
    results = ConnectorEvents(items_seen=0)
    for name, raw in _parse_wxapkg(data):
        if len(raw) == 0 or len(raw) > max_file_bytes * 8 or b"\x00" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            continue
        results.items_seen += 1
        results.extend(
            _event_records(
                text,
                source_kind="真实检测结果",
                channel="小程序",
                platform="未加密 wxapkg",
                locator=f"wxapkg:{source_name}@{package_sha[:32]}:{name}",
                hmac_key=hmac_key,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
    return results


def collect_extended(
    key: str,
    config: dict[str, Any],
    *,
    hmac_key: str,
    access_token: str = "",
    network_enabled: bool,
    timeout_seconds: float,
    max_file_bytes: int,
    checkpoint_store: Any | None = None,
    transport: Any | None = None,
    defer_checkpoints: bool = False,
    pending_checkpoints: list[tuple[str, str]] | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    network_keys = {
        "mediawiki",
        "generic_web",
        "stackoverflow",
        "codeberg",
        "weibo_authorized_api",
        "oci_registry",
        "npm_registry",
        "pypi_registry",
        "huggingface_repo",
        "crates_registry",
        "nuget_registry",
        "rubygems_registry",
    }
    if key in network_keys and not network_enabled:
        raise PublicConnectorError("network_connector_disabled")
    if key in {"apk", "wxapkg"} and str(config.get("url") or "").strip() and not network_enabled:
        raise PublicConnectorError("network_connector_disabled")
    if key == "mediawiki":
        return collect_mediawiki(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            transport=transport,
            checkpoint_store=checkpoint_store,
            defer_checkpoints=defer_checkpoints,
            pending_checkpoints=pending_checkpoints,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "generic_web":
        return collect_generic_web(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "stackoverflow":
        return collect_stackoverflow(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "codeberg":
        return collect_codeberg_repository(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            checkpoint_store=checkpoint_store,
            defer_checkpoints=defer_checkpoints,
            pending_checkpoints=pending_checkpoints,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "weibo_authorized_api":
        return collect_weibo_authorized_api(
            config,
            access_token=access_token,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "oci_registry":
        return collect_oci_registry(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "npm_registry":
        return collect_npm_registry(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "pypi_registry":
        return collect_pypi_registry(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "huggingface_repo":
        return collect_huggingface_repo(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "crates_registry":
        return collect_crates_registry(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "nuget_registry":
        return collect_nuget_registry(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "rubygems_registry":
        return collect_rubygems_registry(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "apk":
        return collect_apk(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    if key == "wxapkg":
        return collect_wxapkg(
            config,
            hmac_key=hmac_key,
            timeout_seconds=timeout_seconds,
            max_file_bytes=max_file_bytes,
            transport=transport,
            enabled_rule_ids=enabled_rule_ids,
        )
    raise PublicConnectorError("collector_not_supported")


EXTENDED_CONNECTOR_SPECS = (
    {
        "name": "mediawiki-public-api",
        "channel": "Wiki",
        "platform": "MediaWiki",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "recentchanges 增量 + 修订内容；仅公开 API",
    },
    {
        "name": "generic-web-rss",
        "channel": "知识分享",
        "platform": "RSS",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "RSS XML 解析、HTTPS 公网地址、条目与大小预算；复用 generic_web 采集器",
    },
    {
        "name": "generic-web-atom",
        "channel": "知识分享",
        "platform": "Atom",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "Atom XML 解析、HTTPS 公网地址、条目与大小预算；复用 generic_web 采集器",
    },
    {
        "name": "generic-web-sitemap",
        "channel": "知识分享",
        "platform": "Sitemap",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "Sitemap XML 与页面解析、HTTPS 公网地址、条目与大小预算；复用 generic_web 采集器",
    },
    {
        "name": "generic-web-url",
        "channel": "知识分享",
        "platform": "URL",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "单页面 HTML 解析、HTTPS 公网地址、大小预算；复用 generic_web 采集器",
    },
    {
        "name": "stackoverflow-public-api",
        "channel": "知识分享",
        "platform": "Stack Overflow",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "关键词搜索 + 问题正文；遵守 API 限流语义",
    },
    {
        "name": "weibo-authorized-api",
        "channel": "微博",
        "platform": "微博（官方 OAuth API）",
        "mode": "online-authorized",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "固定微博官方 OAuth API；仅读取明确授权的微博内容；Token 从加密运行设置读取",
    },
    {
        "name": "oci-registry",
        "channel": "容器镜像",
        "platform": "OCI Registry",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "OCI/Docker manifest、layer tar、whiteout 忽略",
    },
    {
        "name": "npm-registry",
        "channel": "包仓库",
        "platform": "npm Registry",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "只读 package metadata + tarball；静态文本扫描与大小预算",
    },
    {
        "name": "pypi-registry",
        "channel": "包仓库",
        "platform": "PyPI",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "只读 package metadata + sdist/wheel；不执行包代码",
    },
    {
        "name": "huggingface-public-repository",
        "channel": "AI 模型与数据",
        "platform": "Hugging Face Hub",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "只读公开仓库元数据与文本文件；文件数/字节数预算",
    },
    {
        "name": "crates-public-registry",
        "channel": "包仓库",
        "platform": "crates.io",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "只读 crate 元数据与源码归档；不执行 Rust 构建脚本",
    },
    {
        "name": "nuget-public-registry",
        "channel": "包仓库",
        "platform": "NuGet",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "只读 nupkg 压缩包；不执行 .NET 包代码",
    },
    {
        "name": "rubygems-public-registry",
        "channel": "包仓库",
        "platform": "RubyGems",
        "mode": "online-public",
        "network_enabled": False,
        "status": "已实现；默认关闭联网",
        "documentation": "只读 gem 元数据与 data.tar.gz；不执行 Ruby 代码",
    },
    {
        "name": "android-apk",
        "channel": "App",
        "platform": "Android APK",
        "mode": "authorized-artifact",
        "network_enabled": False,
        "status": "已实现；本地文件可离线运行",
        "documentation": "合法取得 APK 的 ZIP 文本成员与 DEX 字符串",
    },
    {
        "name": "wxapkg-unencrypted",
        "channel": "小程序",
        "platform": "未加密 wxapkg",
        "mode": "authorized-artifact",
        "network_enabled": False,
        "status": "已实现；仅未加密包",
        "documentation": "边界校验完整；加密包明确拒绝解密",
    },
)
