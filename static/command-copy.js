(function exposeCommandCopy(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AzureOpenAICommandCopy = api;
}(typeof globalThis === "object" ? globalThis : this, function commandCopyFactory() {
  "use strict";

  const MODEL_OVERRIDE_PATHS = new Set([
    "/responses",
    "/chat/completions",
    "/completions",
    "/embeddings",
  ]);
  const BLOCKED_HEADERS = new Set([
    "authorization",
    "api-key",
    "cookie",
    "host",
    "content-length",
    "accept-encoding",
  ]);

  const bashQuote = (value) => `'${String(value).replace(/'/g, `'"'"'`)}'`;
  const powerShellQuote = (value) => `'${String(value).replace(/'/g, "''")}'`;
  const pythonQuote = (value) => JSON.stringify(String(value));

  const normalizeHeaders = (headers) => {
    const normalized = [];
    if (!headers) return normalized;
    if (typeof headers.forEach === "function") {
      headers.forEach((value, name) => normalized.push([String(name), String(value)]));
    } else {
      Object.entries(headers).forEach(([name, value]) => {
        if (value !== undefined && value !== null) normalized.push([name, String(value)]);
      });
    }
    return normalized.filter(([name]) => {
      const lowerName = name.toLowerCase();
      return !BLOCKED_HEADERS.has(lowerName) && !lowerName.startsWith("x-lab-");
    });
  };

  const normalizeRequestBody = (body, apiPath, deploymentName) => {
    if (body === undefined || body === null || body === "") {
      return { kind: "none", text: "", fields: [], files: [] };
    }
    if (typeof FormData !== "undefined" && body instanceof FormData) {
      const fields = [];
      const files = [];
      for (const [name, value] of body.entries()) {
        if (typeof value === "string") {
          fields.push({ name, value });
        } else {
          files.push({
            name,
            filename: value.name || "upload.bin",
            contentType: value.type || "application/octet-stream",
          });
        }
      }
      return { kind: "multipart", text: "", fields, files };
    }
    let text = typeof body === "string" ? body : JSON.stringify(body);
    if (deploymentName && MODEL_OVERRIDE_PATHS.has(apiPath)) {
      const payload = JSON.parse(text);
      if (!payload || Array.isArray(payload) || typeof payload !== "object") {
        throw new Error("部署替换要求请求 Body 为 JSON 对象。");
      }
      payload.model = deploymentName;
      text = JSON.stringify(payload, null, 2);
    }
    return { kind: "text", text, fields: [], files: [] };
  };

  const buildBash = ({ method, targetUrl, headers, body, accessToken }) => {
    const lines = [
      `curl --request ${bashQuote(method)}`,
      `  --url ${bashQuote(targetUrl)}`,
      `  --header ${bashQuote(`Authorization: Bearer ${accessToken}`)}`,
    ];
    for (const [name, value] of headers) {
      if (body.kind === "multipart" && name.toLowerCase() === "content-type") continue;
      lines.push(`  --header ${bashQuote(`${name}: ${value}`)}`);
    }
    if (body.kind === "text") {
      lines.push(`  --data-binary ${bashQuote(body.text)}`);
    } else if (body.kind === "multipart") {
      for (const field of body.fields) {
        lines.push(`  --form-string ${bashQuote(`${field.name}=${field.value}`)}`);
      }
      for (const file of body.files) {
        lines.push(
          `  --form ${bashQuote(`${file.name}=@/path/to/${file.filename};type=${file.contentType}`)}`,
        );
      }
    }
    return lines.map((line, index) => (
      index < lines.length - 1 ? `${line} \\` : line
    )).join("\n");
  };

  const buildPowerShellWebRequest = ({
    method,
    targetUrl,
    headers,
    body,
    accessToken,
  }) => {
    const contentTypeEntry = headers.find(([name]) => name.toLowerCase() === "content-type");
    const requestHeaders = headers.filter(([name]) => name.toLowerCase() !== "content-type");
    const lines = [
      "$headers = @{",
      `  "Authorization" = ${powerShellQuote(`Bearer ${accessToken}`)}`,
      ...requestHeaders.map(([name, value]) => (
        `  ${powerShellQuote(name)} = ${powerShellQuote(value)}`
      )),
      "}",
      `$uri = ${powerShellQuote(targetUrl)}`,
    ];
    if (body.kind === "text") {
      if (body.text.split(/\r?\n/).some((line) => line === "'@")) {
        throw new Error("请求 Body 无法安全表示为 PowerShell 5.x here-string。");
      }
      lines.push("$body = @'", body.text, "'@");
    }
    const invocation = [
      "Invoke-WebRequest",
      "-UseBasicParsing",
      "-Uri $uri",
      `-Method ${powerShellQuote(method)}`,
      "-Headers $headers",
    ];
    if (contentTypeEntry) invocation.push(`-ContentType ${powerShellQuote(contentTypeEntry[1])}`);
    if (body.kind === "text") {
      invocation.push("-Body ([System.Text.Encoding]::UTF8.GetBytes($body))");
    }
    lines.push(invocation.join(" `\n  "));
    return lines.join("\n");
  };

  const buildPowerShellMultipart = ({
    method,
    targetUrl,
    headers,
    body,
    accessToken,
  }) => {
    const requestHeaders = headers.filter(([name]) => name.toLowerCase() !== "content-type");
    const lines = [
      "Add-Type -AssemblyName System.Net.Http",
      "$client = New-Object System.Net.Http.HttpClient",
      "$request = New-Object System.Net.Http.HttpRequestMessage",
      `$request.Method = New-Object System.Net.Http.HttpMethod(${powerShellQuote(method)})`,
      `$request.RequestUri = ${powerShellQuote(targetUrl)}`,
      "$multipart = New-Object System.Net.Http.MultipartFormDataContent",
      "$fileStreams = New-Object System.Collections.Generic.List[System.IDisposable]",
      "try {",
      `  $request.Headers.TryAddWithoutValidation("Authorization", `
        + `${powerShellQuote(`Bearer ${accessToken}`)}) | Out-Null`,
      ...requestHeaders.map(([name, value]) => (
        `  $request.Headers.TryAddWithoutValidation(${powerShellQuote(name)}, `
          + `${powerShellQuote(value)}) | Out-Null`
      )),
    ];
    body.fields.forEach((field, index) => {
      lines.push(
        `  $field${index} = New-Object System.Net.Http.StringContent(${powerShellQuote(field.value)})`,
        `  $multipart.Add($field${index}, ${powerShellQuote(field.name)})`,
      );
    });
    body.files.forEach((file, index) => {
      const path = `C:\\path\\to\\${file.filename}`;
      lines.push(
        `  $filePath${index} = ${powerShellQuote(path)}`,
        `  $stream${index} = [System.IO.File]::OpenRead($filePath${index})`,
        `  $fileStreams.Add($stream${index})`,
        `  $file${index} = New-Object System.Net.Http.StreamContent($stream${index})`,
        `  $file${index}.Headers.ContentType = `
          + `[System.Net.Http.Headers.MediaTypeHeaderValue]::Parse(${powerShellQuote(file.contentType)})`,
        `  $multipart.Add($file${index}, ${powerShellQuote(file.name)}, `
          + `[System.IO.Path]::GetFileName($filePath${index}))`,
      );
    });
    lines.push(
      "  $request.Content = $multipart",
      "  $response = $client.SendAsync($request).GetAwaiter().GetResult()",
      "  $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()",
      "  [pscustomobject]@{ StatusCode = [int]$response.StatusCode; Content = $content }",
      "} finally {",
      "  foreach ($stream in $fileStreams) { $stream.Dispose() }",
      "  $request.Dispose()",
      "  $client.Dispose()",
      "}",
    );
    return lines.join("\n");
  };

  const buildPython = ({ method, targetUrl, headers, body, accessToken }) => {
    const requestHeaders = Object.fromEntries([
      ["Authorization", `Bearer ${accessToken}`],
      ...headers.filter(([name]) => (
        body.kind !== "multipart" || name.toLowerCase() !== "content-type"
      )),
    ]);
    const lines = [
      "import httpx",
      "",
      `url = ${pythonQuote(targetUrl)}`,
      `headers = ${JSON.stringify(requestHeaders, null, 2)}`,
    ];
    if (body.kind === "multipart") {
      lines.unshift("from contextlib import ExitStack", "from pathlib import Path", "");
      lines.push("", "with ExitStack() as stack:", "    files = [");
      for (const field of body.fields) {
        lines.push(
          `        (${pythonQuote(field.name)}, (None, ${pythonQuote(field.value)})),`,
        );
      }
      lines.push("    ]");
      body.files.forEach((file, index) => {
        const path = `C:\\path\\to\\${file.filename}`;
        lines.push(
          `    file_path_${index} = Path(${pythonQuote(path)})`,
          "    files.append((",
          `        ${pythonQuote(file.name)},`,
          `        (file_path_${index}.name, stack.enter_context(file_path_${index}.open("rb")), `
            + `${pythonQuote(file.contentType)}),`,
          "    ))",
        );
      });
      lines.push(
        "    response = httpx.request(",
        `        ${pythonQuote(method)},`,
        "        url,",
        "        headers=headers,",
        "        files=files,",
        "        follow_redirects=False,",
        "        timeout=180.0,",
        "    )",
      );
    } else {
      if (body.kind === "text") {
        lines.push("", `body = ${pythonQuote(body.text)}.encode("utf-8")`);
      }
      lines.push(
        "",
        "response = httpx.request(",
        `    ${pythonQuote(method)},`,
        "    url,",
        "    headers=headers,",
        ...(body.kind === "text" ? ["    content=body,"] : []),
        "    follow_redirects=False,",
        "    timeout=180.0,",
        ")",
      );
    }
    lines.push(
      "",
      'print(f"HTTP {response.status_code}")',
      "print(response.text)",
      "response.raise_for_status()",
    );
    return lines.join("\n");
  };

  const buildRequestCommands = ({
    request,
    endpoint,
    deploymentName,
    accessToken,
  }) => {
    if (!request || !request.url || !request.method) throw new Error("请求信息不完整。");
    if (!endpoint) throw new Error("Azure Endpoint 信息不完整。");
    if (!accessToken) throw new Error("请先获取访问令牌，再执行请求并复制命令。");
    const sourceUrl = new URL(request.url, "http://127.0.0.1");
    const proxyPrefix = "/proxy/openai/v1";
    if (!sourceUrl.pathname.startsWith(`${proxyPrefix}/`)
      && sourceUrl.pathname !== proxyPrefix) {
      throw new Error("只能为 Azure OpenAI v1 代理请求生成命令。");
    }
    const apiPath = sourceUrl.pathname.slice(proxyPrefix.length) || "/";
    const targetUrl = `${endpoint.replace(/\/+$/, "")}/openai/v1${apiPath}${sourceUrl.search}`;
    const headers = normalizeHeaders(request.headers);
    const body = normalizeRequestBody(request.body, apiPath, deploymentName);
    const commandInput = {
      method: request.method.toUpperCase(),
      targetUrl,
      headers,
      body,
      accessToken,
    };
    return {
      bash: buildBash(commandInput),
      powershell: body.kind === "multipart"
        ? buildPowerShellMultipart(commandInput)
        : buildPowerShellWebRequest(commandInput),
      python: buildPython(commandInput),
      targetUrl,
      hasFilePlaceholders: body.files.length > 0,
    };
  };

  return { buildRequestCommands };
}));
