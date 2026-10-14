"""Offline tests: mock both Entra ID and Azure HTTP; no real API is called."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from azure.core.credentials import AccessToken
from azure.identity import CredentialUnavailableError
from fastapi.testclient import TestClient
import httpx

from server import (
    azure_endpoint_variants,
    create_app,
    create_azure_cli_credential,
    discover_azure_environments,
    load_config,
    TokenManager,
    TokenProvider,
)
from history_store import HistoryStore
from specification import API_DOCUMENTATION, load_official_spec, operations, TAG_DESCRIPTIONS

ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:8765"


class FakeCredential:
    def __init__(self):
        self.calls = 0
        self.failure = False

    async def get_token(self, *scopes):
        self.calls += 1
        if self.failure:
            raise CredentialUnavailableError("No az login session")
        return AccessToken("offline-test-token", int(time.time()) + 3600)

    async def close(self):
        pass


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.credential = FakeCredential()
        self.requests = []
        self.mode = "json"
        self.history_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.history_directory.cleanup)
        self.history_path = Path(self.history_directory.name) / "history.sqlite3"
        config = load_config()
        config["port"] = 8765
        config["max_upload_mb"] = 1

        async def handle(request):
            self.requests.append(request)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("mock timeout", request=request)
            if self.mode == "network":
                raise httpx.ConnectError("mock network", request=request)
            if self.mode == "redirect":
                return httpx.Response(302, headers={"location": "https://example.com"})
            if self.mode == "forbidden":
                return httpx.Response(403, json={"error": {"message": "Mock RBAC denied"}})
            if self.mode == "unauthorized":
                return httpx.Response(401, json={"error": {"message": "Mock expired token"}})
            if self.mode == "sse":
                return httpx.Response(200, text='data: {"delta":"hello"}\n\ndata: [DONE]\n\n',
                                      headers={"content-type": "text/event-stream"})
            if self.mode == "binary":
                return httpx.Response(200, content=b"\x00\xff\x80",
                                      headers={"content-type": "application/octet-stream",
                                               "content-disposition": 'attachment; filename="test.bin"'})
            return httpx.Response(200, json={"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "offline test"}]}]},
                headers={"apim-request-id": "upstream-test-id"})

        async def load_accounts():
            return [{
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "subscription_id": "22222222-2222-2222-2222-222222222222",
                "subscription_name": "Offline Test",
                "tenant_name": "Offline Tenant",
                "is_default": True,
            }]

        async def load_environments(subscription_id, access_token, client):
            self.assertEqual(subscription_id, "22222222-2222-2222-2222-222222222222")
            self.assertEqual(access_token, "offline-test-token")
            return [{
                "resource_id": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/alternate",
                "resource_name": "alternate",
                "endpoint": "https://alternate.openai.azure.com",
                "endpoint_type": "Azure OpenAI API",
                "location": "eastus",
                "deployments": [{
                    "name": "alternate-gpt",
                    "model_name": "gpt-test",
                    "model_version": "1",
                    "provisioning_state": "Succeeded",
                }],
            }]

        self.app = create_app(
            config,
            self.credential,
            httpx.MockTransport(handle),
            load_accounts,
            load_environments,
            history_path=self.history_path,
        )
        self.client = TestClient(self.app, base_url=BASE_URL)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.session = self.client.get("/api/status").json()["session_token"]

    def call(self, method="POST", path="/responses", **kwargs):
        headers = {"X-Lab-Session": self.session, **kwargs.pop("headers", {})}
        return self.client.request(method, "/proxy/openai/v1" + path, headers=headers, **kwargs)

    def history(self, method="POST", path_template="/responses", **params):
        return self.client.get("/api/history", headers={"X-Lab-Session": self.session}, params={
            "document_id": "azure-openai-v1",
            "method": method,
            "path_template": path_template,
            **params,
        })

    def test_startup_and_static_are_offline(self):
        for path in [
            "/",
            "/static/favicon.svg",
            "/static/site.js",
            "/static/site.css",
            "/vendor/swagger-ui-bundle.js",
            "/vendor/swagger-ui.css",
        ]:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.credential.calls, 0)
        self.assertFalse(self.requests)

    def test_schema_covers_all_official_operations_and_local_refs(self):
        spec = self.client.get("/api/openapi.json").json()
        official = load_official_spec()
        self.assertEqual(set(t["name"] for t in spec["tags"]), set(TAG_DESCRIPTIONS))
        self.assertEqual(len(list(operations(spec))), len(list(operations(official))))
        self.assertEqual(spec["servers"][0]["url"], "/proxy/openai/v1")
        self.assertEqual(spec["security"], [])
        self.assertNotIn("securitySchemes", spec["components"])
        self.assertEqual(spec["components"]["schemas"], official["components"]["schemas"])
        self.assertGreater(len(list(operations(spec))), 100)
        for _, _, operation in operations(spec):
            tag = operation["tags"][0]
            documentation = API_DOCUMENTATION[tag]
            description = operation["description"]
            self.assertIn("**官方文档：**", description)
            if "foundry" in documentation:
                self.assertIn(f"[Microsoft Foundry 文档]({documentation['foundry']})", description)
            else:
                self.assertNotIn("[Microsoft Foundry 文档]", description)
            if "openai" in documentation:
                self.assertIn(f"[OpenAI 文档]({documentation['openai']})", description)
            else:
                self.assertNotIn("[OpenAI 文档]", description)

        def validate_refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    reference = value["$ref"]
                    self.assertTrue(reference.startswith("#/"), reference)
                    node = spec
                    for segment in reference[2:].split("/"):
                        node = node[segment.replace("~1", "/").replace("~0", "~")]
                for child in value.values():
                    validate_refs(child)
            elif isinstance(value, list):
                for child in value:
                    validate_refs(child)
        validate_refs(spec)

    def test_explorer_collections_have_exact_operations_and_valid_refs(self):
        catalog = self.client.get("/api/explorer")
        self.assertEqual(catalog.status_code, 200)
        documents = catalog.json()["documents"]
        self.assertEqual([document["id"] for document in documents], ["azure-openai-v1"])
        expected = {
            "inference": {("get", "/models"), ("post", "/responses"), ("post", "/chat/completions")},
            "batch": {
                ("post", "/files"), ("post", "/batches"),
                ("get", "/batches/{batch_id}"), ("get", "/files/{file_id}/content"),
            },
            "vector": {
                ("post", "/embeddings"), ("post", "/files"), ("post", "/vector_stores"),
                ("post", "/vector_stores/{vector_store_id}/files"),
                ("get", "/vector_stores/{vector_store_id}/files/{file_id}"),
                ("post", "/vector_stores/{vector_store_id}/search"),
            },
        }
        original = self.client.get("/api/openapi.json").json()
        expected["all"] = {(method, path) for path, method, _ in operations(original)}
        self.assertEqual([item["id"] for item in documents[0]["collections"]],
                         ["all", "inference", "batch", "vector"])
        for collection in documents[0]["collections"]:
            with self.subTest(collection=collection["id"]):
                response = self.client.get(collection["schema_url"])
                self.assertEqual(response.status_code, 200)
                spec = response.json()
                actual = {(method, path) for path, method, _ in operations(spec)}
                self.assertEqual(actual, expected[collection["id"]])
                self.assertEqual(collection["operation_count"], len(actual))
                self.assertEqual(spec["components"], original["components"])
                self.assertEqual(spec["servers"], original["servers"])
                self.assertEqual([tag["name"] for tag in spec["tags"]], collection["tags"])
                for step in collection["steps"]:
                    operation = spec["paths"][step["path"]][step["method"].lower()]
                    self.assertEqual(operation["operationId"], step["operation_id"])
                    self.assertIn(step["tag"], operation["tags"])

                def validate_refs(value):
                    if isinstance(value, dict):
                        if "$ref" in value:
                            self.assertTrue(value["$ref"].startswith("#/"))
                            node = spec
                            for segment in value["$ref"][2:].split("/"):
                                node = node[segment.replace("~1", "/").replace("~0", "~")]
                        for child in value.values():
                            validate_refs(child)
                    elif isinstance(value, list):
                        for child in value:
                            validate_refs(child)

                validate_refs(spec)
        self.assertFalse(self.requests)
        self.assertEqual(self.credential.calls, 0)

    def test_collection_examples_are_isolated_and_upload_schema_is_preserved(self):
        batch = self.client.get("/api/openapi.json?collection=batch").json()
        vector = self.client.get("/api/openapi.json?collection=vector").json()
        full = self.client.get("/api/openapi.json").json()
        def example(spec, path):
            return spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["example"]
        self.assertEqual(example(batch, "/batches")["endpoint"], "/v1/responses")
        self.assertEqual(example(full, "/batches")["endpoint"], "/v1/chat/completions")
        self.assertEqual(example(vector, "/vector_stores/{vector_store_id}/files")["file_id"], "file-REPLACE-ME")
        for spec in (batch, vector):
            schema = spec["paths"]["/files"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
            self.assertEqual(schema["properties"]["file"]["format"], "binary")
            self.assertEqual(
                spec["paths"]["/files"]["post"]["description"],
                full["paths"]["/files"]["post"]["description"],
            )

    def test_unknown_explorer_selection_is_rejected_without_azure_requests(self):
        for params in ({"document": "preview"}, {"collection": "missing"},
                       {"document": "../vendor/source.json"}):
            result = self.client.get("/api/openapi.json", params=params)
            self.assertEqual(result.status_code, 404)
            self.assertEqual(result.json()["error"]["source"], "local-proxy")
        self.assertFalse(self.requests)
        self.assertEqual(self.credential.calls, 0)

    def test_official_snapshot_hash(self):
        source = json.loads((ROOT / "vendor" / "source.json").read_text("utf-8"))
        actual = hashlib.sha256((ROOT / "vendor" / "azure-openai-v1.json").read_bytes()).hexdigest()
        self.assertEqual(actual, source["sha256"])

    def test_upload_ui_has_binary_and_bracket_fields(self):
        spec = self.client.get("/api/openapi.json").json()
        for path in ("/files", "/containers/{container_id}/files"):
            schema = spec["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
            self.assertEqual(schema["properties"]["file"]["format"], "binary")
        file_schema = spec["paths"]["/files"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        self.assertNotIn("expires_after", file_schema["properties"])
        self.assertIn("expires_after[seconds]", file_schema["required"])
        self.assertIn("expires_after[anchor]", file_schema["required"])

    def test_auth_exposes_token_for_copying(self):
        result = self.client.post("/api/auth", headers={"X-Lab-Session": self.session}, json={
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "subscription_id": "22222222-2222-2222-2222-222222222222",
        })
        self.assertEqual(result.status_code, 200)
        self.assertIn("expires_at", result.json())
        self.assertEqual(result.json()["access_token"], "offline-test-token")
        self.assertEqual(result.json()["subscription_id"], "22222222-2222-2222-2222-222222222222")
        self.assertFalse(self.requests)

    def test_lists_cli_accounts_without_exposing_token(self):
        result = self.client.get("/api/azure-accounts", headers={"X-Lab-Session": self.session})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["accounts"][0]["subscription_name"], "Offline Test")
        self.assertNotIn("token", result.text.lower())

    def test_discovers_environment_and_routes_with_selected_deployment(self):
        result = self.client.get("/api/azure-environments", headers={
            "X-Lab-Session": self.session,
        }, params={
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "subscription_id": "22222222-2222-2222-2222-222222222222",
        })
        self.assertEqual(result.status_code, 200)
        environment = result.json()["environments"][0]
        self.assertNotIn("resource_id", environment)
        self.assertEqual(environment["endpoint_type"], "Azure OpenAI API")
        response = self.call(path="/responses", json={"model": "old", "input": "test"}, headers={
            "X-Lab-Environment": environment["environment_id"],
            "X-Lab-Deployment": "alternate-gpt",
        })
        self.assertEqual(response.status_code, 200)
        sent = self.requests[-1]
        self.assertEqual(str(sent.url), "https://alternate.openai.azure.com/openai/v1/responses")
        self.assertEqual(json.loads(sent.content)["model"], "alternate-gpt")
        self.assertNotIn("x-lab-environment", sent.headers)
        self.assertNotIn("x-lab-deployment", sent.headers)
        record = self.history().json()["records"][0]
        self.assertTrue(record["forwarded"])
        detail = self.client.get(
            f"/api/history/{record['id']}",
            headers={"X-Lab-Session": self.session},
        ).json()
        self.assertEqual(
            detail["forwarded_url"],
            "https://alternate.openai.azure.com/openai/v1/responses",
        )
        self.assertEqual(detail["forwarded_headers"]["authorization"], "<redacted>")
        self.assertNotIn("offline-test-token", json.dumps(detail))
        forwarded_body = self.client.get(
            f"/api/history/{record['id']}/forwarded-request",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(forwarded_body.json()["model"], "alternate-gpt")

    def test_rejects_unknown_environment_and_deployment(self):
        unknown = self.call(headers={"X-Lab-Environment": "unknown"})
        self.assertEqual(unknown.status_code, 400)
        self.assertFalse(self.requests)

        result = self.client.get("/api/azure-environments", headers={
            "X-Lab-Session": self.session,
        }, params={
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "subscription_id": "22222222-2222-2222-2222-222222222222",
        })
        environment_id = result.json()["environments"][0]["environment_id"]
        invalid_deployment = self.call(headers={
            "X-Lab-Environment": environment_id,
            "X-Lab-Deployment": "not-in-resource",
        })
        self.assertEqual(invalid_deployment.status_code, 400)
        self.assertFalse(self.requests)

    def test_proxy_transmits_body_query_and_server_credential(self):
        payload = {"model": "example", "input": "你好", "stream": False}
        result = self.call(path="/responses?api-version=v1", json=payload, headers={
            "Authorization": "Bearer browser-value",
            "api-key": "browser-key",
            "Cookie": "private=value",
            "x-ms-client-request-id": "custom-request-123",
        })
        self.assertEqual(result.status_code, 200)
        sent = self.requests[-1]
        self.assertEqual(str(sent.url), self.app.state.config["endpoint"] + "/openai/v1/responses?api-version=v1")
        self.assertEqual(json.loads(sent.content), payload)
        self.assertEqual(sent.headers["authorization"], "Bearer offline-test-token")
        for header in ["api-key", "cookie", "x-lab-session", "x-lab-allow-mutations"]:
            self.assertNotIn(header, sent.headers)
        self.assertEqual(result.headers["x-ms-client-request-id"], "custom-request-123")
        self.assertEqual(result.headers["apim-request-id"], "upstream-test-id")

    def test_generated_request_id_and_token_cache(self):
        first = self.call(json={"input": "one"})
        second = self.call(json={"input": "two"})
        self.assertNotEqual(first.headers["x-ms-client-request-id"], second.headers["x-ms-client-request-id"])
        self.assertEqual(self.credential.calls, 1)

    def test_block_foreign_host_origin_and_missing_session(self):
        cases = [
            self.client.get("/api/status", headers={"Host": "evil.test:8765"}),
            self.client.get("/api/status", headers={"Origin": "https://evil.test"}),
            self.client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"}),
            self.client.post("/api/auth"),
            self.client.get("/api/azure-accounts"),
            self.client.get("/api/history/settings"),
            self.client.get("/proxy/openai/v1/models"),
            self.client.post("/proxy/openai/v1/responses", headers={"X-Lab-Session": "wrong"}),
        ]
        self.assertTrue(all(response.status_code == 403 for response in cases))
        self.assertFalse(self.requests)
        self.assertEqual(self.credential.calls, 0)

    def test_mutation_gate_applies_to_delete_and_training(self):
        for method, path in [("DELETE", "/files/file-123"), ("POST", "/fine_tuning/jobs"),
                             ("POST", "/batches"), ("POST", "/conversations")]:
            self.assertEqual(self.call(method, path).status_code, 403)
        self.assertFalse(self.requests)
        self.assertEqual(self.call("DELETE", "/files/file-123",
                                  headers={"X-Lab-Allow-Mutations": "true"}).status_code, 200)

    def test_unknown_paths_and_methods_rejected(self):
        for method, path in [("GET", "/not-an-api"), ("DELETE", "/responses"),
                             ("POST", "/models"), ("GET", "/files/%252e%252e"),
                             ("GET", "/files/abc%5Cdef")]:
            self.assertIn(self.call(method, path).status_code, (400, 404))
        self.assertFalse(self.requests)

    def test_invalid_request_id_rejected(self):
        response = self.call(headers={"x-ms-client-request-id": "bad id"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.requests)

    def test_oversized_upload_rejected_before_auth(self):
        response = self.call(path="/files", content=b"x" * (1024 * 1024 + 1),
                             headers={"X-Lab-Allow-Mutations": "true"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.credential.calls, 0)

    def test_multipart_upload_preserved(self):
        body = b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="t.txt"\r\n\r\nhello\r\n--boundary--\r\n'
        response = self.call(path="/files", content=body, headers={
            "X-Lab-Allow-Mutations": "true",
            "Content-Type": "multipart/form-data; boundary=boundary",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[-1].content, body)
        self.assertIn("boundary=boundary", self.requests[-1].headers["content-type"])

    def test_binary_download_preserved(self):
        self.mode = "binary"
        response = self.call("GET", "/files/file-123/content")
        self.assertEqual(response.content, b"\x00\xff\x80")
        self.assertIn("test.bin", response.headers["content-disposition"])

    def test_sse_preserved(self):
        self.mode = "sse"
        response = self.call(json={"stream": True})
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(response.text, 'data: {"delta":"hello"}\n\ndata: [DONE]\n\n')

    def test_azure_error_passthrough_and_no_retry(self):
        self.mode = "forbidden"
        response = self.call()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["message"], "Mock RBAC denied")
        self.assertEqual(len(self.requests), 1)

    def test_unauthorized_invalidates_token_without_replaying(self):
        self.mode = "unauthorized"
        self.assertEqual(self.call().status_code, 401)
        self.mode = "json"
        self.assertEqual(self.call().status_code, 200)
        self.assertEqual(self.credential.calls, 2)
        self.assertEqual(len(self.requests), 2)

    def test_auth_failure_visible_without_forwarding(self):
        self.credential.failure = True
        response = self.call()
        self.assertEqual(response.status_code, 401)
        self.assertIn("Entra ID", response.json()["error"]["message"])
        self.assertFalse(self.requests)

    def test_network_timeout_and_redirect_do_not_retry(self):
        for mode, expected in [("timeout", 504), ("network", 502), ("redirect", 502)]:
            self.mode = mode
            response = self.call()
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("location", response.headers)
        self.assertEqual(len(self.requests), 3)

    def test_history_persists_original_request_response_and_restore_snapshot(self):
        response = self.call(
            path="/responses?api-version=v1",
            json={"model": "browser-model", "input": "history test"},
            headers={
                "Authorization": "******",
                "api-key": "must-not-be-stored",
                "Cookie": "must-not-be-stored",
                "x-ms-client-request-id": "history-request-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        listed = self.history()
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        record = listed.json()["records"][0]
        self.assertEqual(record["response_status"], 200)
        self.assertTrue(record["completed"])
        self.assertGreater(record["response_size"], 0)

        detail = self.client.get(
            f"/api/history/{record['id']}",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(detail.status_code, 200)
        stored_headers = detail.json()["request_headers"]
        self.assertEqual(stored_headers["content-type"], "application/json")
        self.assertEqual(stored_headers["x-ms-client-request-id"], "history-request-1")
        self.assertNotIn("authorization", stored_headers)
        self.assertNotIn("api-key", stored_headers)
        self.assertNotIn("cookie", stored_headers)
        self.assertFalse(any(name.startswith("x-lab-") for name in stored_headers))

        request_body = self.client.get(
            f"/api/history/{record['id']}/request",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(request_body.json()["model"], "browser-model")
        response_body = self.client.get(
            f"/api/history/{record['id']}/response",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(response_body.json()["output"][0]["type"], "message")

        snapshot = self.client.get(
            f"/api/history/{record['id']}/restore",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["query_parameters"], {"api-version": ["v1"]})
        self.assertIn('"browser-model"', snapshot.json()["body_text"])

    def test_history_records_generated_client_request_id(self):
        response = self.call(json={"model": "browser-model", "input": "generated id"})
        record = self.history().json()["records"][0]
        detail = self.client.get(
            f"/api/history/{record['id']}",
            headers={"X-Lab-Session": self.session},
        )

        self.assertEqual(
            detail.json()["request_headers"]["x-ms-client-request-id"],
            response.headers["x-ms-client-request-id"],
        )

    def test_history_saves_binary_and_applies_count_retention(self):
        self.mode = "binary"
        for _ in range(3):
            response = self.call("GET", "/files/file-123/content")
            self.assertEqual(response.content, b"\x00\xff\x80")
        listed = self.history("GET", "/files/{file_id}/content")
        self.assertEqual(listed.json()["total"], 3)
        record = listed.json()["records"][0]
        saved = self.client.get(
            f"/api/history/{record['id']}/response",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(saved.content, b"\x00\xff\x80")

        settings = self.client.put(
            "/api/history/settings",
            headers={"X-Lab-Session": self.session},
            json={"retention_mode": "max_count", "retention_value": 2},
        )
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["deleted_count"], 1)
        self.assertEqual(self.history("GET", "/files/{file_id}/content").json()["total"], 2)

    def test_history_settings_validation_and_record_deletion(self):
        invalid = self.client.put(
            "/api/history/settings",
            headers={"X-Lab-Session": self.session},
            json={"retention_mode": "max_age_days", "retention_value": 3651},
        )
        self.assertEqual(invalid.status_code, 400)
        self.call(json={"input": "delete me"})
        record_id = self.history().json()["records"][0]["id"]
        deleted = self.client.delete(
            f"/api/history/{record_id}",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.history().json()["total"], 0)

    def test_history_age_retention_removes_expired_records(self):
        self.call(json={"input": "old history"})
        connection = sqlite3.connect(self.history_path)
        try:
            connection.execute(
                "UPDATE request_history SET created_at = '2000-01-01T00:00:00.000Z'"
            )
            connection.commit()
        finally:
            connection.close()
        settings = self.client.put(
            "/api/history/settings",
            headers={"X-Lab-Session": self.session},
            json={"retention_mode": "max_age_days", "retention_value": 1},
        )
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["deleted_count"], 1)
        self.assertEqual(self.history().json()["total"], 0)

    def test_history_restore_snapshot_describes_multipart_file(self):
        body = (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="purpose"\r\n\r\nassistants\r\n'
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="history.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\nsaved file\r\n"
            b"--boundary--\r\n"
        )
        response = self.call(path="/files", content=body, headers={
            "X-Lab-Allow-Mutations": "true",
            "Content-Type": "multipart/form-data; boundary=boundary",
        })
        self.assertEqual(response.status_code, 200)
        record_id = self.history("POST", "/files").json()["records"][0]["id"]
        snapshot = self.client.get(
            f"/api/history/{record_id}/restore",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["form_fields"], {"purpose": ["assistants"]})
        self.assertEqual(snapshot.json()["files"], [{
            "field": "file",
            "filename": "history.txt",
            "content_type": "text/plain",
            "size": 10,
        }])
        stored = self.client.get(
            f"/api/history/{record_id}/request",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(stored.content, body)

    def test_history_records_local_write_protection_error(self):
        response = self.call(path="/batches", json={"input_file_id": "file-123"})
        self.assertEqual(response.status_code, 403)
        record = self.history("POST", "/batches").json()["records"][0]
        self.assertFalse(record["forwarded"])
        self.assertTrue(record["forwarded_capture_available"])
        self.assertEqual(record["response_status"], 403)
        stored = self.client.get(
            f"/api/history/{record['id']}/request",
            headers={"X-Lab-Session": self.session},
        )
        self.assertEqual(stored.json()["input_file_id"], "file-123")


class HistoryPersistenceTests(unittest.TestCase):
    def test_initialize_preserves_existing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            first_store = HistoryStore(path)
            first_store.initialize()
            history_id, _ = first_store.create_record(
                document_id="azure-openai-v1",
                operation_id="createResponse",
                method="POST",
                path_template="/responses",
                actual_path="/responses",
                query_string="",
                request_headers={"content-type": "application/json"},
                request_content_type="application/json",
                request_body=b'{"input":"persist me"}',
            )

            restarted_store = HistoryStore(path)
            restarted_store.initialize()

            total, records = restarted_store.list_records(
                "azure-openai-v1", "POST", "/responses", 10, 0
            )
            self.assertEqual(total, 1)
            self.assertEqual(records[0]["id"], history_id)
            self.assertEqual(
                restarted_store.get_request_body(history_id),
                b'{"input":"persist me"}',
            )


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_credential_uses_subscription_without_tenant(self):
        credential = create_azure_cli_credential(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(credential.tenant_id, "")
        self.assertEqual(credential.subscription, "22222222-2222-2222-2222-222222222222")

    async def test_concurrent_requests_share_token(self):
        credential = FakeCredential()
        provider = TokenProvider(credential, "https://ai.azure.com/.default")
        await asyncio.gather(*(provider.get() for _ in range(10)))
        self.assertEqual(credential.calls, 1)
        provider.token = AccessToken("expired", int(time.time()) - 1)
        token = await provider.get()
        self.assertEqual(token.token, "offline-test-token")
        self.assertEqual(credential.calls, 2)

    async def test_token_manager_switches_and_reuses_accounts(self):
        credentials = {}

        def factory(tenant_id, subscription_id):
            key = (tenant_id, subscription_id)
            credentials[key] = FakeCredential()
            return credentials[key]

        manager = TokenManager(factory, "https://ai.azure.com/.default")
        first = ("tenant-one", "11111111-1111-1111-1111-111111111111")
        second = ("tenant-two", "22222222-2222-2222-2222-222222222222")
        await manager.select(*first)
        await manager.select(*second)
        await manager.select(*first)
        self.assertEqual(credentials[first].calls, 1)
        self.assertEqual(credentials[second].calls, 1)
        self.assertEqual((await manager.get()).token, "offline-test-token")
        self.assertEqual(credentials[first].calls, 1)
        await manager.close()


class EnvironmentDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_only_valid_openai_resources_and_deployments(self):
        subscription_id = "22222222-2222-2222-2222-222222222222"

        async def handle(request):
            if request.url.path.endswith("/accounts"):
                return httpx.Response(200, json={"value": [
                    {
                        "id": f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/valid",
                        "name": "valid",
                        "kind": "OpenAI",
                        "location": "eastus",
                        "properties": {
                            "endpoint": "https://valid.openai.azure.com/",
                            "endpoints": {
                                "Azure AI Model Inference API": "https://valid.services.ai.azure.com/",
                                "Azure OpenAI API": "https://valid.openai.azure.com",
                                "Duplicate OpenAI API": "https://valid.openai.azure.com/",
                                "Unsafe API": "https://example.com",
                            },
                        },
                    },
                    {
                        "id": f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/unsafe",
                        "name": "unsafe",
                        "kind": "OpenAI",
                        "location": "eastus",
                        "properties": {"endpoint": "https://example.com"},
                    },
                    {
                        "id": f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/search",
                        "name": "search",
                        "kind": "CognitiveServices",
                        "location": "eastus",
                        "properties": {"endpoint": "https://search.cognitiveservices.azure.com"},
                    },
                ]})
            return httpx.Response(200, json={"value": [
                {
                    "name": "ready",
                    "properties": {
                        "model": {"format": "OpenAI", "name": "gpt-test", "version": "1"},
                        "provisioningState": "Succeeded",
                    },
                },
                {
                    "name": "failed",
                    "properties": {
                        "model": {"format": "OpenAI", "name": "gpt-test", "version": "1"},
                        "provisioningState": "Failed",
                    },
                },
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            environments = await discover_azure_environments(
                subscription_id,
                "management-token",
                client,
            )
        self.assertEqual(len(environments), 2)
        self.assertEqual(
            [(item["endpoint_type"], item["endpoint"]) for item in environments],
            [
                ("Azure AI Model Inference API", "https://valid.services.ai.azure.com"),
                ("Azure OpenAI API", "https://valid.openai.azure.com"),
            ],
        )
        for environment in environments:
            self.assertEqual([item["name"] for item in environment["deployments"]], ["ready"])

    def test_endpoint_variants_fall_back_to_primary_endpoint(self):
        self.assertEqual(
            azure_endpoint_variants({
                "endpoint": "https://primary.cognitiveservices.azure.com/",
                "endpoints": ["invalid"],
            }),
            [{
                "endpoint": "https://primary.cognitiveservices.azure.com",
                "endpoint_type": "默认",
            }],
        )


class ConfigTests(unittest.TestCase):
    def test_invalid_endpoint_is_rejected(self):
        from unittest.mock import patch
        for endpoint in ["http://example.com", "https://example.com",
                         "https://a.openai.azure.com/openai/v1",
                         "https://a.openai.azure.com?redirect=bad",
                         "https://user:pass@a.openai.azure.com"]:
            config = copy.deepcopy(load_config())
            config["endpoint"] = endpoint
            with patch.object(Path, "read_text", return_value=json.dumps(config)):
                with self.assertRaises(ValueError):
                    load_config()


if __name__ == "__main__":
    unittest.main()
