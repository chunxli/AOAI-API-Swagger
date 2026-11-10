"""Loopback-only Azure OpenAI Swagger UI with server-side Entra ID authentication."""

import asyncio
import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import json
import logging
from pathlib import Path
import re
import secrets
import shutil
import sys
import time
from urllib.parse import parse_qsl, quote, unquote, urlsplit
import uuid

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from azure.identity.aio import AzureCliCredential
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
import uvicorn

from history_store import HistoryStore, is_text_content
from startup_settings import StartupManager, StartupSettingsError
from specification import build_explorer, load_official_spec, needs_unlock, operations

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("azure_openai_lab")
AUTH_ERRORS = (CredentialUnavailableError, ClientAuthenticationError)
ARM_SCOPE = "https://management.azure.com/.default"
ARM_API_VERSION = "2024-10-01"
MODEL_OVERRIDE_PATHS = {"/responses", "/chat/completions", "/completions", "/embeddings"}
HISTORY_DOCUMENT_ID = "azure-openai-v1"
HISTORY_WRITE_BUFFER_BYTES = 256 * 1024
DEFAULT_CONFIG = {
    "endpoint": "",
    "deployment": "",
    "embedding_deployment": "",
    "tenant_id": "",
    "token_scope": "https://ai.azure.com/.default",
    "port": 8765,
    "request_timeout_seconds": 180,
    "max_upload_mb": 32,
}
SENSITIVE_LOG_HEADERS = {
    "authorization",
    "api-key",
    "cookie",
    "proxy-authorization",
    "x-lab-session",
}


class AzureCliAccountError(Exception):
    pass


class ManagementDiscoveryError(Exception):
    pass


def normalize_azure_endpoint(endpoint):
    if not isinstance(endpoint, str):
        return None
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith((
            ".openai.azure.com",
            ".services.ai.azure.com",
            ".cognitiveservices.azure.com",
        ))
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return None
    return endpoint.rstrip("/")


def redact_log_headers(headers):
    return {
        name: "<redacted>" if name.lower() in SENSITIVE_LOG_HEADERS else value
        for name, value in headers.items()
    }


def azure_endpoint_variants(properties):
    variants = []
    seen = set()
    named_endpoints = properties.get("endpoints")
    if isinstance(named_endpoints, dict):
        for endpoint_type, endpoint in sorted(named_endpoints.items()):
            if not isinstance(endpoint_type, str) or not endpoint_type.strip():
                continue
            normalized = normalize_azure_endpoint(endpoint)
            if normalized and normalized not in seen:
                variants.append({
                    "endpoint": normalized,
                    "endpoint_type": endpoint_type.strip(),
                })
                seen.add(normalized)

    primary = normalize_azure_endpoint(properties.get("endpoint"))
    if primary and primary not in seen:
        variants.append({
            "endpoint": primary,
            "endpoint_type": "默认",
        })
    return variants


async def list_azure_cli_accounts():
    executable = (
        shutil.which("az.cmd") or shutil.which("az")
        if sys.platform.startswith("win")
        else shutil.which("az")
    )
    if not executable:
        raise AzureCliAccountError("无法启动 Azure CLI。请确认已安装 az 并完成登录。")
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "account",
            "list",
            "--output",
            "json",
            "--only-show-errors",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AzureCliAccountError("无法启动 Azure CLI。请确认已安装 az 并完成登录。") from exc
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise AzureCliAccountError("读取 Azure CLI 账户超时。请在终端检查 az account list。") from exc
    if process.returncode != 0:
        raise AzureCliAccountError("无法读取 Azure CLI 账户。请运行 az login 后重试。")
    try:
        raw_accounts = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AzureCliAccountError("Azure CLI 返回了无效的账户数据。") from exc
    if not isinstance(raw_accounts, list):
        raise AzureCliAccountError("Azure CLI 返回了非预期的账户数据。")

    accounts = []
    for account in raw_accounts:
        if not isinstance(account, dict) or account.get("state") != "Enabled":
            continue
        tenant_id = account.get("tenantId")
        subscription_id = account.get("id")
        name = account.get("name")
        if not all(isinstance(value, str) and value for value in (tenant_id, subscription_id, name)):
            continue
        accounts.append({
            "tenant_id": tenant_id,
            "subscription_id": subscription_id,
            "subscription_name": name,
            "tenant_name": account.get("tenantDisplayName") or "",
            "is_default": account.get("isDefault") is True,
        })
    accounts.sort(key=lambda account: (account["tenant_name"], account["subscription_name"]))
    return accounts


