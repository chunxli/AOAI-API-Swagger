"""Adapt the pinned official schema for the local, authenticated proxy."""

import copy
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
TAG_DESCRIPTIONS = {
    "Responses": "统一推理：文本、多模态、工具与后台任务。新应用优先选择。所有模型调用可能计费。",
    "Chat": "Chat Completions：messages 输入，choices 输出。沿用现有聊天应用。",
    "Completions": "传统 prompt 补全。仅适用于兼容的旧模型，不能直接用于所有聊天部署。",
    "Embeddings": "文本转向量。需要单独的 Embedding 模型部署，不能使用聊天部署名。",
    "Batch": "离线批量作业。先上传 JSONL，使用支持 Batch 的部署，完成后通过 Files 下载。",
    "Containers": "代码执行沙箱及内部文件。不是 Azure Container Apps；创建后可能产生资源费用。",
    "Conversations": "Responses 的持久上下文对象。创建 Conversation 不会直接执行模型。",
    "Evals": "评测定义与评测运行。运行可能调用评分模型并产生费用。",
    "Files": "上传、下载与删除文件。上传使用 multipart/form-data；本站默认上限 32 MiB。",
    "Fine-tuning": "训练作业、检查点与权限。模型、方法和区域各有要求；训练可能产生较大费用。",
    "Models": "数据平面模型信息，不等同于 Azure 资源的 deployment 管理。",
    "Realtime": "仅测试 REST 会话/通话控制。Swagger 不提供 WebRTC/WebSocket/SIP 音频客户端。响应可能包含短期 client secret，请勿分享。",
    "Threads": "旧 Assistants 的 Thread/Run 接口。保留官方定义便于查看，是否可用以 Azure 生命周期公告为准；新项目请勿采用。",
    "Vector Stores": "文档语义索引与检索。入库异步，上传后应检查处理状态；存储可能计费。",
}
API_DOCUMENTATION = {
    "Responses": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/responses",
        "openai": "https://developers.openai.com/api/reference/responses/overview",
    },
    "Chat": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat",
        "openai": "https://developers.openai.com/api/reference/chat-completions/overview",
    },
    "Completions": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/completions",
        "openai": "https://developers.openai.com/api/reference/resources/completions",
    },
    "Embeddings": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/embeddings",
        "openai": "https://developers.openai.com/api/reference/resources/embeddings",
    },
    "Batch": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/batch",
        "openai": "https://developers.openai.com/api/reference/resources/batches",
    },
    "Containers": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/containers",
        "openai": "https://developers.openai.com/api/reference/resources/containers",
    },
    "Conversations": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/conversations",
        "openai": "https://developers.openai.com/api/reference/resources/conversations",
    },
    "Evals": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/evals",
        "openai": "https://developers.openai.com/api/reference/resources/evals",
    },
    "Files": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/files",
        "openai": "https://developers.openai.com/api/reference/resources/files",
    },
    "Fine-tuning": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/fine-tuning",
        "openai": "https://developers.openai.com/api/reference/resources/fine_tuning",
    },
    "Models": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/models",
        "openai": "https://developers.openai.com/api/reference/resources/models",
    },
    "Realtime": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/realtime",
        "openai": "https://developers.openai.com/api/reference/resources/realtime",
    },
    "Threads": {
        "foundry": "https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/threads",
    },
    "Vector Stores": {
        "openai": "https://developers.openai.com/api/reference/resources/vector_stores",
    },
}

