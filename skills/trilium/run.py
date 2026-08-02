#!/usr/bin/env python3
"""Portable Trilium ETAPI helper for the AgentDock Trilium Skill."""
from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import string
import sys
from collections import deque
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SKILL_VERSION = "1.1.1"
UPSTREAM_REPOSITORY = "https://github.com/TriliumNext/Trilium"
DEFAULT_TIMEOUT = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ENTITY_ID_ALPHABET = string.ascii_letters + string.digits
BACKUP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_SUBTREE_DEPTH = 5
MAX_CHILDREN_PER_LEVEL = 10
DEFAULT_SUBTREE_NODE_LIMIT = 100
MAX_SUBTREE_NODE_LIMIT = 500
MAX_HIERARCHY_VALIDATION_NODES = 5000
PROTECTED_SYSTEM_NOTES = {
    "root",
    "_hidden",
    "_share",
    "_lbRoot",
    "_lbAvailableLaunchers",
    "_lbVisibleLaunchers",
    "_globalNoteMap",
}
AUTO_LINK_ATTRIBUTES = {
    ("relation", "internalLink"),
    ("relation", "imageLink"),
    ("relation", "relationMapLink"),
    ("relation", "includeNoteLink"),
    ("label", "internalBookmark"),
}
DANGEROUS_ATTRIBUTES = {
    ("label", "run"),
    ("label", "customrequesthandler"),
    ("label", "customresourceprovider"),
    ("label", "widget"),
    ("label", "shareraw"),
    ("label", "titletemplate"),
    ("label", "webviewsrc"),
    ("label", "iconpack"),
    ("label", "docname"),
    ("label", "docurl"),
    ("relation", "runonnotecreation"),
    ("relation", "runonnotetitlechange"),
    ("relation", "runonnotechange"),
    ("relation", "runonnotecontentchange"),
    ("relation", "runonnotedeletion"),
    ("relation", "runonbranchcreation"),
    ("relation", "runonbranchchange"),
    ("relation", "runonbranchdeletion"),
    ("relation", "runonchildnotecreation"),
    ("relation", "runonattributecreation"),
    ("relation", "runonattributechange"),
    ("relation", "widget"),
    ("relation", "rendernote"),
    ("relation", "sharejs"),
    ("relation", "sharehtml"),
    ("relation", "sharetemplate"),
}