async def discover_azure_environments(subscription_id, access_token, client):
    async def list_values(url):
        values = []
        next_url = url
        while next_url:
            parsed = urlsplit(next_url)
            if parsed.scheme != "https" or parsed.netloc != "management.azure.com":
                raise ManagementDiscoveryError("Azure 管理 API 返回了无效的分页地址。")
            try:
                response = await client.get(
                    next_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.TimeoutException as exc:
                raise ManagementDiscoveryError("查询 Azure 资源超时。") from exc
            except httpx.RequestError as exc:
                raise ManagementDiscoveryError("无法连接 Azure Resource Manager。") from exc
            if response.status_code != 200:
                raise ManagementDiscoveryError(
                    f"Azure Resource Manager 查询失败：HTTP {response.status_code}。请检查订阅读取权限。"
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise ManagementDiscoveryError("Azure Resource Manager 返回了无效 JSON。") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
                raise ManagementDiscoveryError("Azure Resource Manager 返回了非预期数据。")
            values.extend(payload["value"])
            next_url = payload.get("nextLink")
            if next_url is not None and not isinstance(next_url, str):
                raise ManagementDiscoveryError("Azure Resource Manager 返回了无效的分页地址。")
        return values

    accounts_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CognitiveServices/accounts?api-version={ARM_API_VERSION}"
    )
    environments = []
    for account in await list_values(accounts_url):
        if not isinstance(account, dict):
            continue
        if account.get("kind") not in {"OpenAI", "AIServices"}:
            continue
        resource_id = account.get("id")
        resource_name = account.get("name")
        properties = account.get("properties")
        if not isinstance(properties, dict):
            continue
        endpoint_variants = azure_endpoint_variants(properties)
        if not endpoint_variants or not isinstance(resource_id, str) or not isinstance(resource_name, str):
            continue
        deployments_url = (
            "https://management.azure.com"
            + quote(resource_id.rstrip("/") + "/deployments", safe="/-._~:")
            + f"?api-version={ARM_API_VERSION}"
        )
        deployments = []
        for deployment in await list_values(deployments_url):
            if not isinstance(deployment, dict) or not isinstance(deployment.get("name"), str):
                continue
            deployment_properties = deployment.get("properties")
            if not isinstance(deployment_properties, dict):
                continue
            model = deployment_properties.get("model")
            if not isinstance(model, dict) or model.get("format") != "OpenAI":
                continue
            provisioning_state = deployment_properties.get("provisioningState") or ""
            if provisioning_state and provisioning_state != "Succeeded":
                continue
            deployments.append({
                "name": deployment["name"],
                "model_name": model.get("name") or "",
                "model_version": model.get("version") or "",
                "provisioning_state": provisioning_state,
            })
        deployments.sort(key=lambda deployment: deployment["name"].lower())
        for endpoint_variant in endpoint_variants:
            environments.append({
                "resource_id": resource_id,
                "resource_name": resource_name,
                **endpoint_variant,
                "location": account.get("location") or "",
                "deployments": deployments,
            })
    environments.sort(key=lambda environment: (
        environment["resource_name"].lower(),
        environment["endpoint_type"].lower(),
        environment["endpoint"],
    ))
    return environments


def load_config(path=None):
    config_path = Path(path or ROOT / "config.json")
    try:
        overrides = json.loads(config_path.read_text("utf-8"))
    except FileNotFoundError:
        overrides = {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{config_path.name}: 必须是 UTF-8 编码的有效 JSON。") from exc
    if not isinstance(overrides, dict):
        raise ValueError(f"{config_path.name}: 配置必须是 JSON 对象。")
    config = {**DEFAULT_CONFIG, **overrides}
    endpoint = config.get("endpoint", "")
    normalized_endpoint = normalize_azure_endpoint(endpoint) if endpoint else ""
    if not isinstance(endpoint, str) or (
        endpoint and (normalized_endpoint is None or urlsplit(normalized_endpoint).path not in ("", "/"))
    ):
        raise ValueError("config.json: endpoint must be an Azure public-cloud HTTPS resource root.")
    for name in ("deployment", "embedding_deployment"):
        if not isinstance(config.get(name), str) or (config[name] and not config[name].strip()):
            raise ValueError(f"config.json: {name} must be a string; use an empty string for automatic discovery.")
    for name, lower, upper in (
        ("port", 1024, 65535),
        ("request_timeout_seconds", 1, 1800),
        ("max_upload_mb", 1, 512),
    ):
        if type(config.get(name)) is not int or not lower <= config[name] <= upper:
            raise ValueError(f"config.json: {name} must be an integer in {lower}..{upper}.")
    if config.get("token_scope") not in {
        "https://ai.azure.com/.default",
        "https://cognitiveservices.azure.com/.default",
    }:
        raise ValueError("config.json: unsupported Azure OpenAI token scope.")
    if not isinstance(config["tenant_id"], str) or (
        config["tenant_id"] and not re.fullmatch(r"[A-Za-z0-9.-]+", config["tenant_id"])
    ):
        raise ValueError("config.json: invalid tenant_id.")
    config["endpoint"] = normalized_endpoint
    return config


class TokenProvider:
    def __init__(self, credential, scope):
        self.credential = credential
        self.scope = scope
        self.token = None
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            if self.token is None or self.token.expires_on < time.time() + 120:
                self.token = await self.credential.get_token(self.scope)
            return self.token

    def invalidate(self):
        self.token = None


class TokenManager:
    def __init__(self, credential_factory, scope, default_tenant="", default_subscription=None):
        self.credential_factory = credential_factory
        self.scope = scope
        self.providers = {}
        self.active_key = (default_tenant, default_subscription)
        self.lock = asyncio.Lock()

    async def _provider(self, key):
        async with self.lock:
            if key not in self.providers:
                self.providers[key] = TokenProvider(self.credential_factory(*key), self.scope)
            return self.providers[key]

    async def select(self, tenant_id, subscription_id):
        key = (tenant_id, subscription_id)
        provider = await self._provider(key)
        token = await provider.get()
        async with self.lock:
            self.active_key = key
        return token

    async def get(self):
        async with self.lock:
            key = self.active_key
        return await (await self._provider(key)).get()

    def invalidate(self):
        provider = self.providers.get(self.active_key)
        if provider:
            provider.invalidate()

    async def close(self):
        credentials = {id(provider.credential): provider.credential for provider in self.providers.values()}
        await asyncio.gather(*(credential.close() for credential in credentials.values()))


def create_azure_cli_credential(tenant_id, subscription_id):
    return AzureCliCredential(
        tenant_id="" if subscription_id else tenant_id,
        subscription=subscription_id,
        process_timeout=30,
    )


def operation_routes(spec):
    routes = []
    for path, method, operation in operations(spec):
        parts = re.split(r"(\{[^}]+\})", path)
        expression = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
        header_names = {
            p["name"].lower()
            for p in spec["paths"][path].get("parameters", []) + operation.get("parameters", [])
            if p.get("in") == "header"
        }
        routes.append((
            method.upper(),
            path,
            operation.get("operationId") or f"{method}-{path}",
            re.compile(expression),
            header_names,
        ))
    return routes


def error(status, message, request_id=None):
    headers = {"x-ms-client-request-id": request_id} if request_id else None
    return JSONResponse({"error": {"source": "local-proxy", "message": message}}, status, headers=headers)


def create_app(
    config=None,
    credential=None,
    transport=None,
    account_loader=None,
    environment_loader=None,
    history_path=None,
):
    config = config or load_config()
    official = load_official_spec()
    explorer_documents, explorer_schemas = build_explorer(config)
    ui_spec = explorer_schemas[("azure-openai-v1", "all")]
    routes = operation_routes(official)
    session_token = secrets.token_urlsafe(32)
    port = config["port"]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in hosts}
    vendor = ROOT / "node_modules" / "swagger-ui-dist"
    history_store = HistoryStore(history_path or ROOT / ".runtime" / "request-history.sqlite3")
    if not (vendor / "swagger-ui-bundle.js").exists():
        raise RuntimeError("Swagger UI assets are missing. Run npm ci in swagger_site first.")

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(history_store.initialize)
        if credential:
            credential_factory = lambda tenant_id, subscription_id: credential
        else:
            credential_factory = create_azure_cli_credential
        app.state.tokens = TokenManager(
            credential_factory,
            config["token_scope"],
            default_tenant=config.get("tenant_id") or "",
        )
        app.state.arm_tokens = TokenManager(credential_factory, ARM_SCOPE)
        app.state.account_loader = account_loader or list_azure_cli_accounts
        app.state.environment_loader = environment_loader or discover_azure_environments
        app.state.environments = {}
        app.state.history = history_store
        app.state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config["request_timeout_seconds"], connect=15),
            follow_redirects=False,
            transport=transport,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()
            await app.state.tokens.close()
            await app.state.arm_tokens.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.startup = StartupManager(ROOT)
    startup_lock = asyncio.Lock()

    @app.middleware("http")
    async def local_only(request, call_next):
        if request.headers.get("host") not in hosts:
            return error(403, "Only the configured loopback Host is allowed.")
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return error(403, "Cross-origin requests are not allowed.")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return error(403, "Cross-site requests are not allowed.")
        path = request.url.path
        if path.startswith("/proxy/") or path in {
            "/api/auth",
            "/api/azure-accounts",
            "/api/azure-environments",
            "/api/settings/startup",
        } or path.startswith("/api/history"):
            supplied = request.headers.get("x-lab-session", "")
            if not secrets.compare_digest(supplied, session_token):
                return error(403, "Open the local Swagger page first; its session header is required.")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/status")
    async def status():
        return {
            "endpoint": config["endpoint"],
            "deployment": config["deployment"],
            "embedding_deployment": config["embedding_deployment"],
            "tenant_id": config.get("tenant_id") or "Azure CLI 当前租户",
            "session_token": session_token,
            "operation_count": len(routes),
            "tag_count": len(ui_spec["tags"]),
            "max_upload_mb": config["max_upload_mb"],
            "schema_version": official["openapi"],
        }

    @app.get("/api/openapi.json")
    async def openapi(document: str = "azure-openai-v1", collection: str = "all"):
        spec = explorer_schemas.get((document, collection))
        if spec is None:
            return error(404, "未找到该 API 文档或测试集合；当前仅支持已适配的 Azure OpenAI v1。")
        return spec

    @app.get("/api/settings/startup")
    async def startup_settings():
        try:
            async with startup_lock:
                return await asyncio.to_thread(app.state.startup.read)
        except StartupSettingsError as exc:
            LOGGER.warning("Unable to read startup settings: %s", exc)
            return error(503, str(exc))

    @app.put("/api/settings/startup")
    async def update_startup_settings(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return error(415, "自启动设置只接受 application/json。")
        body = await request.body()
        if len(body) > 1024:
            return error(413, "自启动设置请求过大。")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return error(400, "自启动设置必须是有效的 JSON。")
        if not isinstance(payload, dict) or set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
            return error(400, "请仅提供布尔类型的 enabled 设置。")
        try:
            async with startup_lock:
                return await asyncio.to_thread(app.state.startup.write, payload["enabled"])
        except StartupSettingsError as exc:
            LOGGER.warning("Unable to update startup settings: %s", exc)
            return error(503, str(exc))

    @app.get("/api/explorer")
    async def explorer():
        return {"documents": explorer_documents}

    @app.get("/api/azure-accounts")
    async def azure_accounts():
        try:
            accounts = await app.state.account_loader()
        except AzureCliAccountError as exc:
            LOGGER.warning("Unable to list Azure CLI accounts.")
            return error(503, str(exc))
        return {"accounts": accounts}

    @app.get("/api/azure-environments")
    async def azure_environments(tenant_id: str, subscription_id: str):
        try:
            subscription_id = str(uuid.UUID(subscription_id))
        except ValueError:
            return error(400, "subscription_id 必须是有效的 UUID。")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", tenant_id):
            return error(400, "tenant_id 格式无效。")
        try:
            accounts = await app.state.account_loader()
        except AzureCliAccountError as exc:
            return error(503, str(exc))
        if not any(
            account["subscription_id"] == subscription_id and account["tenant_id"] == tenant_id
            for account in accounts
        ):
            return error(403, "所选租户和订阅不在当前 Azure CLI 登录账户中。")
        try:
            token = await app.state.arm_tokens.select(tenant_id, subscription_id)
            discovered = await app.state.environment_loader(subscription_id, token.token, app.state.client)
        except AUTH_ERRORS:
            LOGGER.warning("Azure Resource Manager authentication failed.")
            return error(401, "无法获取 Azure 管理令牌。请重新登录所选租户。")
        except ManagementDiscoveryError as exc:
            LOGGER.warning("Azure OpenAI environment discovery failed.")
            return error(502, str(exc))

        app.state.environments = {
            key: value
            for key, value in app.state.environments.items()
            if value["subscription_id"] != subscription_id
        }
        result = []
        for environment in discovered:
            environment_id = secrets.token_urlsafe(18)
            stored = {
                **environment,
                "tenant_id": tenant_id,
                "subscription_id": subscription_id,
            }
            app.state.environments[environment_id] = stored
            result.append({
                "environment_id": environment_id,
                "resource_id": stored["resource_id"],
                "resource_name": stored["resource_name"],
                "endpoint": stored["endpoint"],
                "endpoint_type": stored.get("endpoint_type", "默认"),
                "location": stored["location"],
                "deployments": stored["deployments"],
            })
        return {"environments": result}

    @app.post("/api/auth")
    async def auth(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return error(400, "认证请求必须是有效的 JSON。")
        if not isinstance(payload, dict):
            return error(400, "认证请求格式无效。")
        tenant_id = payload.get("tenant_id", "")
        subscription_id = payload.get("subscription_id") or None
        if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9.-]*", tenant_id):
            return error(400, "tenant_id 格式无效。")
        if subscription_id is not None:
            if not isinstance(subscription_id, str):
                return error(400, "subscription_id 格式无效。")
            try:
                subscription_id = str(uuid.UUID(subscription_id))
            except ValueError:
                return error(400, "subscription_id 必须是有效的 UUID。")
        try:
            token = await app.state.tokens.select(tenant_id, subscription_id)
        except AUTH_ERRORS:
            LOGGER.warning("Azure CLI authentication failed.")
            return error(401, "无法为所选租户和订阅获取 Entra ID 令牌。请运行 az login --tenant <tenant-id>，并检查 scope。")
        return {
            "authenticated": True,
            "access_token": token.token,
            "expires_at": datetime.fromtimestamp(token.expires_on, timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "subscription_id": subscription_id,
            "message": "令牌获取成功；尚未验证资源 RBAC 权限。请手动测试实际 API。",
        }

    def history_operation_exists(document_id, method, path_template):
        return (
            document_id == HISTORY_DOCUMENT_ID
            and any(
                route_method == method and route_path == path_template
                for route_method, route_path, _, _, _ in routes
            )
        )

    @app.get("/api/history/settings")
    async def history_settings():
        return await asyncio.to_thread(app.state.history.get_settings)

    @app.put("/api/history/settings")
    async def update_history_settings(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return error(400, "历史保留策略必须是有效的 JSON。")
        if not isinstance(payload, dict):
            return error(400, "历史保留策略格式无效。")
        try:
            return await asyncio.to_thread(
                app.state.history.update_settings,
                payload.get("retention_mode"),
                payload.get("retention_value"),
            )
        except ValueError as exc:
            return error(400, str(exc))

    @app.get("/api/history")
    async def list_history(
        document_id: str,
        method: str,
        path_template: str,
        limit: int = 20,
        offset: int = 0,
    ):
        method = method.upper()
        if not history_operation_exists(document_id, method, path_template):
            return error(400, "指定的 API 操作不在当前固定文档中。")
        if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
            return error(400, "limit 必须是 1..100，offset 必须是 0..1000000。")
        total, records = await asyncio.to_thread(
            app.state.history.list_records,
            document_id,
            method,
            path_template,
            limit,
            offset,
        )
        return {"total": total, "records": records, "limit": limit, "offset": offset}

    @app.delete("/api/history")
    async def clear_history(document_id: str, method: str, path_template: str):
        method = method.upper()
        if not history_operation_exists(document_id, method, path_template):
            return error(400, "指定的 API 操作不在当前固定文档中。")
        deleted = await asyncio.to_thread(
            app.state.history.clear_operation,
            document_id,
            method,
            path_template,
        )
        return {"deleted_count": deleted}

    @app.get("/api/history/{history_id}")
    async def history_detail(history_id: str):
        try:
            history_id = str(uuid.UUID(history_id))
        except ValueError:
            return error(400, "历史记录 ID 格式无效。")
        record = await asyncio.to_thread(app.state.history.get_record, history_id)
        if record is None:
            return error(404, "历史记录不存在或已被清理。")
        return record

    @app.delete("/api/history/{history_id}")
    async def delete_history(history_id: str):
        try:
            history_id = str(uuid.UUID(history_id))
        except ValueError:
            return error(400, "历史记录 ID 格式无效。")
        deleted = await asyncio.to_thread(app.state.history.delete_record, history_id)
        if not deleted:
            return error(404, "历史记录不存在或已被清理。")
        return {"deleted": True}

    @app.get("/api/history/{history_id}/request")
    async def history_request_body(history_id: str, download: bool = False):
        try:
            history_id = str(uuid.UUID(history_id))
        except ValueError:
            return error(400, "历史记录 ID 格式无效。")
        record = await asyncio.to_thread(app.state.history.get_record, history_id)
        if record is None:
            return error(404, "历史记录不存在或已被清理。")
        body = await asyncio.to_thread(app.state.history.get_request_body, history_id)
        headers = {}
        if download or not is_text_content(record["request_content_type"]):
            headers["Content-Disposition"] = f'attachment; filename="{history_id}-request.bin"'
        return Response(
            content=body,
            media_type=record["request_content_type"] or "application/octet-stream",
            headers=headers,
        )

    @app.get("/api/history/{history_id}/response")
    async def history_response_body(history_id: str, download: bool = False):
        try:
            history_id = str(uuid.UUID(history_id))
        except ValueError:
            return error(400, "历史记录 ID 格式无效。")
        record = await asyncio.to_thread(app.state.history.get_record, history_id)
        if record is None:
            return error(404, "历史记录不存在或已被清理。")
        headers = {}
        if download or not is_text_content(record["response_content_type"]):
            headers["Content-Disposition"] = f'attachment; filename="{history_id}-response.bin"'
        return StreamingResponse(
            app.state.history.iter_response_body(history_id),
            media_type=record["response_content_type"] or "application/octet-stream",
            headers=headers,
        )

    @app.get("/api/history/{history_id}/forwarded-request")
    async def history_forwarded_request_body(history_id: str, download: bool = False):
        try:
            history_id = str(uuid.UUID(history_id))
        except ValueError:
            return error(400, "历史记录 ID 格式无效。")
        record = await asyncio.to_thread(app.state.history.get_record, history_id)
        if record is None:
            return error(404, "历史记录不存在或已被清理。")
        if not record["forwarded_url"]:
            return error(404, "该请求在本地终止，未发往 Azure。")
        body = await asyncio.to_thread(app.state.history.get_forwarded_body, history_id)
        headers = {}
        if download or not is_text_content(record["forwarded_content_type"]):
            headers["Content-Disposition"] = (
                f'attachment; filename="{history_id}-forwarded-request.bin"'
            )
        return Response(
            content=body,
            media_type=record["forwarded_content_type"] or "application/octet-stream",
            headers=headers,
        )

    @app.get("/api/history/{history_id}/restore")
    async def history_restore_snapshot(history_id: str):
        try:
            history_id = str(uuid.UUID(history_id))
        except ValueError:
            return error(400, "历史记录 ID 格式无效。")
        record = await asyncio.to_thread(app.state.history.get_record, history_id)
        if record is None:
            return error(404, "历史记录不存在或已被清理。")
        body = await asyncio.to_thread(app.state.history.get_request_body, history_id)
        template_parts = record["path_template"].strip("/").split("/")
        actual_parts = record["actual_path"].strip("/").split("/")
        path_parameters = {}
        if len(template_parts) == len(actual_parts):
            for template_part, actual_part in zip(template_parts, actual_parts):
                if template_part.startswith("{") and template_part.endswith("}"):
                    path_parameters[template_part[1:-1]] = unquote(actual_part)
        query_parameters = {}
        for name, value in parse_qsl(record["query_string"], keep_blank_values=True):
            query_parameters.setdefault(name, []).append(value)

        content_type = record["request_content_type"]
        body_text = None
        form_fields = {}
        files = []
        if "application/json" in content_type.lower():
            body_text = body.decode("utf-8", errors="replace")
        elif "application/x-www-form-urlencoded" in content_type.lower():
            for name, value in parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True):
                form_fields.setdefault(name, []).append(value)
        elif "multipart/form-data" in content_type.lower() and body:
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
            )
            if message.is_multipart():
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    filename = part.get_filename()
                    if not name:
                        continue
                    payload = part.get_payload(decode=True) or b""
                    if filename is not None:
                        files.append({
                            "field": name,
                            "filename": filename,
                            "content_type": part.get_content_type(),
                            "size": len(payload),
                        })
                    else:
                        charset = part.get_content_charset() or "utf-8"
                        form_fields.setdefault(name, []).append(
                            payload.decode(charset, errors="replace")
                        )
        return {
            "history_id": history_id,
            "method": record["method"],
            "path_template": record["path_template"],
            "path_parameters": path_parameters,
            "query_parameters": query_parameters,
            "headers": record["request_headers"],
            "content_type": content_type,
            "body_text": body_text,
            "form_fields": form_fields,
            "files": files,
        }

    @app.api_route("/proxy/openai/v1/{path:path}", methods=["GET", "POST", "DELETE", "PATCH", "PUT"])
    async def proxy(path, request: Request):
        api_path = "/" + path
        if (
            any(char in api_path for char in ("%", "\\", "?", "#"))
            or any(ord(char) < 32 for char in api_path)
            or any(part in {".", "..", ""} for part in path.split("/"))
        ):
            return error(400, "Invalid API path.")
        route_info = None
        for method, path_template, operation_id, pattern, headers in routes:
            if method == request.method and pattern.fullmatch(api_path):
                route_info = (path_template, operation_id, headers)
                break
        if route_info is None:
            return error(404, "This method/path is not in the pinned official v1 schema.")
        path_template, operation_id, allowed_headers = route_info
        environment_id = request.headers.get("x-lab-environment")
        environment = app.state.environments.get(environment_id) if environment_id else None
        deployment_name = request.headers.get("x-lab-deployment")
        body = bytearray()
        history_id = None
        history_started = time.perf_counter()
        history_body = b""

        async def ensure_history():
            nonlocal history_id
            if history_id is not None:
                return history_id
            safe_header_names = allowed_headers | {
                "content-type", "accept", "openai-beta", "aoai-evals",
            }
            safe_headers = {
                key: value
                for key, value in request.headers.items()
                if key in safe_header_names
                and key not in {"authorization", "api-key", "cookie"}
                and not key.startswith("x-lab-")
            }
            safe_headers["x-ms-client-request-id"] = request_id
            try:
                history_id, _ = await asyncio.to_thread(
                    app.state.history.create_record,
                    document_id=HISTORY_DOCUMENT_ID,
                    operation_id=operation_id,
                    method=request.method,
                    path_template=path_template,
                    actual_path=api_path,
                    query_string=request.url.query,
                    request_headers=safe_headers,
                    request_content_type=request.headers.get("content-type", ""),
                    request_body=history_body,
                    resource_name=environment.get("resource_name") if environment else None,
                    endpoint_type=environment.get("endpoint_type") if environment else None,
                    deployment_name=deployment_name,
                )
            except Exception:
                LOGGER.exception("Unable to create request history record.")
            return history_id

        async def recorded_error(status_code, message, request_id=None):
            response = error(status_code, message, request_id)
            current_history_id = await ensure_history()
            if current_history_id:
                try:
                    await asyncio.to_thread(
                        app.state.history.record_response,
                        current_history_id,
                        status=status_code,
                        response_headers=dict(response.headers),
                        response_content_type=response.media_type or "application/json",
                        body=response.body,
                        duration_ms=(time.perf_counter() - history_started) * 1000,
                    )
                except Exception:
                    LOGGER.exception("Unable to finalize request history record %s.", current_history_id)
            return response

        request_id = request.headers.get("x-ms-client-request-id", "").strip() or str(uuid.uuid4())
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > config["max_upload_mb"] * 1024 * 1024:
                history_body = bytes(body)
                return await recorded_error(
                    413,
                    f"Request exceeds the local {config['max_upload_mb']} MiB limit.",
                    request_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) else None,
                )
        history_body = bytes(body)
        await ensure_history()
        if needs_unlock(request.method, api_path) and request.headers.get("x-lab-allow-mutations") != "true":
            return await recorded_error(
                403,
                "此操作可能创建/修改/删除资源或启动作业。请先在页面顶部明确解锁。",
            )
        if environment_id and environment is None:
            return await recorded_error(400, "所选 Azure 环境已失效，请刷新资源列表。")
        target_endpoint = environment["endpoint"] if environment else config["endpoint"]
        if not target_endpoint:
            return await recorded_error(400, "请先在页面选择 Azure 订阅与 Endpoint，等待资源发现完成后再执行请求。")
        if deployment_name:
            if environment is None:
                return await recorded_error(400, "选择模型部署时必须同时选择 Azure Endpoint。")
            valid_deployments = {deployment["name"] for deployment in environment["deployments"]}
            if deployment_name not in valid_deployments:
                return await recorded_error(400, "所选模型部署不属于当前 Azure Endpoint。")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
            return await recorded_error(
                400,
                "x-ms-client-request-id must be 1..128 ASCII letters, digits or . _ : -.",
            )
        if deployment_name and api_path in MODEL_OVERRIDE_PATHS:
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return await recorded_error(400, "当前部署切换要求请求 Body 为有效 JSON。", request_id)
            if not isinstance(payload, dict):
                return await recorded_error(400, "当前部署切换要求请求 Body 为 JSON 对象。", request_id)
            payload["model"] = deployment_name
            body = bytearray(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        try:
            token = (
                await app.state.tokens.select(environment["tenant_id"], environment["subscription_id"])
                if environment
                else await app.state.tokens.get()
            )
        except AUTH_ERRORS:
            LOGGER.warning("Azure CLI authentication failed for %s %s", request.method, api_path)
            return await recorded_error(
                401,
                "Entra ID 认证失败。请运行 az login，并确认页面所选租户和订阅；使用可选配置时请检查 tenant_id。",
                request_id,
            )

        allowed_headers |= {"content-type", "accept", "openai-beta", "aoai-evals"}
        blocked_headers = {"authorization", "api-key", "host", "cookie", "content-length"}
        outgoing_headers = {
            key: value
            for key, value in request.headers.items()
            if key in allowed_headers and key not in blocked_headers and not key.startswith("x-lab-")
        }
        outgoing_headers.update({
            "Authorization": f"Bearer {token.token}",
            "x-ms-client-request-id": request_id,
            "Accept-Encoding": "identity",
        })
        url = target_endpoint + "/openai/v1" + quote(api_path, safe="/-._~:")
        if request.url.query:
            url += "?" + request.url.query
        client = app.state.client
        upstream_request = client.build_request(
            request.method, url, headers=outgoing_headers, content=bytes(body)
        )
        if history_id:
            try:
                await asyncio.to_thread(
                    app.state.history.record_forwarded_request,
                    history_id,
                    url=str(upstream_request.url),
                    headers=redact_log_headers(dict(upstream_request.headers)),
                    content_type=upstream_request.headers.get("content-type", ""),
                    body=bytes(body),
                )
            except Exception:
                LOGGER.exception("Unable to record forwarded request %s.", history_id)
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            LOGGER.warning("Azure request timed out: %s", request_id)
            return await recorded_error(
                504,
                "Azure 请求超时。不会自动重试，以免重复计费或创建资源。",
                request_id,
            )
        except httpx.RequestError:
            LOGGER.warning("Azure network request failed: %s", request_id)
            return await recorded_error(
                502,
                "无法连接 Azure。请检查 DNS、网络、防火墙或代理设置。",
                request_id,
            )

        LOGGER.info("%s %s -> %s (client-request-id=%s)", request.method, api_path, upstream.status_code, request_id)
        if upstream.status_code == 401:
            app.state.tokens.invalidate()
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            return await recorded_error(
                502,
                "Azure 返回了重定向；本地代理拒绝跟随或转发重定向。",
                request_id,
            )
        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key in {"content-type", "content-disposition", "retry-after", "request-id", "location", "openai-processing-ms"}
            or key.startswith(("x-ms-", "x-request-", "apim-", "x-ratelimit-"))
        }
        response_headers["x-ms-client-request-id"] = request_id

        async def stream():
            sequence = 0
            pending = bytearray()
            stream_completed = False
            storage_error = None
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    if history_id and storage_error is None:
                        pending.extend(chunk)
                        if len(pending) >= HISTORY_WRITE_BUFFER_BYTES:
                            try:
                                await asyncio.to_thread(
                                    app.state.history.append_response_chunk,
                                    history_id,
                                    sequence,
                                    bytes(pending),
                                )
                                sequence += 1
                                pending.clear()
                            except Exception as exc:
                                storage_error = str(exc)
                                LOGGER.exception(
                                    "Unable to append response history for %s.", history_id
                                )
                stream_completed = True
            except httpx.RequestError:
                LOGGER.error("Azure response stream interrupted: %s", request_id)
                raise
            finally:
                await upstream.aclose()
                if history_id:
                    try:
                        if pending and storage_error is None:
                            await asyncio.to_thread(
                                app.state.history.append_response_chunk,
                                history_id,
                                sequence,
                                bytes(pending),
                            )
                        await asyncio.to_thread(
                            app.state.history.complete_record,
                            history_id,
                            status=upstream.status_code,
                            response_headers=response_headers,
                            response_content_type=upstream.headers.get("content-type", ""),
                            duration_ms=(time.perf_counter() - history_started) * 1000,
                            completed=stream_completed and storage_error is None,
                            storage_error=storage_error,
                        )
                    except Exception:
                        LOGGER.exception("Unable to finalize request history record %s.", history_id)

        return StreamingResponse(stream(), status_code=upstream.status_code, headers=response_headers)

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.mount("/vendor", StaticFiles(directory=vendor), name="vendor")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-port", action="store_true", help="Print the validated listening port without starting.")
    args = parser.parse_args()
    settings = load_config()
    if args.print_port:
        print(settings["port"])
        sys.exit(0)
    logging.basicConfig(level=logging.INFO)
    print(f"Swagger UI: http://127.0.0.1:{settings['port']}")
    print("No Azure request is sent until you explicitly test authentication or execute an operation.")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings["port"], access_log=False)