DOCUMENTS = {
    "azure-openai-v1": {
        "title": "Azure OpenAI v1",
        "api_version": "v1",
        "schema_file": "azure-openai-v1.json",
        "source_file": "source.json",
        "adapter": "azure-openai-v1",
    },
}
COLLECTIONS = {
    "all": {
        "title": "全部接口",
        "description": "当前文档的完整接口。按分类浏览，所有请求均需手动执行。",
        "steps": [],
    },
    "inference": {
        "title": "推理入门",
        "description": "先检查权限，再选择 Responses 或 Chat 发起推理；两种推理接口无需依次执行。",
        "steps": [
            ("get", "/models", "检查资源访问权限"),
            ("post", "/responses", "使用 Responses 推理（可能计费）"),
            ("post", "/chat/completions", "或使用 Chat Completions 推理"),
        ],
    },
    "batch": {
        "title": "Batch 测试",
        "description": "手动传递文件和作业 ID；上传和创建作业需解锁。JSONL 使用 /v1/responses，"
                       "其中的 model 必须填写兼容 Batch 的部署名，页面不会改写文件内容。",
        "steps": [
            ("post", "/files", "上传 JSONL，purpose 选择 batch，记录返回的文件 ID"),
            ("post", "/batches", "填写 input_file_id，创建作业并记录 Batch ID"),
            ("get", "/batches/{batch_id}", "手动查询状态，完成后记录 output_file_id / error_file_id"),
            ("get", "/files/{file_id}/content", "使用结果或错误文件 ID 下载内容"),
        ],
        "examples": {
            "/batches": {
                "input_file_id": "file-REPLACE-ME",
                "endpoint": "/v1/responses",
                "completion_window": "24h",
            },
        },
    },
    "vector": {
        "title": "向量检索",
        "description": "Embeddings 是独立测试分支，需兼容的 embedding 部署。托管文件检索使用 "
                       "Files → Vector Store → 等待入库 → Search，不需要手动传入 embedding 向量。",
        "steps": [
            ("post", "/embeddings", "可选：独立测试文本向量化（可能计费）"),
            ("post", "/files", "上传检索文件，purpose 选择 assistants，记录文件 ID"),
            ("post", "/vector_stores", "创建向量库，记录 vector_store_id"),
            ("post", "/vector_stores/{vector_store_id}/files", "将上传的 file_id 关联到向量库"),
            ("get", "/vector_stores/{vector_store_id}/files/{file_id}", "手动检查文件入库状态，完成后再搜索"),
            ("post", "/vector_stores/{vector_store_id}/search", "填写 query 执行搜索"),
        ],
        "examples": {
            "/vector_stores/{vector_store_id}/files": {"file_id": "file-REPLACE-ME"},
        },
    },
}


def load_official_spec(document_id="azure-openai-v1"):
    return json.loads((ROOT / "vendor" / DOCUMENTS[document_id]["schema_file"]).read_text("utf-8"))


def build_explorer(config):
    catalog = []
    schemas = {}
    for document_id, document in DOCUMENTS.items():
        if document["adapter"] != "azure-openai-v1":
            raise ValueError(f"Unsupported API adapter: {document['adapter']}")
        official = load_official_spec(document_id)
        base = build_ui_spec(official, config)
        source = json.loads((ROOT / "vendor" / document["source_file"]).read_text("utf-8"))
        collections = []
        for collection_id, collection in COLLECTIONS.items():
            spec = copy.deepcopy(base)
            steps = []
            if collection["steps"]:
                paths = {}
                for method, path, instruction in collection["steps"]:
                    operation = spec["paths"][path][method]
                    item = paths.setdefault(path, {
                        key: value for key, value in spec["paths"][path].items() if key not in METHODS
                    })
                    item[method] = operation
                    steps.append({
                        "method": method.upper(), "path": path, "instruction": instruction,
                        "tag": operation["tags"][0], "operation_id": operation["operationId"],
                    })
                spec["paths"] = paths
                tags = {tag for _, _, operation in operations(spec) for tag in operation["tags"]}
                spec["tags"] = [tag for tag in spec["tags"] if tag["name"] in tags]
                spec["info"]["title"] = f"{document['title']} · {collection['title']}"
                spec["info"]["description"] = collection["description"] + "\n\n" + base["info"]["description"]
            for path, example in collection.get("examples", {}).items():
                spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["example"] = copy.deepcopy(example)
            spec["x-lab-collection"] = {"document_id": document_id, "id": collection_id}
            schemas[(document_id, collection_id)] = spec
            collections.append({
                "id": collection_id,
                "title": collection["title"],
                "description": collection["description"],
                "operation_count": len(list(operations(spec))),
                "tags": [tag["name"] for tag in spec["tags"]],
                "steps": steps,
                "schema_url": f"/api/openapi.json?document={document_id}&collection={collection_id}",
            })
        catalog.append({
            "id": document_id, "title": document["title"], "api_version": document["api_version"],
            "schema_version": official["openapi"],
            "source_url": source["url"], "source_commit": source["commit"],
            "retrieved": source["retrieved"], "collections": collections,
        })
    return catalog, schemas


def operations(spec):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield path, method, operation


def needs_unlock(method, path):
    if method.lower() in {"get", "head"}:
        return False
    inference = {"/responses", "/chat/completions", "/completions", "/embeddings"}
    if method.lower() == "post" and path in inference:
        return False
    if method.lower() == "post" and re.fullmatch(r"/vector_stores/[^/]+/search", path):
        return False
    return True


