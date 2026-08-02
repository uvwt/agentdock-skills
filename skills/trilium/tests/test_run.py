#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "run.py"
TOKEN = "test-etapi-secret-token"


class FakeTriliumHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    notes: dict[str, dict[str, object]] = {}
    contents: dict[str, str] = {}
    branches: dict[str, dict[str, object]] = {}

    @classmethod
    def reset_state(cls) -> None:
        cls.requests = []
        cls.notes = {
            "root": {
                "noteId": "root",
                "title": "root",
                "type": "book",
                "mime": "text/html",
                "isProtected": False,
                "blobId": "root-blob",
                "utcDateModified": "2026-01-01T00:00:00.000Z",
                "parentNoteIds": [],
                "parentBranchIds": [],
                "childNoteIds": ["n1", "code1"],
                "childBranchIds": ["b1", "b-code"],
                "attributes": [],
            },
            "n1": {
                "noteId": "n1",
                "title": "Target",
                "type": "text",
                "mime": "text/html",
                "isProtected": False,
                "blobId": "blob-n1",
                "utcDateModified": "2026-01-01T00:00:01.000Z",
                "parentNoteIds": ["root"],
                "parentBranchIds": ["b1"],
                "childNoteIds": ["c1", "c2"],
                "childBranchIds": ["b-c1", "b-c2"],
                "attributes": [
                    {
                        "attributeId": "a-status",
                        "noteId": "n1",
                        "type": "label",
                        "name": "status",
                        "value": "todo",
                        "isInheritable": False,
                    },
                    {
                        "attributeId": "a-link",
                        "noteId": "n1",
                        "type": "relation",
                        "name": "internalLink",
                        "value": "c1",
                        "isInheritable": False,
                    },
                    {
                        "attributeId": "a-ref",
                        "noteId": "n1",
                        "type": "relation",
                        "name": "reference",
                        "value": "c1",
                        "position": 20,
                        "isInheritable": False,
                    },
                    {
                        "attributeId": "a-inherited",
                        "noteId": "root",
                        "type": "label",
                        "name": "workspace",
                        "value": "main",
                        "isInheritable": True,
                    },
                ],
            },
            "code1": {
                "noteId": "code1",
                "title": "Code",
                "type": "code",
                "mime": "application/json",
                "isProtected": False,
                "blobId": "blob-code1",
                "utcDateModified": "2026-01-01T00:00:02.000Z",
                "parentNoteIds": ["root"],
                "parentBranchIds": ["b-code"],
                "childNoteIds": [],
                "childBranchIds": [],
                "attributes": [],
            },
            "c1": {
                "noteId": "c1",
                "title": "Child One",
                "type": "text",
                "mime": "text/html",
                "isProtected": False,
                "blobId": "blob-c1",
                "utcDateModified": "2026-01-01T00:00:03.000Z",
                "parentNoteIds": ["n1"],
                "parentBranchIds": ["b-c1"],
                "childNoteIds": [],
                "childBranchIds": [],
                "attributes": [],
            },
            "c2": {
                "noteId": "c2",
                "title": "Child Two",
                "type": "book",
                "mime": "text/html",
                "isProtected": False,
                "blobId": "blob-c2",
                "utcDateModified": "2026-01-01T00:00:04.000Z",
                "parentNoteIds": ["n1"],
                "parentBranchIds": ["b-c2"],
                "childNoteIds": ["g1"],
                "childBranchIds": ["b-g1"],
                "attributes": [],
            },
            "g1": {
                "noteId": "g1",
                "title": "Grandchild",
                "type": "code",
                "mime": "text/plain",
                "isProtected": False,
                "blobId": "blob-g1",
                "utcDateModified": "2026-01-01T00:00:05.000Z",
                "parentNoteIds": ["c2"],
                "parentBranchIds": ["b-g1"],
                "childNoteIds": [],
                "childBranchIds": [],
                "attributes": [],
            },
            "parent2": {
                "noteId": "parent2",
                "title": "Second Parent",
                "type": "book",
                "mime": "text/html",
                "isProtected": False,
                "blobId": "blob-parent2",
                "utcDateModified": "2026-01-01T00:00:06.000Z",
                "parentNoteIds": ["root"],
                "parentBranchIds": ["b-parent2"],
                "childNoteIds": [],
                "childBranchIds": [],
                "attributes": [],
            },
            "relation-target": {
                "noteId": "relation-target",
                "title": "Relation Target",
                "type": "text",
                "mime": "text/html",
                "isProtected": False,
                "blobId": "blob-relation-target",
                "utcDateModified": "2026-01-01T00:00:07.000Z",
                "parentNoteIds": ["root"],
                "parentBranchIds": ["b-relation-target"],
                "childNoteIds": [],
                "childBranchIds": [],
                "attributes": [],
            },
            "protected": {
                "noteId": "protected",
                "title": "Protected",
                "type": "text",
                "mime": "text/html",
                "isProtected": True,
                "blobId": "blob-protected",
                "utcDateModified": "2026-01-01T00:00:08.000Z",
                "parentNoteIds": ["root"],
                "parentBranchIds": ["b-protected"],
                "childNoteIds": [],
                "childBranchIds": [],
                "attributes": [],
            },
        }
        cls.contents = {
            "n1": "<p>Hello</p>",
            "code1": '{"enabled": false}\nvalue = 1',
            "c1": "<p>Child</p>",
            "c2": "",
            "g1": "print('g1')",
            "parent2": "",
            "relation-target": "<p>Target</p>",
        }
        cls.branches = {
            "b1": {
                "branchId": "b1",
                "noteId": "n1",
                "parentNoteId": "root",
                "prefix": "",
                "notePosition": 10,
                "isExpanded": False,
            },
            "b-code": {
                "branchId": "b-code",
                "noteId": "code1",
                "parentNoteId": "root",
                "prefix": "",
                "notePosition": 20,
                "isExpanded": False,
            },
            "b-protected": {
                "branchId": "b-protected",
                "noteId": "protected",
                "parentNoteId": "root",
                "prefix": "",
                "notePosition": 30,
                "isExpanded": False,
            },
        }

    def log_message(self, format: str, *args: object) -> None:
        return

    def record_request(self) -> tuple[str, bytes]:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "body": body,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        return parsed.path, body

    def send_json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, status: int, payload: str, content_type: str) -> None:
        raw = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.end_headers()

    def do_GET(self) -> None:
        path, _ = self.record_request()
        if path == "/etapi/app-info":
            self.send_json(200, {"appVersion": "0.103.0", "dbVersion": 234})
            return
        if path == "/etapi/notes":
            self.send_json(200, {"results": [{"noteId": "n1", "title": "Target"}]})
            return
        if path == "/etapi/notes/leak":
            self.send_json(500, {"message": f"upstream echoed {TOKEN}", "token": TOKEN})
            return
        if path.startswith("/etapi/notes/") and path.endswith("/content"):
            note_id = path.removeprefix("/etapi/notes/").removesuffix("/content")
            note = self.__class__.notes.get(note_id)
            if note is None:
                self.send_json(404, {"code": "NOT_FOUND"})
                return
            self.send_text(200, self.__class__.contents.get(note_id, ""), str(note.get("mime", "text/plain")))
            return
        if path.startswith("/etapi/notes/"):
            note_id = path.removeprefix("/etapi/notes/")
            note = self.__class__.notes.get(note_id)
            if note is None:
                self.send_json(404, {"code": "NOT_FOUND"})
            else:
                self.send_json(200, note)
            return
        if path.startswith("/etapi/attributes/"):
            attribute_id = path.removeprefix("/etapi/attributes/")
            for note in self.__class__.notes.values():
                for attribute in note.get("attributes", []):
                    if attribute.get("attributeId") == attribute_id:
                        self.send_json(200, attribute)
                        return
            self.send_json(404, {"code": "NOT_FOUND"})
            return
        if path.startswith("/etapi/branches/"):
            branch_id = path.removeprefix("/etapi/branches/")
            branch = self.__class__.branches.get(branch_id)
            if branch is None:
                self.send_json(404, {"code": "NOT_FOUND"})
            else:
                self.send_json(200, branch)
            return
        if path == "/etapi/attachments/bin/content":
            raw = b"\x00\x01\x02\xff"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_json(404, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:
        path, body = self.record_request()
        if path == "/etapi/create-note":
            request_payload = json.loads(body)
            self.send_json(
                201,
                {
                    "note": {
                        "noteId": "new-note",
                        "title": request_payload["title"],
                        "type": request_payload["type"],
                    },
                    "branch": {"branchId": "new-branch"},
                },
            )
            return
        if path == "/etapi/attributes":
            attribute = json.loads(body)
            note_id = attribute["noteId"]
            self.__class__.notes[note_id]["attributes"].append(attribute)
            self.send_json(201, attribute)
            return
        if path == "/etapi/branches":
            request_payload = json.loads(body)
            branch_id = f"new-branch-{len(self.__class__.branches) + 1}"
            branch = {
                "branchId": branch_id,
                "noteId": request_payload["noteId"],
                "parentNoteId": request_payload["parentNoteId"],
                "prefix": request_payload.get("prefix", ""),
                "notePosition": request_payload.get("notePosition", 0),
                "isExpanded": request_payload.get("isExpanded", False),
            }
            self.__class__.branches[branch_id] = branch
            note = self.__class__.notes[request_payload["noteId"]]
            note["parentBranchIds"].append(branch_id)
            note["parentNoteIds"].append(request_payload["parentNoteId"])
            parent = self.__class__.notes[request_payload["parentNoteId"]]
            parent["childBranchIds"].append(branch_id)
            parent["childNoteIds"].append(request_payload["noteId"])
            self.send_json(201, branch)
            return
        self.send_empty()

    def do_PATCH(self) -> None:
        path, body = self.record_request()
        if path.startswith("/etapi/attributes/"):
            attribute_id = path.removeprefix("/etapi/attributes/")
            patch = json.loads(body)
            for note in self.__class__.notes.values():
                attributes = note.get("attributes", [])
                for attribute in attributes:
                    if attribute.get("attributeId") == attribute_id:
                        attribute.update(patch)
                        self.send_json(200, attribute)
                        return
            self.send_json(404, {"code": "NOT_FOUND"})
            return
        self.send_json(200, {"success": True})

    def do_PUT(self) -> None:
        path, body = self.record_request()
        if path.startswith("/etapi/notes/") and path.endswith("/content"):
            note_id = path.removeprefix("/etapi/notes/").removesuffix("/content")
            self.__class__.contents[note_id] = body.decode("utf-8")
            note = self.__class__.notes[note_id]
            note["blobId"] = f"updated-{note_id}"
            note["utcDateModified"] = "2026-01-01T00:10:00.000Z"
        self.send_empty()

    def do_DELETE(self) -> None:
        path, _ = self.record_request()
        if path == "/etapi/notes/n1":
            self.send_empty()
            return
        if path.startswith("/etapi/attributes/"):
            attribute_id = path.removeprefix("/etapi/attributes/")
            for note in self.__class__.notes.values():
                attributes = note.get("attributes", [])
                for attribute in list(attributes):
                    if attribute.get("attributeId") == attribute_id:
                        attributes.remove(attribute)
                        self.send_empty()
                        return
            self.send_empty()
            return
        if path.startswith("/etapi/branches/"):
            branch_id = path.removeprefix("/etapi/branches/")
            branch = self.__class__.branches.pop(branch_id, None)
            if branch is None:
                self.send_empty()
                return
            note = self.__class__.notes[str(branch["noteId"])]
            parent = self.__class__.notes[str(branch["parentNoteId"])]
            note["parentBranchIds"].remove(branch_id)
            note["parentNoteIds"].remove(branch["parentNoteId"])
            parent["childBranchIds"].remove(branch_id)
            parent["childNoteIds"].remove(branch["noteId"])
            self.send_empty()
            return
        self.send_json(404, {"code": "NOT_FOUND"})


class TriliumSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTriliumHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        FakeTriliumHandler.reset_state()

    def run_skill(
        self,
        payload: object,
        *,
        configured: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        env = os.environ.copy()
        env.pop("TRILIUM_URL", None)
        env.pop("TRILIUM_ETAPI_TOKEN", None)
        env.pop("TRILIUM_INSECURE_TLS", None)
        if configured:
            env["TRILIUM_URL"] = self.base_url
            env["TRILIUM_ETAPI_TOKEN"] = TOKEN
        proc = subprocess.run(
            [sys.executable, str(RUNNER)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertTrue(proc.stdout.strip(), proc.stderr)
        return proc, json.loads(proc.stdout)

    def test_status_without_environment_is_read_only(self) -> None:
        proc, result = self.run_skill({"skill_action": "status"}, configured=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(result["configured"])
        self.assertEqual(
            result["missing_environment"],
            ["TRILIUM_URL", "TRILIUM_ETAPI_TOKEN"],
        )
        self.assertEqual(FakeTriliumHandler.requests, [])

    def test_status_calls_app_info_with_token(self) -> None:
        proc, result = self.run_skill({"skill_action": "status"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["ready"])
        self.assertEqual(result["app_info"]["appVersion"], "0.103.0")
        request = FakeTriliumHandler.requests[0]
        self.assertEqual(request["path"], "/etapi/app-info")
        self.assertEqual(request["authorization"], TOKEN)

    def test_search_maps_query_fields(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "search-notes",
                "query": "tolkien #book",
                "fast_search": True,
                "ancestor_note_id": "books",
                "limit": 7,
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["success"])
        query = FakeTriliumHandler.requests[0]["query"]
        self.assertEqual(query["search"], ["tolkien #book"])
        self.assertEqual(query["fastSearch"], ["true"])
        self.assertEqual(query["ancestorNoteId"], ["books"])
        self.assertEqual(query["limit"], ["7"])

    def test_set_content_sends_raw_text(self) -> None:
        content = "<p>新的正文</p>"
        proc, result = self.run_skill(
            {"skill_action": "set-note-content", "note_id": "n1", "content": content}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["success"])
        request = FakeTriliumHandler.requests[0]
        self.assertEqual(request["method"], "PUT")
        self.assertEqual(request["body"].decode("utf-8"), content)
        self.assertTrue(str(request["content_type"]).startswith("text/plain"))

    def test_create_note_maps_snake_case_fields(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "create-note",
                "parent_note_id": "root",
                "title": "Created",
                "type": "code",
                "content": "print('ok')",
                "mime": "text/x-python",
                "note_position": 30,
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["response"]["note"]["noteId"], "new-note")
        post_request = next(
            item
            for item in FakeTriliumHandler.requests
            if item["method"] == "POST" and item["path"] == "/etapi/create-note"
        )
        request_body = json.loads(post_request["body"])
        self.assertEqual(request_body["parentNoteId"], "root")
        self.assertEqual(request_body["notePosition"], 30)
        self.assertNotIn("parent_note_id", request_body)

    def test_create_note_rejects_protected_parent(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "create-note",
                "parent_note_id": "protected",
                "title": "Blocked",
                "type": "text",
                "content": "<p>Blocked</p>",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "protected_note")
        self.assertNotIn("POST", [item["method"] for item in FakeTriliumHandler.requests])

    def test_delete_system_note_rejected_without_network(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "delete-note",
                "note_id": "root",
                "confirm": True,
                "confirm_title": "root",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "protected_system_note")
        self.assertEqual(FakeTriliumHandler.requests, [])

    def test_delete_protected_note_rejected(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "delete-note",
                "note_id": "protected",
                "confirm": True,
                "confirm_title": "Protected",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "protected_note")
        self.assertEqual([item["method"] for item in FakeTriliumHandler.requests], ["GET"])

    def test_delete_requires_confirmation_without_network_call(self) -> None:
        proc, result = self.run_skill(
            {"skill_action": "delete-note", "note_id": "n1", "confirm_title": "Target"}
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(FakeTriliumHandler.requests, [])

    def test_delete_title_mismatch_does_not_delete(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "delete-note",
                "note_id": "n1",
                "confirm": True,
                "confirm_title": "Wrong",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "confirmation_mismatch")
        self.assertEqual([item["method"] for item in FakeTriliumHandler.requests], ["GET"])

    def test_delete_reads_target_then_deletes(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "delete-note",
                "note_id": "n1",
                "confirm": True,
                "confirm_title": "Target",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["success"])
        self.assertEqual(result["target"], {"note_id": "n1", "title": "Target"})
        self.assertEqual([item["method"] for item in FakeTriliumHandler.requests], ["GET", "DELETE"])

    def test_upstream_error_redacts_token(self) -> None:
        proc, result = self.run_skill({"skill_action": "get-note", "note_id": "leak"})
        self.assertEqual(proc.returncode, 1)
        rendered = json.dumps(result)
        self.assertNotIn(TOKEN, rendered)
        self.assertIn("<redacted>", rendered)

    def test_binary_content_returns_only_metadata(self) -> None:
        proc, result = self.run_skill(
            {"skill_action": "get-attachment-content", "attachment_id": "bin"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            result["response"],
            {
                "binary": True,
                "content_type": "application/octet-stream",
                "size_bytes": 4,
            },
        )

    def test_create_attribute_generates_entity_id(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "create-attribute",
                "note_id": "n1",
                "type": "label",
                "name": "status",
                "value": "done",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        attribute_id = result["response"]["attributeId"]
        self.assertEqual(len(attribute_id), 12)
        self.assertTrue(attribute_id.isalnum())

    def test_get_note_content_preserves_json_as_text(self) -> None:
        proc, result = self.run_skill({"skill_action": "get-note-content", "note_id": "code1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["response"], '{"enabled": false}\nvalue = 1')

    def test_append_note_content_rechecks_then_writes(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "append-note-content",
                "note_id": "code1",
                "content": "next = 2",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = '{"enabled": false}\nvalue = 1\nnext = 2'
        self.assertEqual(result["content"], expected)
        self.assertEqual(FakeTriliumHandler.contents["code1"], expected)
        self.assertEqual(
            [item["method"] for item in FakeTriliumHandler.requests],
            ["GET", "GET", "GET", "PUT"],
        )

    def test_append_note_content_supports_empty_existing_content(self) -> None:
        FakeTriliumHandler.contents["code1"] = ""
        proc, result = self.run_skill(
            {
                "skill_action": "append-note-content",
                "note_id": "code1",
                "content": "first line",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["content"], "first line")
        self.assertEqual(FakeTriliumHandler.contents["code1"], "first line")

    def test_edit_note_content_replaces_unique_text(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "edit-note-content",
                "note_id": "code1",
                "edits": [{"old_text": "false", "new_text": "true"}],
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["edits_applied"], 1)
        self.assertEqual(FakeTriliumHandler.contents["code1"], '{"enabled": true}\nvalue = 1')

    def test_edit_note_content_rejects_non_unique_text_without_write(self) -> None:
        FakeTriliumHandler.contents["code1"] = "repeat repeat"
        proc, result = self.run_skill(
            {
                "skill_action": "edit-note-content",
                "note_id": "code1",
                "edits": [{"old_text": "repeat", "new_text": "done"}],
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "edit_text_not_unique")
        self.assertNotIn("PUT", [item["method"] for item in FakeTriliumHandler.requests])

    def test_get_attributes_returns_only_owned_non_auto_link_attributes(self) -> None:
        proc, result = self.run_skill({"skill_action": "get-attributes", "note_id": "n1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [item["attributeId"] for item in result["attributes"]],
            ["a-status", "a-ref"],
        )

    def test_set_attribute_updates_existing_attribute(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "set-attribute",
                "note_id": "n1",
                "type": "label",
                "name": "status",
                "value": "done",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["mode"], "updated")
        self.assertEqual(result["attribute_id"], "a-status")
        self.assertEqual(FakeTriliumHandler.notes["n1"]["attributes"][0]["value"], "done")
        self.assertEqual([item["method"] for item in FakeTriliumHandler.requests], ["GET", "PATCH"])

    def test_set_attribute_rejects_dangerous_name_without_network(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "set-attribute",
                "note_id": "n1",
                "type": "label",
                "name": "run",
                "value": "backend",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "dangerous_attribute")
        self.assertEqual(FakeTriliumHandler.requests, [])

    def test_create_attribute_rejects_protected_note(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "create-attribute",
                "note_id": "protected",
                "type": "label",
                "name": "status",
                "value": "done",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "protected_note")
        self.assertEqual([item["method"] for item in FakeTriliumHandler.requests], ["GET"])

    def test_relation_attribute_requires_target_note_id(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "set-attribute",
                "note_id": "n1",
                "type": "relation",
                "name": "reference",
                "value": "",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "bad_field")
        self.assertEqual(FakeTriliumHandler.requests, [])

    def test_set_relation_attribute_replaces_etapi_entity(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "set-attribute",
                "note_id": "n1",
                "type": "relation",
                "name": "reference",
                "value": "relation-target",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["mode"], "replaced")
        self.assertEqual(result["old_attribute_id"], "a-ref")
        self.assertNotEqual(result["attribute_id"], "a-ref")
        references = [
            item
            for item in FakeTriliumHandler.notes["n1"]["attributes"]
            if item.get("type") == "relation" and item.get("name") == "reference"
        ]
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["value"], "relation-target")
        methods = [item["method"] for item in FakeTriliumHandler.requests]
        self.assertNotIn("PATCH", methods)
        self.assertLess(methods.index("POST"), methods.index("DELETE"))

    def test_update_relation_attribute_uses_replacement(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "update-attribute",
                "attribute_id": "a-ref",
                "value": "relation-target",
                "position": 40,
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["mode"], "replaced")
        replacement = next(
            item
            for item in FakeTriliumHandler.notes["n1"]["attributes"]
            if item.get("type") == "relation" and item.get("name") == "reference"
        )
        self.assertEqual(replacement["value"], "relation-target")
        self.assertEqual(replacement["position"], 40)

    def test_get_child_notes_returns_child_count(self) -> None:
        proc, result = self.run_skill({"skill_action": "get-child-notes", "note_id": "n1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["total_children"], 2)
        self.assertEqual(
            result["children"],
            [
                {"note_id": "c1", "title": "Child One", "type": "text", "child_count": 0},
                {"note_id": "c2", "title": "Child Two", "type": "book", "child_count": 1},
            ],
        )

    def test_get_subtree_builds_nested_structure(self) -> None:
        proc, result = self.run_skill(
            {"skill_action": "get-subtree", "note_id": "n1", "depth": 2, "node_limit": 20}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        subtree = result["subtree"]
        self.assertEqual(subtree["note_id"], "n1")
        self.assertEqual(subtree["children"][1]["note_id"], "c2")
        self.assertEqual(subtree["children"][1]["children"][0]["note_id"], "g1")
        self.assertEqual(result["nodes_read"], 4)

    def test_create_branch_rejects_cycle_before_write(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "create-branch",
                "note_id": "n1",
                "parent_note_id": "g1",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "hierarchy_cycle")
        self.assertNotIn("POST", [item["method"] for item in FakeTriliumHandler.requests])

    def test_clone_note_creates_additional_branch(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "clone-note",
                "note_id": "n1",
                "parent_note_id": "parent2",
                "prefix": "Ref: ",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["parent_note_id"], "parent2")
        self.assertIn("parent2", FakeTriliumHandler.notes["n1"]["parentNoteIds"])
        self.assertEqual(result["branch"]["prefix"], "Ref: ")

    def test_update_branch_rejects_protected_note(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "update-branch",
                "branch_id": "b-protected",
                "prefix": "Blocked",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "protected_note")
        self.assertNotIn("PATCH", [item["method"] for item in FakeTriliumHandler.requests])

    def test_delete_last_branch_requires_note_confirmation(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "delete-branch",
                "branch_id": "b-code",
                "confirm": True,
                "confirm_branch_id": "b-code",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "last_branch_confirmation_required")
        self.assertEqual(
            [item["method"] for item in FakeTriliumHandler.requests],
            ["GET", "GET"],
        )

    def test_delete_last_branch_with_note_confirmation(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "delete-branch",
                "branch_id": "b-code",
                "confirm": True,
                "confirm_branch_id": "b-code",
                "confirm_delete_note": True,
                "confirm_title": "Code",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["target"]["deletes_note"])
        self.assertNotIn("b-code", FakeTriliumHandler.branches)

    def test_move_note_requires_confirmation_without_network(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "move-note",
                "note_id": "code1",
                "new_parent_note_id": "parent2",
                "confirm_title": "Code",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "confirmation_required")
        self.assertEqual(FakeTriliumHandler.requests, [])

    def test_move_note_creates_destination_then_removes_source(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "move-note",
                "note_id": "code1",
                "new_parent_note_id": "parent2",
                "confirm": True,
                "confirm_title": "Code",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["moved"])
        self.assertEqual(FakeTriliumHandler.notes["code1"]["parentNoteIds"], ["parent2"])
        self.assertNotIn("b-code", FakeTriliumHandler.branches)
        self.assertEqual(
            [item["method"] for item in FakeTriliumHandler.requests],
            ["GET", "GET", "GET", "POST", "DELETE", "GET"],
        )

    def test_move_note_requires_branch_id_for_cloned_note(self) -> None:
        FakeTriliumHandler.notes["n1"]["parentBranchIds"].append("b-extra")
        FakeTriliumHandler.notes["n1"]["parentNoteIds"].append("parent2")
        FakeTriliumHandler.branches["b-extra"] = {
            "branchId": "b-extra",
            "noteId": "n1",
            "parentNoteId": "parent2",
            "prefix": "",
            "notePosition": 1,
            "isExpanded": False,
        }
        proc, result = self.run_skill(
            {
                "skill_action": "move-note",
                "note_id": "n1",
                "new_parent_note_id": "c2",
                "confirm": True,
                "confirm_title": "Target",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "ambiguous_branch")
        self.assertNotIn("POST", [item["method"] for item in FakeTriliumHandler.requests])

    def test_move_note_rejects_cycle_before_write(self) -> None:
        proc, result = self.run_skill(
            {
                "skill_action": "move-note",
                "note_id": "n1",
                "new_parent_note_id": "g1",
                "confirm": True,
                "confirm_title": "Target",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "hierarchy_cycle")
        self.assertNotIn("POST", [item["method"] for item in FakeTriliumHandler.requests])

    def test_move_note_rejects_existing_target_branch(self) -> None:
        FakeTriliumHandler.notes["n1"]["parentBranchIds"].append("b-extra")
        FakeTriliumHandler.notes["n1"]["parentNoteIds"].append("parent2")
        FakeTriliumHandler.branches["b-extra"] = {
            "branchId": "b-extra",
            "noteId": "n1",
            "parentNoteId": "parent2",
            "prefix": "",
            "notePosition": 1,
            "isExpanded": False,
        }
        proc, result = self.run_skill(
            {
                "skill_action": "move-note",
                "note_id": "n1",
                "new_parent_note_id": "parent2",
                "branch_id": "b1",
                "confirm": True,
                "confirm_title": "Target",
            }
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "already_under_target_parent")
        self.assertNotIn("POST", [item["method"] for item in FakeTriliumHandler.requests])

    def test_unknown_action_fails_with_supported_actions(self) -> None:
        proc, result = self.run_skill({"skill_action": "raw-request"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "unsupported_action")
        self.assertNotIn("raw-request", result["details"]["supported_actions"])

    def test_non_object_input_fails(self) -> None:
        proc, result = self.run_skill(["status"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(result["code"], "bad_json")


if __name__ == "__main__":
    unittest.main()
