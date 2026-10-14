"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { buildRequestCommands } = require("./static/command-copy.js");

const baseInput = {
  endpoint: "https://example.openai.azure.com",
  deploymentName: "gpt-5'prod",
  accessToken: "existing-latest-token",
};

test("builds equivalent Bash and PowerShell 5 commands for JSON requests", () => {
  const commands = buildRequestCommands({
    ...baseInput,
    request: {
      method: "post",
      url: "http://127.0.0.1:8765/proxy/openai/v1/responses?api-version=v1",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Lab-Session": "local-secret",
        Authorization: "must-not-copy",
      },
      body: JSON.stringify({ model: "old-model", input: "it's ready" }),
    },
  });

  assert.equal(
    commands.targetUrl,
    "https://example.openai.azure.com/openai/v1/responses?api-version=v1",
  );
  assert.match(commands.bash, /curl --request 'POST'/);
  assert.match(commands.bash, /'Authorization: Bearer existing-latest-token'/);
  assert.doesNotMatch(commands.bash, /az account get-access-token/);
  assert.match(commands.bash, /"model": "gpt-5'"'"'prod"/);
  assert.doesNotMatch(commands.bash, /X-Lab-Session|local-secret|must-not-copy/);
  assert.match(commands.powershell, /Invoke-WebRequest/);
  assert.match(commands.powershell, /-UseBasicParsing/);
  assert.match(commands.powershell, /'Bearer existing-latest-token'/);
  assert.doesNotMatch(commands.powershell, /az account get-access-token/);
  assert.match(commands.powershell, /"model": "gpt-5'prod"/);
  assert.doesNotMatch(commands.powershell, /X-Lab-Session|local-secret|must-not-copy/);
  assert.match(commands.python, /import httpx/);
  assert.match(commands.python, /"Authorization": "Bearer existing-latest-token"/);
  assert.match(commands.python, /gpt-5'prod/);
  assert.doesNotMatch(commands.python, /az account get-access-token/);
  assert.doesNotMatch(commands.python, /X-Lab-Session|local-secret|must-not-copy/);
  assert.equal(commands.hasFilePlaceholders, false);
});

test("preserves GET query and omits a request body", () => {
  const commands = buildRequestCommands({
    ...baseInput,
    deploymentName: null,
    request: {
      method: "GET",
      url: "/proxy/openai/v1/models?limit=20&after=model-1",
      headers: { Accept: "application/json" },
    },
  });

  assert.match(commands.bash, /models\?limit=20&after=model-1/);
  assert.doesNotMatch(commands.bash, /--data/);
  assert.match(commands.powershell, /-Method 'GET'/);
  assert.doesNotMatch(commands.powershell, /-Body/);
  assert.match(commands.python, /"GET"/);
  assert.doesNotMatch(commands.python, /content=body/);
});

test("rejects non-proxy requests", () => {
  assert.throws(() => buildRequestCommands({
    ...baseInput,
    request: {
      method: "GET",
      url: "https://untrusted.example.test/data",
      headers: {},
    },
  }), /只能为 Azure OpenAI v1 代理请求生成命令/);
});

test("requires an existing access token", () => {
  assert.throws(() => buildRequestCommands({
    ...baseInput,
    accessToken: "",
    request: {
      method: "GET",
      url: "/proxy/openai/v1/models",
      headers: {},
    },
  }), /请先获取访问令牌/);
});

test("builds multipart commands with editable file paths", () => {
  const form = new FormData();
  form.append("purpose", "assistants");
  form.append("file", new Blob(["hello"], { type: "text/plain" }), "sample.txt");

  const commands = buildRequestCommands({
    ...baseInput,
    deploymentName: null,
    request: {
      method: "POST",
      url: "/proxy/openai/v1/files",
      headers: { "Content-Type": "multipart/form-data" },
      body: form,
    },
  });

  assert.match(commands.bash, /--form-string 'purpose=assistants'/);
  assert.match(commands.bash, /--form 'file=@\/path\/to\/sample.txt;type=text\/plain'/);
  assert.doesNotMatch(commands.bash, /Content-Type: multipart\/form-data/);
  assert.match(commands.powershell, /System\.Net\.Http\.MultipartFormDataContent/);
  assert.match(commands.powershell, /C:\\path\\to\\sample\.txt/);
  assert.match(commands.python, /from contextlib import ExitStack/);
  assert.ok(commands.python.includes('Path("C:\\\\path\\\\to\\\\sample.txt")'));
  assert.match(commands.python, /files=files/);
  assert.equal(commands.hasFilePlaceholders, true);
});