def build_ui_spec(official, config):
    spec = copy.deepcopy(official)
    spec["info"]["title"] = "Azure OpenAI · 本地 REST API 实验室"
    spec["info"]["description"] = (
        "基于 Azure 官方 v1 定义。请求经本地代理发送到配置的 Azure 资源，"
        "代理使用页面所选 Azure CLI 租户和订阅的 Entra ID 会话；令牌仅在验证后暂存于当前页面内存，且不显示明文。"
        "推理也会计费；资源变更和作业操作须在页面顶部解锁。"
        "按当前集合指南手动执行；切换集合不会自动发送请求。"
    )
    spec["servers"] = [{"url": "/proxy/openai/v1", "description": "本机 Entra ID 代理"}]
    spec["security"] = []
    spec.get("components", {}).pop("securitySchemes", None)
    spec["tags"] = [{"name": name, "description": text} for name, text in TAG_DESCRIPTIONS.items()]
    spec["x-local-proxy"] = True

    examples = {
        "/responses": {
            "model": config["deployment"],
            "input": "请用一句话介绍 Azure OpenAI。",
            "max_output_tokens": 1024,
            "store": False,
            "stream": False,
        },
        "/chat/completions": {
            "model": config["deployment"],
            "messages": [{"role": "user", "content": "请用一句话介绍 Azure OpenAI。"}],
            "max_completion_tokens": 1024,
            "stream": False,
        },
        "/completions": {
            "model": "YOUR-COMPLETIONS-DEPLOYMENT",
            "prompt": "Once upon a time",
            "max_tokens": 64,
        },
        "/embeddings": {
            "model": config["embedding_deployment"],
            "input": ["Azure OpenAI provides model APIs."],
        },
        "/conversations": {"metadata": {"purpose": "local-swagger-test"}},
        "/vector_stores": {"name": "local-swagger-test"},
        "/vector_stores/{vector_store_id}/search": {"query": "Azure OpenAI"},
        "/batches": {
            "input_file_id": "file-REPLACE-ME",
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    }
    for path, method, operation in operations(spec):
        operation["security"] = []
        operation.pop("servers", None)
        # Azure SDK example-file links are not standalone OpenAPI request examples.
        operation.pop("x-ms-examples", None)
        description_sections = []
        if needs_unlock(method, path):
            description_sections.append("**本地保护：此操作需勾选顶部「允许资源变更/作业操作」。**")
        documentation = API_DOCUMENTATION[operation["tags"][0]]
        documentation_links = []
        if documentation.get("foundry"):
            documentation_links.append(f"[Microsoft Foundry 文档]({documentation['foundry']})")
        if documentation.get("openai"):
            documentation_links.append(f"[OpenAI 文档]({documentation['openai']})")
        description_sections.append("**官方文档：** " + " · ".join(documentation_links))
        if operation.get("description"):
            description_sections.append(operation["description"])
        operation["description"] = "\n\n".join(description_sections)
        if method == "post" and path in examples:
            content = operation.get("requestBody", {}).get("content", {})
            if "application/json" in content:
                content["application/json"]["example"] = examples[path]
        content = operation.get("requestBody", {}).get("content", {})
        if "multipart/form-data" in content:
            media = content["multipart/form-data"]
            schema = media["schema"]
            if "$ref" in schema:
                schema = copy.deepcopy(spec["components"]["schemas"][schema["$ref"].split("/")[-1]])
            properties = schema.get("properties", {})
            if "file" in properties:
                # The official generator omits the type needed for Swagger's file picker.
                properties["file"].update({"type": "string", "format": "binary"})
            if "expires_after" in properties:
                expiry = properties.pop("expires_after")
                for name, field in expiry["properties"].items():
                    properties[f"expires_after[{name}]"] = field
                properties["expires_after[seconds]"]["example"] = 86400
                properties["expires_after[anchor]"]["example"] = "created_at"
                if "expires_after" in schema.get("required", []):
                    schema["required"].remove("expires_after")
                    schema["required"].extend(f"expires_after[{name}]" for name in expiry["required"])
            media["schema"] = schema
        parameters = operation.setdefault("parameters", [])
        if not any(p.get("name", "").lower() == "x-ms-client-request-id" for p in parameters):
            parameters.append({
                "name": "x-ms-client-request-id",
                "in": "header",
                "required": False,
                "description": "可自定义。留空则本地代理生成 UUID；返回 Header 会带回此值。",
                "schema": {"type": "string", "maxLength": 128},
            })
    for item in spec["paths"].values():
        item.pop("servers", None)
    return spec
