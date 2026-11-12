![Azure OpenAI REST API Lab](docs/images/banner.svg)

# Azure OpenAI 本地 REST API 实验室

本地 Swagger UI，自动发现 Azure 订阅、Endpoint 和部署，使用 Azure CLI / Entra ID 认证，无需 API Key。官方快照包含 **14 类 API、106 个操作**。

[使用](#使用) · [启动与更新](#启动与更新) · [卸载](#quick-uninstall) · [常见问题](#常见问题) · [Swagger JSON 来源](#api-swagger-json-来源)

## Quick Start

在 **PowerShell 5.1 / 7+（Windows）** 中运行：

```powershell
$setup = Join-Path $env:TEMP ("aoai-setup-" + [guid]::NewGuid().ToString("N") + ".ps1")
try {
    Invoke-WebRequest -UseBasicParsing -Uri ("https://raw.githubusercontent.com/chunxli/AOAI-API-Swagger/main/setup.ps1?nocache=" + [guid]::NewGuid().ToString("N")) -Headers @{ "Cache-Control" = "no-cache" } -OutFile $setup -ErrorAction Stop
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $setup
    if ($LASTEXITCODE -ne 0) { throw "Setup failed. See the error above." }
} finally {
    if (Test-Path -LiteralPath $setup) { Remove-Item -LiteralPath $setup -Force }
}
```

缺失工具通过 `winget` 安装；脚本自动下载/更新代码、安装依赖、登录 Azure、后台启动并打开浏览器。按提示完成 UAC 或 MFA。

- 默认安装目录：`%LOCALAPPDATA%\AzureOpenAILab`；默认地址：**http://127.0.0.1:8765**。
- 无需创建 `config.json`；需已有 Azure 部署及相应权限。
- 登录自启动默认开启，可在页面关闭。

## 使用

1. 选择订阅、Endpoint 和模型部署，点击“获取访问令牌”。
2. 选择“推理入门”：先用 **GET /models** 检查权限，再选择 **Responses** 或 **Chat Completions**，通过 **Try it out → Execute** 发起请求。
3. 查看响应和请求历史，或复制 Bash、PowerShell、Python 等价命令。

> **Execute 调用真实 Azure API，可能计费。** 上传、修改、删除和作业操作需先解锁。

Responses、Chat Completions、Completions 和 Embeddings 的 `model` 自动同步为当前部署。Embedding 需兼容部署；Batch JSONL、Fine-tuning 等操作需自行填写模型。

提供全部接口、推理入门、Batch、向量检索四种集合，不自动执行或传递 ID。切换集合保留连接，清除请求编辑和响应。资源卡片可跳转 Azure Portal / Microsoft Foundry。

<details>
<summary>应用截图（虚构演示数据）</summary>

![连接与安全控制](docs/images/app-workspace.png)

![API Explorer](docs/images/app-api-explorer.png)

</details>

## 启动与更新

### 手动启动

关闭自启动后，默认安装可运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $env:LOCALAPPDATA "AzureOpenAILab\start.ps1")
```

按启动输出的地址访问。自定义目录可直接从页面复制手动启动命令。

服务在后台运行，日志保存在 `.runtime`。以下命令在安装目录执行：

| 命令 | 用途 |
|---|---|
| `.\restart.ps1` | 重启 |
| `.\stop.ps1` | 停止本站服务 |
| `.\service.ps1 status` | 查看 PID 和访问地址 |
| `.\start.ps1 -Foreground` | 前台调试，Ctrl+C 停止；不初始化自启动 |

### 登录 Windows 后自动启动

首次安装或首次升级到此策略时，后台启动会**自动开启一次**登录自启动；之后保留开关选择。仅在当前 Windows 用户登录后运行，不自动打开浏览器。

关闭开关不停止当前服务。选择记录在 `.runtime/startup-default.json`，删除会重置默认值；移动或删除应用前先关闭开关。

### 更新与安装选项

**重跑 Quick Start 即可更新并重启**。仅支持干净 `main` 分支上的快进更新，不覆盖本地改动。安装中止时不重启，也不回滚已更新代码。

从仓库运行 [setup.ps1](setup.ps1) 使用该仓库；下载版使用默认安装目录。可追加参数：

| 参数 | 用途 |
|---|---|
| `-InstallDirectory "D:\AzureOpenAILab"` | 指定安装目录，更新/卸载时保持一致 |
| `-CheckOnly` | 只检查工具 |
| `-NpmRegistryPrefix` | 手动包源映射，见下方 npm 排查 |

<details>
<summary>手动安装（可选）</summary>

需 Git、Python 3.10+、Node.js/npm 和 Azure CLI：

```powershell
git clone https://github.com/chunxli/AOAI-API-Swagger.git
Set-Location .\AOAI-API-Swagger
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
npm.cmd install --ignore-scripts --no-audit --no-fund
az login
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

