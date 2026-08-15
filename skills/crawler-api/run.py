#!/usr/bin/env python3

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 20
IMAGE_AWEME_TYPES = {68, 150}


class SkillError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def read_input() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SkillError("invalid_input", f"stdin 不是合法 JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise SkillError("invalid_input", "stdin 必须是 JSON 对象")
    return payload


def base_url() -> str:
    value = os.getenv("CRAWLER_BASE_URL", "").strip().rstrip("/")
    if not value:
        raise SkillError("missing_config", "缺少环境变量 CRAWLER_BASE_URL")

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SkillError("invalid_config", "CRAWLER_BASE_URL 必须是 http/https 服务根地址")
    return value


def request_json(path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    url = f"{base_url()}{path}"
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"

    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SkillError("request_failed", f"Crawler HTTP 请求失败: {exc.code}", http_status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SkillError("request_failed", f"Crawler 服务不可达: {exc}") from exc

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillError("request_failed", "Crawler 返回的不是合法 JSON", http_status=status) from exc


def require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillError("invalid_input", f"{key} 必须是非空字符串")
    return value.strip()


def require_int(payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkillError("invalid_input", f"{key} 必须是整数")
    if value < minimum or value > maximum:
        raise SkillError("invalid_input", f"{key} 必须在 {minimum}..{maximum} 范围内")
    return value


def check_crawler_response(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise SkillError("request_failed", "Crawler 返回结构不是 JSON 对象")

    code = payload.get("code")
    if code != 0:
        message = str(payload.get("msg") or "Crawler 返回业务错误")
        raise SkillError("crawler_error", message, crawler_code=code)
    return payload.get("data")


def iter_awemes(value: Any):
    if isinstance(value, dict):
        aweme_info = value.get("aweme_info")
        if isinstance(aweme_info, dict):
            yield aweme_info

        if "aweme_id" in value and any(key in value for key in ("desc", "images", "video", "author")):
            yield value

        for child in value.values():
            yield from iter_awemes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_awemes(child)


def media_kind(aweme: dict[str, Any]) -> str:
    images = aweme.get("images")
    if isinstance(images, list) and images:
        return "image"

    aweme_type = aweme.get("aweme_type")
    if isinstance(aweme_type, int) and aweme_type in IMAGE_AWEME_TYPES:
        return "image"

    if isinstance(aweme.get("video"), dict):
        return "video"
    return "unknown"


def normalize_awemes(data: Any, requested_kind: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for aweme in iter_awemes(data):
        aweme_id = str(aweme.get("aweme_id") or "").strip()
        if not aweme_id or aweme_id in seen:
            continue

        kind = media_kind(aweme)
        if requested_kind != "all" and kind != requested_kind:
            continue

        author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
        images = aweme.get("images") if isinstance(aweme.get("images"), list) else []
        share_info = aweme.get("share_info") if isinstance(aweme.get("share_info"), dict) else {}

        items.append(
            {
                "aweme_id": aweme_id,
                "description": str(aweme.get("desc") or ""),
                "kind": kind,
                "image_count": len(images),
                "author": {
                    "nickname": str(author.get("nickname") or ""),
                    "uid": str(author.get("uid") or ""),
                    "sec_uid": str(author.get("sec_uid") or ""),
                },
                "share_url": str(share_info.get("share_url") or aweme.get("share_url") or ""),
            }
        )
        seen.add(aweme_id)

    return items


def action_status() -> dict[str, Any]:
    status, payload = request_json("/openapi.json")
    if not isinstance(payload, dict):
        raise SkillError("request_failed", "OpenAPI 返回结构不是 JSON 对象", http_status=status)

    paths = payload.get("paths") if isinstance(payload.get("paths"), dict) else {}
    expected = ["/douyin/search", "/douyin/detail", "/douyin/account_list", "/douyin/add_account"]
    return {
        "ok": True,
        "http_status": status,
        "base_url": base_url(),
        "douyin_endpoints": {path: path in paths for path in expected},
    }


def action_douyin_search(payload: dict[str, Any]) -> dict[str, Any]:
    keyword = require_text(payload, "keyword")
    offset = require_int(payload, "offset", 0, 0, 10000)
    limit = require_int(payload, "limit", 10, 1, 50)
    kind = payload.get("kind", "all")
    if kind not in {"all", "image", "video"}:
        raise SkillError("invalid_input", "kind 只能是 all、image 或 video")

    status, response = request_json(
        "/douyin/search",
        {"keyword": keyword, "offset": offset, "limit": limit},
    )
    data = check_crawler_response(response)
    items = normalize_awemes(data, kind)
    return {
        "ok": True,
        "http_status": status,
        "keyword": keyword,
        "offset": offset,
        "limit": limit,
        "kind": kind,
        "count": len(items),
        "items": items,
    }


def action_douyin_detail(payload: dict[str, Any]) -> dict[str, Any]:
    aweme_id = require_text(payload, "id")
    status, response = request_json("/douyin/detail", {"id": aweme_id})
    data = check_crawler_response(response)
    return {
        "ok": True,
        "http_status": status,
        "id": aweme_id,
        "data": data,
    }


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("skill_action")
    if action == "status":
        return action_status()
    if action == "douyin_search":
        return action_douyin_search(payload)
    if action == "douyin_detail":
        return action_douyin_detail(payload)
    raise SkillError("invalid_input", "未知 skill_action")


def main() -> int:
    try:
        result = dispatch(read_input())
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except SkillError as exc:
        error = {"ok": False, "code": exc.code, "message": exc.message}
        error.update(exc.details)
        print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