class TriliumError(RuntimeError):
    def __init__(self, message: str, *, code: str = "trilium_error", details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def emit(payload: dict[str, Any], *, exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def load_input() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TriliumError(f"Invalid JSON input: {exc}", code="bad_json") from exc
    if not isinstance(payload, dict):
        raise TriliumError("Skill input must be a JSON object.", code="bad_json")
    return payload


def normalize_api_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise TriliumError("base_url must use http or https.", code="bad_base_url")
    if not parsed.hostname:
        raise TriliumError("base_url must include a host.", code="bad_base_url")
    if parsed.username or parsed.password:
        raise TriliumError("base_url must not contain credentials.", code="bad_base_url")
    if parsed.query or parsed.fragment:
        raise TriliumError("base_url must not contain query or fragment.", code="bad_base_url")

    path = parsed.path.rstrip("/")
    if not path.endswith("/etapi"):
        path += "/etapi"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_api_base_url(payload: dict[str, Any]) -> str:
    value = payload.get("base_url") or os.environ.get("TRILIUM_URL")
    if not value:
        raise TriliumError("TRILIUM_URL is not configured.", code="missing_base_url")
    if not isinstance(value, str):
        raise TriliumError("base_url must be a string.", code="bad_base_url")
    return normalize_api_base_url(value)


def resolve_token() -> str:
    token = os.environ.get("TRILIUM_ETAPI_TOKEN", "")
    if not token:
        raise TriliumError("TRILIUM_ETAPI_TOKEN is not configured.", code="missing_token")
    return token


def timeout_from(payload: dict[str, Any]) -> int:
    value = payload.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 120:
        raise TriliumError("timeout must be an integer between 1 and 120.", code="bad_timeout")
    return value


def redact(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key.lower() in {"authorization", "token", "password", "auth_token"}:
                result[key] = "<redacted>"
            else:
                result[key] = redact(child, secret)
        return result
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "<redacted>")
    return value


def is_textual_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return (
        media_type.startswith("text/")
        or media_type in {
            "application/json",
            "application/javascript",
            "application/xml",
            "application/xhtml+xml",
            "application/svg+xml",
        }
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def parse_response(raw: bytes, content_type: str, *, preserve_text: bool = False) -> Any:
    if not raw:
        if preserve_text and is_textual_content_type(content_type):
            return ""
        return None
    if not is_textual_content_type(content_type):
        return {
            "binary": True,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(raw),
        }

    text = raw.decode("utf-8", errors="replace")
    if preserve_text:
        return text
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json") or text[:1] in {"{", "["}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def request_trilium(
    payload: dict[str, Any],
    endpoint: str,
    *,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    text_body: str | None = None,
    preserve_text: bool = False,
) -> dict[str, Any]:
    if not endpoint.startswith("/") or ".." in endpoint:
        raise TriliumError("Invalid ETAPI endpoint.", code="bad_endpoint")
    if json_body is not None and text_body is not None:
        raise TriliumError("A request cannot use both JSON and text bodies.", code="bad_request")

    api_base_url = resolve_api_base_url(payload)
    token = resolve_token()
    url = api_base_url + endpoint
    # Trilium 的布尔查询参数严格只接受小写 true/false，不能直接使用
    # urllib 对 Python bool 的默认字符串化结果 True/False。
    compact_query = {
        key: ("true" if value else "false") if isinstance(value, bool) else value
        for key, value in (query or {}).items()
        if value is not None
    }
    if compact_query:
        url += "?" + urlencode(compact_query, doseq=True)

    headers = {
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.5",
        "Authorization": token,
        "User-Agent": f"AgentDock-Trilium-Skill/{SKILL_VERSION}",
    }
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif text_body is not None:
        data = text_body.encode("utf-8")
        headers["Content-Type"] = "text/plain; charset=utf-8"

    request = Request(url, data=data, headers=headers, method=method.upper())
    context = ssl._create_unverified_context() if os.environ.get("TRILIUM_INSECURE_TLS") == "1" else None

    try:
        with urlopen(request, timeout=timeout_from(payload), context=context) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as exc:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        status = exc.code
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
    except URLError as exc:
        raise TriliumError(f"Cannot reach Trilium ETAPI: {exc.reason}", code="network_error") from exc
    except TimeoutError as exc:
        raise TriliumError("Trilium ETAPI request timed out.", code="network_timeout") from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise TriliumError("Trilium ETAPI response exceeded 8 MiB.", code="response_too_large")

    response_payload = redact(parse_response(raw, content_type, preserve_text=preserve_text), token)
    return {
        "success": 200 <= status < 300,
        "service": "trilium",
        "api_base_url": api_base_url,
        "endpoint": endpoint,
        "http_status": status,
        "response": response_payload,
    }


def require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise TriliumError(f"Missing required field: {key}.", code="missing_field", details={"field": key})
    return payload[key]


def require_string(payload: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = require(payload, key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise TriliumError(f"{key} must be {qualifier}.", code="bad_field", details={"field": key})
    return value if allow_empty else value.strip()


def optional_string(payload: dict[str, Any], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, str):
        raise TriliumError(f"{key} must be a string.", code="bad_field", details={"field": key})
    return value


def optional_int(payload: dict[str, Any], key: str, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if key not in payload:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TriliumError(f"{key} must be an integer.", code="bad_field", details={"field": key})
    if minimum is not None and value < minimum:
        raise TriliumError(f"{key} must be at least {minimum}.", code="bad_field", details={"field": key})
    if maximum is not None and value > maximum:
        raise TriliumError(f"{key} must be at most {maximum}.", code="bad_field", details={"field": key})
    return value


def path_id(payload: dict[str, Any], key: str) -> str:
    return quote(require_string(payload, key), safe="")


def copy_fields(payload: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for source, target in mapping.items():
        if source in payload:
            body[target] = payload[source]
    return body


def require_confirmed(payload: dict[str, Any], action: str) -> None:
    if payload.get("confirm") is not True:
        raise TriliumError(
            f"{action} requires confirm: true.",
            code="confirmation_required",
            details={"action": action},
        )


def generated_entity_id() -> str:
    return "".join(secrets.choice(ENTITY_ID_ALPHABET) for _ in range(12))


def unwrap_object(result: dict[str, Any], context: str) -> dict[str, Any]:
    if not result.get("success"):
        raise TriliumError(
            f"{context} failed.",
            code="upstream_error",
            details={
                "endpoint": result.get("endpoint"),
                "http_status": result.get("http_status"),
                "response": result.get("response"),
            },
        )
    response = result.get("response")
    if not isinstance(response, dict):
        raise TriliumError(f"{context} returned an unexpected response.", code="bad_upstream_response")
    return response


def unwrap_text(result: dict[str, Any], context: str) -> str:
    if not result.get("success"):
        raise TriliumError(
            f"{context} failed.",
            code="upstream_error",
            details={
                "endpoint": result.get("endpoint"),
                "http_status": result.get("http_status"),
                "response": result.get("response"),
            },
        )
    response = result.get("response")
    if not isinstance(response, str):
        raise TriliumError(f"{context} requires textual note content.", code="binary_content")
    return response


def get_note_object(payload: dict[str, Any], note_id: str, context: str = "Read note") -> dict[str, Any]:
    result = request_trilium(payload, f"/notes/{quote(note_id, safe='')}")
    return unwrap_object(result, context)


def get_attribute_object(payload: dict[str, Any], attribute_id: str, context: str = "Read attribute") -> dict[str, Any]:
    result = request_trilium(payload, f"/attributes/{quote(attribute_id, safe='')}")
    return unwrap_object(result, context)


def note_change_marker(note: dict[str, Any]) -> tuple[Any, Any]:
    return note.get("blobId"), note.get("utcDateModified")


def ensure_note_unchanged(payload: dict[str, Any], note_id: str, initial_note: dict[str, Any]) -> None:
    current_note = get_note_object(payload, note_id, "Re-read note before update")
    if note_change_marker(current_note) != note_change_marker(initial_note):
        raise TriliumError(
            "The note changed while preparing the update; no content was written.",
            code="concurrent_modification",
            details={"note_id": note_id},
        )


def validate_attribute_type(attribute_type: str) -> None:
    if attribute_type not in {"label", "relation"}:
        raise TriliumError("type must be label or relation.", code="bad_field", details={"field": "type"})


def validate_attribute_safety(attribute_type: str, name: str) -> None:
    if (attribute_type, name.strip().lower()) in DANGEROUS_ATTRIBUTES:
        raise TriliumError(
            f"Attribute '{name}' is potentially dangerous and cannot be set by this Skill.",
            code="dangerous_attribute",
            details={"type": attribute_type, "name": name},
        )


def ensure_writable_note(note: dict[str, Any], context: str) -> None:
    if note.get("isProtected") is True:
        raise TriliumError(f"Protected notes cannot be modified during {context}.", code="protected_note")


def validate_attribute_value(payload: dict[str, Any], attribute_type: str, value: str) -> None:
    if attribute_type != "relation":
        return
    if not value:
        raise TriliumError(
            "relation attributes require a non-empty target note ID.",
            code="bad_field",
            details={"field": "value"},
        )
    get_note_object(payload, value, "Read relation target")


def is_auto_link_attribute(attribute: dict[str, Any]) -> bool:
    attribute_type = attribute.get("type")
    name = attribute.get("name")
    return isinstance(attribute_type, str) and isinstance(name, str) and (attribute_type, name) in AUTO_LINK_ATTRIBUTES


def ensure_movable_note(note_id: str) -> None:
    if note_id in PROTECTED_SYSTEM_NOTES:
        raise TriliumError("System notes cannot be moved or cloned.", code="protected_system_note")


def ensure_parent_outside_subtree(
    payload: dict[str, Any],
    source_note: dict[str, Any],
    target_parent_note_id: str,
) -> None:
    source_note_id = source_note.get("noteId")
    child_ids = source_note.get("childNoteIds")
    if not isinstance(source_note_id, str) or not source_note_id:
        raise TriliumError("Trilium returned an invalid source note ID.", code="bad_upstream_response")
    if not isinstance(child_ids, list) or not all(isinstance(item, str) for item in child_ids):
        raise TriliumError("Trilium returned invalid childNoteIds.", code="bad_upstream_response")

    pending = deque(child_ids)
    visited = {source_note_id}
    inspected = 0
    while pending:
        child_id = pending.popleft()
        if child_id in visited:
            continue
        if child_id == target_parent_note_id:
            raise TriliumError(
                "Moving or cloning the note under this parent would create a cycle.",
                code="hierarchy_cycle",
                details={"note_id": source_note_id, "parent_note_id": target_parent_note_id},
            )

        visited.add(child_id)
        inspected += 1
        if inspected > MAX_HIERARCHY_VALIDATION_NODES:
            raise TriliumError(
                "The subtree is too large to validate safely through ETAPI; no branch was written.",
                code="hierarchy_validation_limit",
                details={"max_nodes": MAX_HIERARCHY_VALIDATION_NODES},
            )

        child = get_note_object(payload, child_id, "Validate branch destination")
        descendant_ids = child.get("childNoteIds")
        if not isinstance(descendant_ids, list) or not all(isinstance(item, str) for item in descendant_ids):
            raise TriliumError("Trilium returned invalid childNoteIds.", code="bad_upstream_response")
        pending.extend(descendant_ids)


def validate_branch_destination(
    payload: dict[str, Any],
    source_note: dict[str, Any],
    target_parent: dict[str, Any],
) -> None:
    source_note_id = source_note.get("noteId")
    target_parent_note_id = target_parent.get("noteId")
    if not isinstance(source_note_id, str) or not source_note_id:
        raise TriliumError("Trilium returned an invalid source note ID.", code="bad_upstream_response")
    if not isinstance(target_parent_note_id, str) or not target_parent_note_id:
        raise TriliumError("Trilium returned an invalid parent note ID.", code="bad_upstream_response")

    ensure_movable_note(source_note_id)
    ensure_writable_note(source_note, "branch creation")
    ensure_writable_note(target_parent, "branch creation")
    if target_parent_note_id == "none" or source_note_id == target_parent_note_id:
        raise TriliumError("A note cannot be placed under this parent.", code="invalid_branch_parent")
    if target_parent_note_id != "_lbBookmarks" and target_parent.get("type") == "launcher":
        raise TriliumError("Launcher notes cannot have children.", code="invalid_branch_parent")

    ensure_parent_outside_subtree(payload, source_note, target_parent_note_id)


def op_status(payload: dict[str, Any]) -> dict[str, Any]:
    base_value = payload.get("base_url") or os.environ.get("TRILIUM_URL")
    token_present = bool(os.environ.get("TRILIUM_ETAPI_TOKEN"))
    missing = []
    if not base_value:
        missing.append("TRILIUM_URL")
    if not token_present:
        missing.append("TRILIUM_ETAPI_TOKEN")

    result: dict[str, Any] = {
        "success": True,
        "service": "trilium",
        "skill_version": SKILL_VERSION,
        "upstream": UPSTREAM_REPOSITORY,
        "configured": not missing,
        "ready": False,
        "missing_environment": missing,
    }
    if missing:
        return result

    api_result = request_trilium(payload, "/app-info")
    result.update(
        {
            "success": api_result["success"],
            "ready": api_result["success"],
            "api_base_url": api_result["api_base_url"],
            "http_status": api_result["http_status"],
            "app_info": api_result["response"],
        }
    )
    return result


def op_search_notes(payload: dict[str, Any]) -> dict[str, Any]:
    query_text = require_string(payload, "query")
    query: dict[str, Any] = {"search": query_text}
    for source, target in {
        "fast_search": "fastSearch",
        "include_archived_notes": "includeArchivedNotes",
        "ancestor_note_id": "ancestorNoteId",
        "ancestor_depth": "ancestorDepth",
        "order_by": "orderBy",
        "order_direction": "orderDirection",
        "debug": "debug",
    }.items():
        if source in payload:
            value = payload[source]
            if source in {"fast_search", "include_archived_notes", "debug"} and not isinstance(value, bool):
                raise TriliumError(f"{source} must be a boolean.", code="bad_field", details={"field": source})
            query[target] = value
    limit = optional_int(payload, "limit", minimum=1, maximum=1000)
    if limit is not None:
        query["limit"] = limit
    return request_trilium(payload, "/notes", query=query)


def op_get_note(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/notes/{path_id(payload, 'note_id')}")


def op_get_note_content(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/notes/{path_id(payload, 'note_id')}/content", preserve_text=True)


def op_create_note(payload: dict[str, Any]) -> dict[str, Any]:
    parent_note_id = require_string(payload, "parent_note_id")
    parent = get_note_object(payload, parent_note_id, "Read parent note")
    ensure_writable_note(parent, "note creation")
    if parent_note_id != "_lbBookmarks" and parent.get("type") == "launcher":
        raise TriliumError("Launcher notes cannot have children.", code="invalid_branch_parent")

    body = {
        "parentNoteId": parent_note_id,
        "title": require_string(payload, "title"),
        "type": require_string(payload, "type"),
        "content": require_string(payload, "content", allow_empty=True) if "content" in payload else "",
    }
    body.update(
        copy_fields(
            payload,
            {
                "mime": "mime",
                "note_position": "notePosition",
                "prefix": "prefix",
                "is_expanded": "isExpanded",
                "note_id": "noteId",
                "date_created": "dateCreated",
                "utc_date_created": "utcDateCreated",
            },
        )
    )
    return request_trilium(payload, "/create-note", method="POST", json_body=body)


def op_update_note(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = path_id(payload, "note_id")
    body = copy_fields(
        payload,
        {
            "title": "title",
            "type": "type",
            "mime": "mime",
            "date_created": "dateCreated",
            "utc_date_created": "utcDateCreated",
        },
    )
    if not body:
        raise TriliumError("update-note requires at least one editable field.", code="missing_changes")
    return request_trilium(payload, f"/notes/{note_id}", method="PATCH", json_body=body)


def op_set_note_content(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = path_id(payload, "note_id")
    content = require_string(payload, "content", allow_empty=True)
    return request_trilium(payload, f"/notes/{note_id}/content", method="PUT", text_body=content)


def op_append_note_content(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    content = require_string(payload, "content")
    note = get_note_object(payload, note_id)
    if note.get("isProtected") is True:
        raise TriliumError("Protected note content cannot be modified.", code="protected_note")

    existing = unwrap_text(
        request_trilium(payload, f"/notes/{quote(note_id, safe='')}/content", preserve_text=True),
        "Read note content",
    )
    if note.get("type") == "text":
        combined = existing + content
    else:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        combined = existing + separator + content

    # ETAPI 的 PUT 会自动保存 Revision。写入前再读一次元数据，避免在
    # 读取和写回之间静默覆盖其他客户端刚完成的修改。
    ensure_note_unchanged(payload, note_id, note)
    written = request_trilium(
        payload,
        f"/notes/{quote(note_id, safe='')}/content",
        method="PUT",
        text_body=combined,
    )
    if not written["success"]:
        return written
    return {
        "success": True,
        "service": "trilium",
        "action": "append-note-content",
        "note_id": note_id,
        "title": note.get("title"),
        "type": note.get("type"),
        "content": combined,
        "http_status": written["http_status"],
    }


def op_edit_note_content(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    edits = require(payload, "edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
        raise TriliumError("edits must be an array containing 1 to 50 edits.", code="bad_field", details={"field": "edits"})

    note = get_note_object(payload, note_id)
    if note.get("isProtected") is True:
        raise TriliumError("Protected note content cannot be modified.", code="protected_note")
    if note.get("type") == "text":
        raise TriliumError(
            "edit-note-content does not support rich-text notes; use set-note-content with HTML.",
            code="unsupported_note_type",
        )

    content = unwrap_text(
        request_trilium(payload, f"/notes/{quote(note_id, safe='')}/content", preserve_text=True),
        "Read note content",
    )
    updated = content
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise TriliumError("Each edit must be a JSON object.", code="bad_field", details={"field": f"edits[{index}]"})
        old_text = edit.get("old_text")
        new_text = edit.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise TriliumError("old_text must be a non-empty string.", code="bad_field", details={"field": f"edits[{index}].old_text"})
        if not isinstance(new_text, str):
            raise TriliumError("new_text must be a string.", code="bad_field", details={"field": f"edits[{index}].new_text"})

        occurrences = updated.count(old_text)
        if occurrences == 0:
            raise TriliumError(
                "old_text was not found; no content was written.",
                code="edit_text_not_found",
                details={"edit_index": index},
            )
        if occurrences > 1:
            raise TriliumError(
                "old_text is not unique; include more surrounding context.",
                code="edit_text_not_unique",
                details={"edit_index": index, "occurrences": occurrences},
            )
        updated = updated.replace(old_text, new_text, 1)

    ensure_note_unchanged(payload, note_id, note)
    written = request_trilium(
        payload,
        f"/notes/{quote(note_id, safe='')}/content",
        method="PUT",
        text_body=updated,
    )
    if not written["success"]:
        return written
    return {
        "success": True,
        "service": "trilium",
        "action": "edit-note-content",
        "note_id": note_id,
        "title": note.get("title"),
        "edits_applied": len(edits),
        "content": updated,
        "http_status": written["http_status"],
    }


def op_delete_note(payload: dict[str, Any]) -> dict[str, Any]:
    require_confirmed(payload, "delete-note")
    raw_note_id = require_string(payload, "note_id")
    ensure_movable_note(raw_note_id)
    note_id = quote(raw_note_id, safe="")
    confirm_title = require_string(payload, "confirm_title")

    target = request_trilium(payload, f"/notes/{note_id}")
    if not target["success"]:
        return target
    target_payload = target["response"]
    if not isinstance(target_payload, dict):
        raise TriliumError("Trilium returned an invalid note response.", code="bad_upstream_response")
    ensure_writable_note(target_payload, "note deletion")
    current_title = target_payload.get("title")
    if current_title != confirm_title:
        raise TriliumError(
            "confirm_title does not match the current note title.",
            code="confirmation_mismatch",
            details={"expected_title": current_title},
        )

    deleted = request_trilium(payload, f"/notes/{note_id}", method="DELETE")
    return {
        "success": deleted["success"],
        "service": "trilium",
        "action": "delete-note",
        "target": {"note_id": require_string(payload, "note_id"), "title": current_title},
        "http_status": deleted["http_status"],
        "response": deleted["response"],
    }


def op_note_history(payload: dict[str, Any]) -> dict[str, Any]:
    query = {"ancestorNoteId": payload.get("ancestor_note_id")}
    return request_trilium(payload, "/notes/history", query=query)


def op_list_note_revisions(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/notes/{path_id(payload, 'note_id')}/revisions")


def op_get_revision(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/revisions/{path_id(payload, 'revision_id')}")


def op_get_revision_content(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/revisions/{path_id(payload, 'revision_id')}/content", preserve_text=True)


def op_create_revision(payload: dict[str, Any]) -> dict[str, Any]:
    body = {"description": optional_string(payload, "description") or ""}
    return request_trilium(payload, f"/notes/{path_id(payload, 'note_id')}/revision", method="POST", json_body=body)


def op_undelete_note(payload: dict[str, Any]) -> dict[str, Any]:
    require_confirmed(payload, "undelete-note")
    note_id = require_string(payload, "note_id")
    if require_string(payload, "confirm_note_id") != note_id:
        raise TriliumError("confirm_note_id does not match note_id.", code="confirmation_mismatch")
    return request_trilium(payload, f"/notes/{quote(note_id, safe='')}/undelete", method="POST", json_body={})


def op_get_child_notes(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    limit = optional_int(payload, "limit", minimum=1, maximum=500) or 100
    parent = get_note_object(payload, note_id)
    child_ids = parent.get("childNoteIds")
    if not isinstance(child_ids, list) or not all(isinstance(item, str) for item in child_ids):
        raise TriliumError("Trilium returned invalid childNoteIds.", code="bad_upstream_response")

    children = []
    for child_id in child_ids[:limit]:
        child = get_note_object(payload, child_id, "Read child note")
        grandchild_ids = child.get("childNoteIds")
        child_count = len(grandchild_ids) if isinstance(grandchild_ids, list) else 0
        children.append(
            {
                "note_id": child.get("noteId"),
                "title": child.get("title"),
                "type": child.get("type"),
                "child_count": child_count,
            }
        )

    return {
        "success": True,
        "service": "trilium",
        "action": "get-child-notes",
        "note_id": note_id,
        "children": children,
        "total_children": len(child_ids),
        "truncated": len(child_ids) > limit,
    }


def build_subtree(
    payload: dict[str, Any],
    note_id: str,
    *,
    current_depth: int,
    max_depth: int,
    state: dict[str, int],
    ancestors: set[str],
) -> dict[str, Any]:
    if state["count"] >= state["limit"]:
        return {"note_id": "", "title": "... node limit reached", "type": "truncated"}

    state["count"] += 1
    note = get_note_object(payload, note_id, "Read subtree note")
    child_ids = note.get("childNoteIds")
    if not isinstance(child_ids, list) or not all(isinstance(item, str) for item in child_ids):
        raise TriliumError("Trilium returned invalid childNoteIds.", code="bad_upstream_response")

    node: dict[str, Any] = {
        "note_id": note.get("noteId"),
        "title": note.get("title"),
        "type": note.get("type"),
    }
    if current_depth >= max_depth:
        if child_ids:
            node["children"] = f"{len(child_ids)} children not shown (depth limit reached)"
        return node
    if not child_ids:
        return node

    children: list[dict[str, Any]] = []
    for child_id in child_ids[:MAX_CHILDREN_PER_LEVEL]:
        if state["count"] >= state["limit"]:
            children.append({"note_id": "", "title": "... node limit reached", "type": "truncated"})
            break
        if child_id in ancestors:
            children.append({"note_id": child_id, "title": "... cycle detected", "type": "truncated"})
            continue
        children.append(
            build_subtree(
                payload,
                child_id,
                current_depth=current_depth + 1,
                max_depth=max_depth,
                state=state,
                ancestors=ancestors | {child_id},
            )
        )
    if len(child_ids) > MAX_CHILDREN_PER_LEVEL:
        children.append(
            {
                "note_id": "",
                "title": f"... and {len(child_ids) - MAX_CHILDREN_PER_LEVEL} more",
                "type": "truncated",
            }
        )
    node["children"] = children
    return node


def op_get_subtree(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    depth = optional_int(payload, "depth", minimum=1, maximum=MAX_SUBTREE_DEPTH) or 2
    node_limit = optional_int(payload, "node_limit", minimum=1, maximum=MAX_SUBTREE_NODE_LIMIT) or DEFAULT_SUBTREE_NODE_LIMIT
    state = {"count": 0, "limit": node_limit}
    subtree = build_subtree(
        payload,
        note_id,
        current_depth=0,
        max_depth=depth,
        state=state,
        ancestors={note_id},
    )
    return {
        "success": True,
        "service": "trilium",
        "action": "get-subtree",
        "depth": depth,
        "node_limit": node_limit,
        "nodes_read": state["count"],
        "subtree": subtree,
    }


def op_get_branch(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/branches/{path_id(payload, 'branch_id')}")


def op_create_branch(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    parent_note_id = require_string(payload, "parent_note_id")
    note = get_note_object(payload, note_id)
    parent = get_note_object(payload, parent_note_id, "Read branch target parent")
    validate_branch_destination(payload, note, parent)

    body = {"noteId": note_id, "parentNoteId": parent_note_id}
    body.update(copy_fields(payload, {"note_position": "notePosition", "prefix": "prefix", "is_expanded": "isExpanded"}))
    return request_trilium(payload, "/branches", method="POST", json_body=body)


def op_clone_note(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    parent_note_id = require_string(payload, "parent_note_id")
    note = get_note_object(payload, note_id)
    parent = get_note_object(payload, parent_note_id, "Read clone target parent")
    validate_branch_destination(payload, note, parent)

    parent_ids = note.get("parentNoteIds")
    if not isinstance(parent_ids, list) or not all(isinstance(item, str) for item in parent_ids):
        raise TriliumError("Trilium returned invalid parentNoteIds.", code="bad_upstream_response")
    if parent_note_id in parent_ids:
        raise TriliumError("The note already exists under this parent.", code="already_cloned")

    body: dict[str, Any] = {"noteId": note_id, "parentNoteId": parent_note_id}
    body.update(copy_fields(payload, {"prefix": "prefix", "note_position": "notePosition", "is_expanded": "isExpanded"}))
    created = request_trilium(payload, "/branches", method="POST", json_body=body)
    branch = unwrap_object(created, "Clone note")

    verified = get_note_object(payload, note_id, "Verify cloned note")
    verified_parent_ids = verified.get("parentNoteIds")
    verified_branch_ids = verified.get("parentBranchIds")
    branch_id = branch.get("branchId")
    clone_verified = (
        isinstance(verified_parent_ids, list)
        and parent_note_id in verified_parent_ids
        and isinstance(verified_branch_ids, list)
        and isinstance(branch_id, str)
        and branch_id in verified_branch_ids
    )
    return {
        "success": clone_verified,
        "service": "trilium",
        "action": "clone-note",
        "note_id": note_id,
        "title": note.get("title"),
        "parent_note_id": parent_note_id,
        "parent_title": parent.get("title"),
        "branch": branch,
        **({} if clone_verified else {"code": "clone_verification_failed"}),
    }


def op_move_note(payload: dict[str, Any]) -> dict[str, Any]:
    require_confirmed(payload, "move-note")
    note_id = require_string(payload, "note_id")
    new_parent_note_id = require_string(payload, "new_parent_note_id")
    ensure_movable_note(note_id)
    if note_id == new_parent_note_id:
        raise TriliumError("A note cannot be moved under itself.", code="invalid_move")

    note = get_note_object(payload, note_id)
    if note.get("title") != require_string(payload, "confirm_title"):
        raise TriliumError(
            "confirm_title does not match the current note title.",
            code="confirmation_mismatch",
            details={"expected_title": note.get("title")},
        )
    target_parent = get_note_object(payload, new_parent_note_id, "Read move target parent")

    branch_ids = note.get("parentBranchIds")
    if not isinstance(branch_ids, list) or not all(isinstance(item, str) for item in branch_ids) or not branch_ids:
        raise TriliumError("The note has no valid parent branches.", code="missing_parent_branch")
    requested_branch_id = optional_string(payload, "branch_id")
    if requested_branch_id:
        if requested_branch_id not in branch_ids:
            raise TriliumError("branch_id does not belong to note_id.", code="branch_mismatch")
        branch_id = requested_branch_id
    elif len(branch_ids) == 1:
        branch_id = branch_ids[0]
    else:
        raise TriliumError(
            "The note has multiple parent branches; specify branch_id to choose which location to move.",
            code="ambiguous_branch",
            details={"parent_branch_ids": branch_ids},
        )

    old_branch = unwrap_object(
        request_trilium(payload, f"/branches/{quote(branch_id, safe='')}"),
        "Read source branch",
    )
    if old_branch.get("noteId") != note_id:
        raise TriliumError("The selected branch does not belong to the note.", code="branch_mismatch")
    if old_branch.get("parentNoteId") == new_parent_note_id:
        return {
            "success": True,
            "service": "trilium",
            "action": "move-note",
            "moved": False,
            "reason": "already_under_target_parent",
            "note_id": note_id,
            "branch_id": branch_id,
        }

    parent_ids = note.get("parentNoteIds")
    if not isinstance(parent_ids, list) or not all(isinstance(item, str) for item in parent_ids):
        raise TriliumError("Trilium returned invalid parentNoteIds.", code="bad_upstream_response")
    if new_parent_note_id in parent_ids:
        raise TriliumError(
            "The note already has another branch under the target parent; remove the source branch explicitly instead.",
            code="already_under_target_parent",
            details={"note_id": note_id, "parent_note_id": new_parent_note_id},
        )
    validate_branch_destination(payload, note, target_parent)

    body: dict[str, Any] = {
        "noteId": note_id,
        "parentNoteId": new_parent_note_id,
        "prefix": old_branch.get("prefix", ""),
        "notePosition": old_branch.get("notePosition", 0),
        "isExpanded": old_branch.get("isExpanded", False),
    }
    body.update(copy_fields(payload, {"prefix": "prefix", "note_position": "notePosition", "is_expanded": "isExpanded"}))
    new_branch = unwrap_object(
        request_trilium(payload, "/branches", method="POST", json_body=body),
        "Create destination branch",
    )

    # ETAPI 没有原子 move 接口。新位置创建成功后再删除旧 Branch；若删除
    # 响应不确定，保留新位置并明确返回不完整状态，避免回滚导致笔记失去所有父节点。
    try:
        deleted = request_trilium(payload, f"/branches/{quote(branch_id, safe='')}", method="DELETE")
    except TriliumError as exc:
        return {
            "success": False,
            "service": "trilium",
            "code": "move_incomplete",
            "message": "Destination branch was created, but deleting the source branch had an uncertain result.",
            "details": {"source_branch_id": branch_id, "destination_branch": new_branch, "error": str(exc)},
        }
    if not deleted["success"]:
        return {
            "success": False,
            "service": "trilium",
            "code": "move_incomplete",
            "message": "Destination branch was created, but the source branch could not be deleted.",
            "details": {
                "source_branch_id": branch_id,
                "destination_branch": new_branch,
                "delete_http_status": deleted.get("http_status"),
                "delete_response": deleted.get("response"),
            },
        }

    verified = get_note_object(payload, note_id, "Verify moved note")
    verified_branch_ids = verified.get("parentBranchIds")
    verified_parent_ids = verified.get("parentNoteIds")
    move_verified = (
        isinstance(verified_branch_ids, list)
        and branch_id not in verified_branch_ids
        and isinstance(verified_parent_ids, list)
        and new_parent_note_id in verified_parent_ids
    )
    return {
        "success": move_verified,
        "service": "trilium",
        "action": "move-note",
        "moved": move_verified,
        "note_id": note_id,
        "title": note.get("title"),
        "old_branch_id": branch_id,
        "new_parent_note_id": new_parent_note_id,
        "new_parent_title": target_parent.get("title"),
        "new_branch": new_branch,
        **({} if move_verified else {"code": "move_verification_failed"}),
    }


def op_update_branch(payload: dict[str, Any]) -> dict[str, Any]:
    branch_id = require_string(payload, "branch_id")
    body = copy_fields(payload, {"note_position": "notePosition", "prefix": "prefix", "is_expanded": "isExpanded"})
    if not body:
        raise TriliumError("update-branch requires at least one editable field.", code="missing_changes")

    branch = unwrap_object(
        request_trilium(payload, f"/branches/{quote(branch_id, safe='')}"),
        "Read branch before update",
    )
    note_id = branch.get("noteId")
    if not isinstance(note_id, str) or not note_id:
        raise TriliumError("Trilium returned an invalid branch note ID.", code="bad_upstream_response")
    ensure_movable_note(note_id)
    note = get_note_object(payload, note_id, "Read branch note")
    ensure_writable_note(note, "branch update")
    return request_trilium(
        payload,
        f"/branches/{quote(branch_id, safe='')}",
        method="PATCH",
        json_body=body,
    )


def op_delete_branch(payload: dict[str, Any]) -> dict[str, Any]:
    require_confirmed(payload, "delete-branch")
    branch_id = require_string(payload, "branch_id")
    if require_string(payload, "confirm_branch_id") != branch_id:
        raise TriliumError("confirm_branch_id does not match branch_id.", code="confirmation_mismatch")

    branch = unwrap_object(
        request_trilium(payload, f"/branches/{quote(branch_id, safe='')}"),
        "Read branch before deletion",
    )
    note_id = branch.get("noteId")
    if not isinstance(note_id, str) or not note_id:
        raise TriliumError("Trilium returned an invalid branch note ID.", code="bad_upstream_response")
    requested_note_id = optional_string(payload, "note_id")
    if requested_note_id is not None and requested_note_id != note_id:
        raise TriliumError("note_id does not belong to this branch.", code="branch_mismatch")

    ensure_movable_note(note_id)
    note = get_note_object(payload, note_id, "Read branch note")
    ensure_writable_note(note, "branch deletion")
    parent_branch_ids = note.get("parentBranchIds")
    if not isinstance(parent_branch_ids, list) or not all(isinstance(item, str) for item in parent_branch_ids):
        raise TriliumError("Trilium returned invalid parentBranchIds.", code="bad_upstream_response")
    if branch_id not in parent_branch_ids:
        raise TriliumError("The branch does not belong to the current note state.", code="branch_mismatch")

    deletes_note = len(parent_branch_ids) == 1
    if deletes_note:
        if payload.get("confirm_delete_note") is not True:
            raise TriliumError(
                "Deleting the last branch also deletes the note and its subtree; confirm_delete_note: true is required.",
                code="last_branch_confirmation_required",
                details={"note_id": note_id, "title": note.get("title")},
            )
        if require_string(payload, "confirm_title") != note.get("title"):
            raise TriliumError(
                "confirm_title does not match the current note title.",
                code="confirmation_mismatch",
                details={"expected_title": note.get("title")},
            )

    deleted = request_trilium(
        payload,
        f"/branches/{quote(branch_id, safe='')}",
        method="DELETE",
    )
    return {
        "success": deleted["success"],
        "service": "trilium",
        "action": "delete-branch",
        "target": {
            "branch_id": branch_id,
            "note_id": note_id,
            "title": note.get("title"),
            "parent_note_id": branch.get("parentNoteId"),
            "deletes_note": deletes_note,
        },
        "http_status": deleted["http_status"],
        "response": deleted["response"],
    }


def op_refresh_note_ordering(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/refresh-note-ordering/{path_id(payload, 'parent_note_id')}", method="POST", json_body={})


def op_get_attribute(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/attributes/{path_id(payload, 'attribute_id')}")


def op_get_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    note = get_note_object(payload, note_id)
    attributes = note.get("attributes")
    if not isinstance(attributes, list) or not all(isinstance(item, dict) for item in attributes):
        raise TriliumError("Trilium returned invalid attributes.", code="bad_upstream_response")

    owned = [
        attribute
        for attribute in attributes
        if attribute.get("noteId") == note_id and not is_auto_link_attribute(attribute)
    ]
    return {
        "success": True,
        "service": "trilium",
        "action": "get-attributes",
        "note_id": note_id,
        "attributes": owned,
    }


def replace_relation_attribute(
    payload: dict[str, Any],
    attribute: dict[str, Any],
    value: str,
    *,
    position: int | None = None,
) -> dict[str, Any]:
    old_attribute_id = attribute.get("attributeId")
    note_id = attribute.get("noteId")
    name = attribute.get("name")
    if not isinstance(old_attribute_id, str) or not old_attribute_id:
        raise TriliumError("Trilium returned an invalid attribute ID.", code="bad_upstream_response")
    if not isinstance(note_id, str) or not note_id:
        raise TriliumError("Trilium returned an invalid attribute owner.", code="bad_upstream_response")
    if not isinstance(name, str) or not name:
        raise TriliumError("Trilium returned an invalid attribute name.", code="bad_upstream_response")

    validate_attribute_safety("relation", name)
    validate_attribute_value(payload, "relation", value)
    new_attribute_id = generated_entity_id()
    body: dict[str, Any] = {
        "attributeId": new_attribute_id,
        "noteId": note_id,
        "type": "relation",
        "name": name,
        "value": value,
    }
    if isinstance(attribute.get("isInheritable"), bool):
        body["isInheritable"] = attribute["isInheritable"]
    if position is not None:
        body["position"] = position
    elif isinstance(attribute.get("position"), int) and not isinstance(attribute.get("position"), bool):
        body["position"] = attribute["position"]

    created = request_trilium(payload, "/attributes", method="POST", json_body=body)
    replacement = unwrap_object(created, "Create replacement relation")

    try:
        deleted = request_trilium(
            payload,
            f"/attributes/{quote(old_attribute_id, safe='')}",
            method="DELETE",
        )
    except TriliumError as exc:
        return {
            "success": False,
            "service": "trilium",
            "code": "attribute_replace_incomplete",
            "message": "The replacement relation was created, but deleting the old relation had an uncertain result.",
            "details": {
                "old_attribute_id": old_attribute_id,
                "new_attribute": replacement,
                "error": str(exc),
            },
        }
    if not deleted["success"]:
        return {
            "success": False,
            "service": "trilium",
            "code": "attribute_replace_incomplete",
            "message": "The replacement relation was created, but the old relation could not be deleted.",
            "details": {
                "old_attribute_id": old_attribute_id,
                "new_attribute": replacement,
                "delete_http_status": deleted.get("http_status"),
                "delete_response": deleted.get("response"),
            },
        }

    verified_note = get_note_object(payload, note_id, "Verify relation replacement")
    attributes = verified_note.get("attributes")
    if not isinstance(attributes, list) or not all(isinstance(item, dict) for item in attributes):
        raise TriliumError("Trilium returned invalid attributes.", code="bad_upstream_response")
    old_present = any(item.get("attributeId") == old_attribute_id for item in attributes)
    replacement_present = any(
        item.get("attributeId") == new_attribute_id
        and item.get("noteId") == note_id
        and item.get("type") == "relation"
        and item.get("name") == name
        and item.get("value") == value
        for item in attributes
    )
    if old_present or not replacement_present:
        return {
            "success": False,
            "service": "trilium",
            "code": "attribute_replace_verification_failed",
            "message": "The relation replacement could not be verified.",
            "details": {
                "old_attribute_id": old_attribute_id,
                "new_attribute_id": new_attribute_id,
            },
        }

    return {
        "success": True,
        "service": "trilium",
        "mode": "replaced",
        "note_id": note_id,
        "old_attribute_id": old_attribute_id,
        "attribute_id": new_attribute_id,
        "type": "relation",
        "name": name,
        "value": value,
        "response": replacement,
    }


def op_create_attribute(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    attribute_type = require_string(payload, "type")
    name = require_string(payload, "name")
    value = optional_string(payload, "value") or ""
    validate_attribute_type(attribute_type)
    validate_attribute_safety(attribute_type, name)
    validate_attribute_value(payload, attribute_type, value)

    note = get_note_object(payload, note_id)
    ensure_writable_note(note, "attribute creation")
    body = {
        "attributeId": optional_string(payload, "attribute_id") or generated_entity_id(),
        "noteId": note_id,
        "type": attribute_type,
        "name": name,
        "value": value,
    }
    body.update(copy_fields(payload, {"is_inheritable": "isInheritable", "position": "position"}))
    return request_trilium(payload, "/attributes", method="POST", json_body=body)


def op_set_attribute(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = require_string(payload, "note_id")
    attribute_type = require_string(payload, "type")
    name = require_string(payload, "name")
    value = optional_string(payload, "value") or ""
    validate_attribute_type(attribute_type)
    validate_attribute_safety(attribute_type, name)
    validate_attribute_value(payload, attribute_type, value)

    note = get_note_object(payload, note_id)
    ensure_writable_note(note, "attribute upsert")
    attributes = note.get("attributes")
    if not isinstance(attributes, list) or not all(isinstance(item, dict) for item in attributes):
        raise TriliumError("Trilium returned invalid attributes.", code="bad_upstream_response")
    matches = [
        attribute
        for attribute in attributes
        if attribute.get("noteId") == note_id
        and attribute.get("type") == attribute_type
        and attribute.get("name") == name
    ]
    if len(matches) > 1:
        raise TriliumError(
            "Multiple owned attributes share this type and name; update one explicitly by attribute_id.",
            code="duplicate_attributes",
            details={"attribute_ids": [item.get("attributeId") for item in matches]},
        )
    if matches:
        attribute = matches[0]
        attribute_id = attribute.get("attributeId")
        if not isinstance(attribute_id, str) or not attribute_id:
            raise TriliumError("Trilium returned an invalid attribute ID.", code="bad_upstream_response")
        if attribute.get("value") == value:
            return {
                "success": True,
                "service": "trilium",
                "action": "set-attribute",
                "mode": "unchanged",
                "note_id": note_id,
                "attribute_id": attribute_id,
                "type": attribute_type,
                "name": name,
                "value": value,
            }
        if attribute_type == "relation":
            replaced = replace_relation_attribute(payload, attribute, value)
            replaced["action"] = "set-attribute"
            return replaced

        updated = request_trilium(
            payload,
            f"/attributes/{quote(attribute_id, safe='')}",
            method="PATCH",
            json_body={"value": value},
        )
        if not updated["success"]:
            return updated
        return {
            "success": True,
            "service": "trilium",
            "action": "set-attribute",
            "mode": "updated",
            "note_id": note_id,
            "attribute_id": attribute_id,
            "type": attribute_type,
            "name": name,
            "value": value,
            "response": updated.get("response"),
        }

    created = op_create_attribute(payload)
    if not created["success"]:
        return created
    response = created.get("response")
    attribute_id = response.get("attributeId") if isinstance(response, dict) else None
    return {
        "success": True,
        "service": "trilium",
        "action": "set-attribute",
        "mode": "created",
        "note_id": note_id,
        "attribute_id": attribute_id,
        "type": attribute_type,
        "name": name,
        "value": value,
        "response": response,
    }


def op_update_attribute(payload: dict[str, Any]) -> dict[str, Any]:
    attribute_id = require_string(payload, "attribute_id")
    body: dict[str, Any] = {}
    if "value" in payload:
        body["value"] = optional_string(payload, "value")
    if "position" in payload:
        body["position"] = optional_int(payload, "position")
    if not body:
        raise TriliumError("update-attribute requires value or position.", code="missing_changes")

    attribute = get_attribute_object(payload, attribute_id)
    note_id = attribute.get("noteId")
    attribute_type = attribute.get("type")
    name = attribute.get("name")
    if not isinstance(note_id, str) or not note_id:
        raise TriliumError("Trilium returned an invalid attribute owner.", code="bad_upstream_response")
    if not isinstance(attribute_type, str) or not isinstance(name, str):
        raise TriliumError("Trilium returned an invalid attribute.", code="bad_upstream_response")
    validate_attribute_type(attribute_type)
    validate_attribute_safety(attribute_type, name)
    note = get_note_object(payload, note_id, "Read attribute owner")
    ensure_writable_note(note, "attribute update")

    if attribute_type == "relation" and "value" in body:
        value = body["value"]
        if not isinstance(value, str):
            raise TriliumError("value must be a string.", code="bad_field", details={"field": "value"})
        validate_attribute_value(payload, "relation", value)
        if attribute.get("value") != value:
            replaced = replace_relation_attribute(
                payload,
                attribute,
                value,
                position=body.get("position"),
            )
            replaced["action"] = "update-attribute"
            return replaced
        body.pop("value")
        if not body:
            return {
                "success": True,
                "service": "trilium",
                "action": "update-attribute",
                "mode": "unchanged",
                "attribute_id": attribute_id,
                "note_id": note_id,
            }

    return request_trilium(
        payload,
        f"/attributes/{quote(attribute_id, safe='')}",
        method="PATCH",
        json_body=body,
    )


def op_delete_attribute(payload: dict[str, Any]) -> dict[str, Any]:
    require_confirmed(payload, "delete-attribute")
    attribute_id = require_string(payload, "attribute_id")
    if require_string(payload, "confirm_attribute_id") != attribute_id:
        raise TriliumError("confirm_attribute_id does not match attribute_id.", code="confirmation_mismatch")

    attribute = get_attribute_object(payload, attribute_id)
    note_id = attribute.get("noteId")
    if not isinstance(note_id, str) or not note_id:
        raise TriliumError("Trilium returned an invalid attribute owner.", code="bad_upstream_response")
    requested_note_id = optional_string(payload, "note_id")
    if requested_note_id is not None and requested_note_id != note_id:
        raise TriliumError("note_id does not own this attribute.", code="attribute_owner_mismatch")
    note = get_note_object(payload, note_id, "Read attribute owner")
    ensure_writable_note(note, "attribute deletion")

    deleted = request_trilium(
        payload,
        f"/attributes/{quote(attribute_id, safe='')}",
        method="DELETE",
    )
    return {
        "success": deleted["success"],
        "service": "trilium",
        "action": "delete-attribute",
        "target": {
            "attribute_id": attribute_id,
            "note_id": note_id,
            "type": attribute.get("type"),
            "name": attribute.get("name"),
        },
        "http_status": deleted["http_status"],
        "response": deleted["response"],
    }


def op_list_note_attachments(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/notes/{path_id(payload, 'note_id')}/attachments")


def op_get_attachment(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/attachments/{path_id(payload, 'attachment_id')}")


def op_get_attachment_content(payload: dict[str, Any]) -> dict[str, Any]:
    return request_trilium(payload, f"/attachments/{path_id(payload, 'attachment_id')}/content", preserve_text=True)


def calendar_action(endpoint_template: str, input_key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def execute(payload: dict[str, Any]) -> dict[str, Any]:
        return request_trilium(payload, endpoint_template.format(value=path_id(payload, input_key)))

    return execute


def op_create_backup(payload: dict[str, Any]) -> dict[str, Any]:
    require_confirmed(payload, "create-backup")
    backup_name = require_string(payload, "backup_name")
    if not BACKUP_NAME_PATTERN.fullmatch(backup_name):
        raise TriliumError(
            "backup_name may contain only letters, digits, dot, underscore, and hyphen (1-64 characters).",
            code="bad_backup_name",
        )
    if require_string(payload, "confirm_backup_name") != backup_name:
        raise TriliumError("confirm_backup_name does not match backup_name.", code="confirmation_mismatch")
    return request_trilium(payload, f"/backup/{quote(backup_name, safe='')}", method="PUT", json_body={})


ACTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "status": op_status,
    "search-notes": op_search_notes,
    "get-note": op_get_note,
    "get-note-content": op_get_note_content,
    "create-note": op_create_note,
    "update-note": op_update_note,
    "set-note-content": op_set_note_content,
    "append-note-content": op_append_note_content,
    "edit-note-content": op_edit_note_content,
    "delete-note": op_delete_note,
    "note-history": op_note_history,
    "list-note-revisions": op_list_note_revisions,
    "get-revision": op_get_revision,
    "get-revision-content": op_get_revision_content,
    "create-revision": op_create_revision,
    "undelete-note": op_undelete_note,
    "get-child-notes": op_get_child_notes,
    "get-subtree": op_get_subtree,
    "get-branch": op_get_branch,
    "create-branch": op_create_branch,
    "clone-note": op_clone_note,
    "move-note": op_move_note,
    "update-branch": op_update_branch,
    "delete-branch": op_delete_branch,
    "refresh-note-ordering": op_refresh_note_ordering,
    "get-attributes": op_get_attributes,
    "get-attribute": op_get_attribute,
    "create-attribute": op_create_attribute,
    "set-attribute": op_set_attribute,
    "update-attribute": op_update_attribute,
    "delete-attribute": op_delete_attribute,
    "list-note-attachments": op_list_note_attachments,
    "get-attachment": op_get_attachment,
    "get-attachment-content": op_get_attachment_content,
    "get-inbox-note": calendar_action("/inbox/{value}", "date"),
    "get-day-note": calendar_action("/calendar/days/{value}", "date"),
    "get-week-note": calendar_action("/calendar/weeks/{value}", "week"),
    "get-month-note": calendar_action("/calendar/months/{value}", "month"),
    "get-year-note": calendar_action("/calendar/years/{value}", "year"),
    "create-backup": op_create_backup,
}


def main() -> int:
    try:
        payload = load_input()
        action = payload.get("skill_action", "status")
        if not isinstance(action, str) or action not in ACTIONS:
            raise TriliumError(
                f"Unsupported skill_action: {action!r}.",
                code="unsupported_action",
                details={"supported_actions": sorted(ACTIONS)},
            )
        result = ACTIONS[action](payload)
        return emit(result, exit_code=0 if result.get("success") else 1)
    except TriliumError as exc:
        result: dict[str, Any] = {
            "success": False,
            "service": "trilium",
            "code": exc.code,
            "message": str(exc),
        }
        if exc.details is not None:
            result["details"] = exc.details
        return emit(result, exit_code=1)
    except Exception as exc:  # pragma: no cover - unexpected boundary failure
        return emit(
            {
                "success": False,
                "service": "trilium",
                "code": "internal_error",
                "message": f"Unexpected Trilium Skill failure: {exc}",
            },
            exit_code=1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