仓库不含 npm 锁文件，首次用 `npm install`，不用 `npm ci`。Azure CLI 无需额外扩展或精确版本。

</details>

## Quick Uninstall

**默认停服务、关自启动，保留文件和数据**。不影响 Azure 资源或共享工具。

```powershell
$uninstall = Join-Path $env:TEMP ("aoai-uninstall-" + [guid]::NewGuid().ToString("N") + ".ps1")
try {
    Invoke-WebRequest -UseBasicParsing -Uri ("https://raw.githubusercontent.com/chunxli/AOAI-API-Swagger/main/uninstall.ps1?nocache=" + [guid]::NewGuid().ToString("N")) -Headers @{ "Cache-Control" = "no-cache" } -OutFile $uninstall -ErrorAction Stop
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uninstall
    if ($LASTEXITCODE -ne 0) { throw "Uninstall failed. See the error above." }
} finally {
    if (Test-Path -LiteralPath $uninstall) { Remove-Item -LiteralPath $uninstall -Force }
}
```

[uninstall.ps1](uninstall.ps1) 默认处理 Quick Start 安装目录。在 `-File $uninstall` 后追加：

- `-InstallDirectory "实际目录"`：指定安装。
- `-WhatIf`：只预览。
- **`-RemoveFiles`：确认后永久删除安装和数据。** 仅支持默认目录，须在目录外使用下载版脚本；含符号链接/目录联接时拒删，自定义目录和开发仓库不自动删除。

使用原安装用户执行。保留文件时可重跑 Quick Start 恢复运行；Azure CLI 登录和浏览器存储不清理。

## 可选配置与数据安全

修改端口：在安装目录创建 UTF-8 的 `config.json`，重启生效。

```json
{"port": 9000}
```

默认超时 **180 秒**、上传上限 **32 MiB**，scope 为 `https://ai.azure.com/.default`，也支持 `https://cognitiveservices.azure.com/.default`。更多选项见 [server.py](server.py) 的 `DEFAULT_CONFIG`；页面选择优先。

- **仅限本机使用**，不要通过监听地址或反向代理暴露服务。
- 读取当前 Azure CLI 账户，不更改默认订阅。需资源读取及推理权限，如 `Cognitive Services OpenAI User`；页面解锁不替代 RBAC 授权。
- 等价命令**含临时 Token，勿分享**。Swagger 自带 curl 指向本机代理，缺少会话码，不能直接执行。
- 历史默认永久保留请求、响应和上传内容，认证头脱敏，正文仍可能含机密。可按条数/天数清理；恢复不自动执行，文件需重新选择。
- `.runtime`、`config.json`、`package-lock.json` 不入 Git；分享日志和截图前仍需脱敏。Realtime 响应可能含短期 secret。
- **Azure 请求不自动重试**。SSE 通常等流结束后显示，初次建议 `stream: false`。仅支持 Azure 公有云，具体能力取决于模型、区域和权限；Realtime 仅 REST 控制，Threads 仅作旧接口参考。

## 常见问题

| 现象 | 处理 |
|---|---|
| 账户为空或登录过期 | 同一 Windows 用户下执行 `az login` 或 `az login --tenant '<tenant-id>'`，再“刷新账户” |
| 切换登录账号 | `az logout` 后 `az login`；会影响使用该登录的其他 CLI 工具 |
| Azure 401/403 | 检查租户、scope、RBAC、网络及角色生效时间 |
| 本地 403 | 检查页面端口、会话及写操作解锁 |
| HTTP 500 / 非 JSON 响应 | 查看后端日志 |
| npm `EALLOWREMOTE` | 自动核验并映射重试一次；失败见下方排查 |
| npm TLS 握手失败 | 检查受批准包源、代理和 CA，勿关闭 SSL 校验 |
| 端口占用或自启动未运行 | 查 `service.ps1 status`、Windows 启动设置及日志；勿终止未知进程 |
| Azure 400/404/429 或上传 413 | 按错误检查字段、部署兼容性、配额和大小限制 |

默认安装的后端日志：

```powershell
Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA "AzureOpenAILab\.runtime\server.stderr.log") -Tail 80
```

### npm proxy compatibility：包源兼容处理

<details>
<summary>自动修复失败时展开：诊断与手动映射</summary>

npm 12 可能误判代理返回的压缩包地址。自动修复保留已有自定义映射和独立 scoped registry，这些情况需手动配置。

在安装目录检查（不要分享含凭据的地址或完整 `.npmrc`）：

```powershell
npm.cmd --version
npm.cmd config get registry
npm.cmd config get allow-remote
npm.cmd view "@scarf/scarf@1.4.0" dist.tarball
```

当前受批准的 registry 支持相同压缩包时，可指定**原始地址前缀**：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -NpmRegistryPrefix "https://packages.example.org/feed/npm/registry"
```

将示例替换为实际 HTTPS 前缀，不得含凭据、查询参数或 fragment。Quick Start 在 `-File $setup` 后追加此参数，仅本次有效，不修改下载权限、TLS 或全局配置。仍失败时联系包源维护者。

参考：[npm 配置](https://docs.npmjs.com/cli/v12/using-npm/config#allow-remote)、[上游问题](https://github.com/npm/cli/issues/9796)。

</details>

## API Swagger JSON 来源

定义来自微软官方 [Azure/azure-rest-api-specs](https://github.com/Azure/azure-rest-api-specs)。

- 固定提交：[`a6943a926f76`](https://github.com/Azure/azure-rest-api-specs/commit/a6943a926f76b3a2f90371b3466157eddf760e25)，快照获取日期 **2026-09-23**。
- Azure OpenAI **v1** / OpenAPI **3.2.0**：[官方 JSON 下载](https://raw.githubusercontent.com/Azure/azure-rest-api-specs/a6943a926f76b3a2f90371b3466157eddf760e25/specification/ai/data-plane/OpenAI.v1/azure-v1-v1-generated.json) · [本地原始副本](vendor/azure-openai-v1.json) · [来源与 SHA-256](vendor/source.json)。

原始 JSON 保持不变；[specification.py](specification.py) 在内存中适配代理、认证和示例，通过 `/api/openapi.json` 提供给页面。定义为固定快照，不自动追随上游更新。

参考：[官方 API 生命周期](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle)、[Entra ID 认证](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/managed-identity)。MIT 授权见 [Azure LICENSE](vendor/LICENSE-Azure.txt)、[OpenAI LICENSE](vendor/LICENSE-OpenAI.txt)；Swagger UI 授权随 npm 包保留。

## 开发测试

在仓库目录运行离线测试：

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_server test_startup_settings
node --check .\static\site.js
npm.cmd test
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test_setup.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test_uninstall.ps1
```

验证环境：Windows、Python 3.14、Azure CLI 2.91.0、npm 12.2.1。依赖版本见 [requirements-lock.txt](requirements-lock.txt) 和 [package.json](package.json)。
