"use strict";

async function initialize() {
  const readLocalJson = window.AzureOpenAISessionFetch.readJsonResponse;
  const setHeaderStatus = (state, text) => {
    const dot = document.getElementById("header-status-dot");
    dot.className = `status-dot ${state}`;
    document.getElementById("header-status-text").textContent = text;
  };
  const setConnectionState = (state, text) => {
    const container = document.getElementById("connection-state");
    container.className = `connection-state ${state}`;
    document.getElementById("connection-label").textContent = text;
  };

  const response = await fetch("/api/status");
  const status = await readLocalJson(response);
  document.getElementById("tenant").textContent = status.tenant_id;
  document.getElementById("metrics").textContent =
    `${status.tag_count} API 分类 · ${status.operation_count} 操作 · OpenAPI ${status.schema_version} · 上传上限 ${status.max_upload_mb} MiB`;
  setHeaderStatus("ready", "本地代理已就绪");

  const authButton = document.getElementById("auth-check");
  const authButtonLabel = document.getElementById("auth-button-label");
  const copyTokenButton = document.getElementById("copy-token");
  const copyTokenLabel = document.getElementById("copy-token-label");
  const copyBearerTokenButton = document.getElementById("copy-bearer-token");
  const copyBearerTokenLabel = document.getElementById("copy-bearer-token-label");
  const copyTokenActions = copyTokenButton.closest(".copy-token-actions");
  const floatingTokenActions = document.getElementById("floating-token-actions");
  const floatingCopyTokenButton = document.getElementById("floating-copy-token");
  const floatingCopyTokenLabel = document.getElementById("floating-copy-token-label");
  const floatingCopyBearerTokenButton = document.getElementById("floating-copy-bearer-token");
  const floatingCopyBearerTokenLabel = document.getElementById(
    "floating-copy-bearer-token-label",
  );
  const accountSelect = document.getElementById("azure-account");
  const favoriteAccountButton = document.getElementById("favorite-account");
  const favoriteAccountUpButton = document.getElementById("favorite-account-up");
  const favoriteAccountDownButton = document.getElementById("favorite-account-down");
  const refreshAccountsButton = document.getElementById("refresh-accounts");
  const refreshEnvironmentsButton = document.getElementById("refresh-environments");
  const endpointSelect = document.getElementById("endpoint");
  const deploymentSelect = document.getElementById("deployment");
  const resourceResult = document.getElementById("resource-result");
  const subscriptionId = document.getElementById("subscription-id");
  const endpointDetail = document.getElementById("endpoint-detail");
  const deploymentDetail = document.getElementById("deployment-detail");
  const copyTenantIdButton = document.getElementById("copy-tenant-id");
  const copySubscriptionIdButton = document.getElementById("copy-subscription-id");
  const copyEndpointButton = document.getElementById("copy-endpoint");
  const copyModelButton = document.getElementById("copy-model");
  const resourcePortalLink = document.getElementById("open-resource");
  const deploymentPortalLink = document.getElementById("open-deployment");
  const discoveryProgress = [
    document.getElementById("resource-progress"),
    document.getElementById("deployment-progress"),
  ];
  const setDiscoveryLoading = (loading) => {
    for (const progress of discoveryProgress) {
      progress.hidden = !loading;
      progress.closest(".setup-step").setAttribute("aria-busy", String(loading));
    }
  };
  const setPortalLink = (link, url) => {
    link.setAttribute("aria-disabled", String(!url));
    if (url) {
      link.href = url;
      link.removeAttribute("tabindex");
    } else {
      link.removeAttribute("href");
      link.setAttribute("tabindex", "-1");
    }
  };
  const clearPortalLinks = () => {
    setPortalLink(resourcePortalLink, null);
    setPortalLink(deploymentPortalLink, null);
  };
  let accessToken = null;
  let accounts = [];
  let environments = [];
  let environmentRequestId = 0;
  let environmentAbortController = null;
  const favoritesStorageKey = "azure-openai-lab.favorite-subscriptions";
  const storedFavorites = localStorage.getItem(favoritesStorageKey);
  const parsedFavorites = storedFavorites ? JSON.parse(storedFavorites) : [];
  if (!Array.isArray(parsedFavorites) || parsedFavorites.some((value) => typeof value !== "string")) {
    throw new Error("收藏订阅的本地存储格式无效，请清除该站点的浏览器存储后重试。");
  }
  const favoriteOrder = [...new Set(parsedFavorites)];
  const favoriteSubscriptions = new Set(favoriteOrder);
  const persistFavoriteOrder = () => {
    localStorage.setItem(favoritesStorageKey, JSON.stringify(favoriteOrder));
  };
  const setResourceStatus = (state, text) => {
    resourceResult.className = `setup-feedback ${state}`;
    resourceResult.textContent = text;
  };
  const copyTextToClipboard = async (value) => {
    const text = String(value);
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.setAttribute("aria-hidden", "true");
    Object.assign(textarea.style, {
      position: "fixed",
      top: "0",
      left: "0",
      width: "1px",
      height: "1px",
      padding: "0",
      border: "0",
      opacity: "0",
      pointerEvents: "none",
    });
    document.body.append(textarea);
    let copied = false;
    try {
      textarea.focus({ preventScroll: true });
      textarea.select();
      textarea.setSelectionRange(0, text.length);
      copied = document.execCommand("copy");
    } finally {
      textarea.remove();
    }
    if (copied) return;
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    throw new Error("当前浏览器不允许写入系统剪贴板。");
  };
  const setCopyValue = (button, value) => {
    button.dataset.copyValue = value || "";
    button.disabled = !value;
    button.classList.remove("copied");
  };
  for (const button of [
    copyTenantIdButton,
    copySubscriptionIdButton,
    copyEndpointButton,
    copyModelButton,
  ]) {
    button.addEventListener("click", async () => {
      const value = button.dataset.copyValue;
      if (!value) return;
      button.disabled = true;
      try {
        await copyTextToClipboard(value);
        button.classList.add("copied");
        window.setTimeout(() => {
          button.classList.remove("copied");
        }, 1500);
      } catch (error) {
        setResourceStatus("error", `无法写入剪贴板：${error.message}`);
      } finally {
        button.disabled = false;
      }
    });
  }
  const clearToken = () => {
    accessToken = null;
    floatingTokenActions.hidden = true;
    copyTokenButton.disabled = true;
    copyTokenLabel.textContent = "复制 Token";
    copyBearerTokenButton.disabled = true;
    copyBearerTokenLabel.textContent = "复制 Bearer Token";
    setConnectionState("loading", "身份未验证");
    setRequestCommands(null);
  };
  const selectedAccount = () => accounts[Number(accountSelect.value)] || {
    tenant_id: "",
    subscription_id: null,
    subscription_name: "Azure CLI 默认账户",
  };
  const updateFavoriteButton = () => {
    const subscription = selectedAccount().subscription_id;
    const isFavorite = Boolean(subscription && favoriteSubscriptions.has(subscription));
    favoriteAccountButton.disabled = accountSelect.disabled || !subscription;
    favoriteAccountButton.setAttribute("aria-pressed", String(isFavorite));
    favoriteAccountButton.textContent = isFavorite ? "★ 已收藏" : "☆ 收藏";
    favoriteAccountButton.title = isFavorite ? "取消收藏当前订阅" : "收藏当前订阅";
    const favoriteIndex = isFavorite ? favoriteOrder.indexOf(subscription) : -1;
    favoriteAccountUpButton.disabled = accountSelect.disabled || favoriteIndex <= 0;
    favoriteAccountDownButton.disabled = (
      accountSelect.disabled
      || favoriteIndex < 0
      || favoriteIndex >= favoriteOrder.length - 1
    );
  };
  const renderAccountOptions = (preferredSubscriptionId = null) => {
    const defaultAccount = accounts.find((account) => account.is_default);
    const favoriteGroup = document.createElement("optgroup");
    favoriteGroup.label = "★ 已收藏";
    const otherGroup = document.createElement("optgroup");
    otherGroup.label = "其他订阅";

    const appendAccountOption = (account, index, group) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = account.tenant_name
        ? `${account.subscription_name} — ${account.tenant_name}`
        : account.subscription_name;
      group.append(option);
    };
    const accountIndexesBySubscription = new Map(
      accounts.map((account, index) => [account.subscription_id, index]),
    );
    favoriteOrder.forEach((subscription) => {
      const index = accountIndexesBySubscription.get(subscription);
      if (index !== undefined) appendAccountOption(accounts[index], index, favoriteGroup);
    });
    accounts.forEach((account, index) => {
      if (!favoriteSubscriptions.has(account.subscription_id)) {
        appendAccountOption(account, index, otherGroup);
      }
    });
    accountSelect.replaceChildren();
    if (favoriteGroup.children.length) accountSelect.append(favoriteGroup);
    if (otherGroup.children.length) accountSelect.append(otherGroup);
    const firstListedOption = accountSelect.querySelector("option");
    const firstListedAccount = firstListedOption
      ? accounts[Number(firstListedOption.value)]
      : null;
    const selectedSubscriptionId = (
      preferredSubscriptionId
      || firstListedAccount?.subscription_id
      || defaultAccount?.subscription_id
    );
    const selectedIndex = accounts.findIndex(
      (account) => account.subscription_id === selectedSubscriptionId,
    );
    accountSelect.value = String(selectedIndex >= 0 ? selectedIndex : 0);
    updateFavoriteButton();
  };
  const selectedEnvironment = () => environments[Number(endpointSelect.value)] || null;
  const selectedDeployment = () => {
    const environment = selectedEnvironment();
    return environment?.deployments[Number(deploymentSelect.value)] || null;
  };
  const updateAccountDetails = () => {
    const account = selectedAccount();
    document.getElementById("tenant").textContent = account.tenant_id || "—";
    subscriptionId.textContent = account.subscription_id || "—";
    setCopyValue(copyTenantIdButton, account.tenant_id);
    setCopyValue(copySubscriptionIdButton, account.subscription_id);
  };
  const updateDeploymentDetails = () => {
    const deployment = selectedDeployment();
    const environment = selectedEnvironment();
    const tenantId = selectedAccount().tenant_id;
    const deploymentQuery = new URLSearchParams({
      wsid: environment?.resource_id || "",
      tid: tenantId,
    });
    setPortalLink(
      deploymentPortalLink,
      environment?.resource_id && deployment?.name && tenantId
        ? `https://ai.azure.com/resource/deployments/${encodeURIComponent(deployment.name)}?${deploymentQuery}`
        : null,
    );
    if (!deployment) {
      deploymentDetail.textContent = "当前 Endpoint 没有可用的 OpenAI 部署。";
      setCopyValue(copyModelButton, null);
      return;
    }
    deploymentDetail.textContent = deployment.name;
    deploymentDetail.title = deploymentDetail.textContent;
    setCopyValue(copyModelButton, deployment.name);
  };
  const populateDeployments = () => {
    const environment = selectedEnvironment();
    const tenantId = selectedAccount().tenant_id;
    setPortalLink(
      resourcePortalLink,
      environment?.resource_id && tenantId
        ? `https://portal.azure.com/#@${encodeURIComponent(tenantId)}/resource${environment.resource_id.split("/").map(encodeURIComponent).join("/")}/overview`
        : null,
    );
    endpointDetail.textContent = environment?.endpoint || "未选择 Azure Endpoint。";
    endpointDetail.title = endpointDetail.textContent;
    setCopyValue(copyEndpointButton, environment?.endpoint);
    deploymentSelect.replaceChildren();
    if (!environment?.deployments.length) {
      const option = document.createElement("option");
      option.textContent = "此资源没有 OpenAI 部署";
      deploymentSelect.append(option);
      deploymentSelect.disabled = true;
      updateDeploymentDetails();
      return;
    }
    environment.deployments.forEach((deployment, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      const model = [deployment.model_name, deployment.model_version].filter(Boolean).join(" ");
      option.textContent = model ? `${deployment.name} (${model})` : deployment.name;
      deploymentSelect.append(option);
    });
    deploymentSelect.disabled = false;
    updateDeploymentDetails();
  };
  const loadEnvironments = async () => {
    const account = selectedAccount();
    const requestId = ++environmentRequestId;
    environmentAbortController?.abort();
    environmentAbortController = new AbortController();
    const { signal } = environmentAbortController;
    refreshEnvironmentsButton.disabled = true;
    clearPortalLinks();
    setDiscoveryLoading(true);
    setCopyValue(copyEndpointButton, null);
    setCopyValue(copyModelButton, null);
    endpointSelect.disabled = true;
    deploymentSelect.disabled = true;
    endpointSelect.replaceChildren();
    deploymentSelect.replaceChildren();
    endpointDetail.textContent = "正在查询 Azure Resource Manager…";
    deploymentDetail.textContent = "等待 Endpoint 加载完成…";
    setResourceStatus("loading", `正在读取 ${account.subscription_name} 中的 Azure OpenAI 资源…`);
    try {
      const query = new URLSearchParams({
        tenant_id: account.tenant_id,
        subscription_id: account.subscription_id,
      });
      const response = await fetch(`/api/azure-environments?${query}`, {
        headers: { "X-Lab-Session": status.session_token },
        signal,
      });
      const result = await readLocalJson(response);
      const currentAccount = selectedAccount();
      if (
        requestId !== environmentRequestId
        || currentAccount.tenant_id !== account.tenant_id
        || currentAccount.subscription_id !== account.subscription_id
      ) {
        return;
      }
      environments = result.environments;
      if (!environments.length) {
        throw new Error("所选订阅中未发现可访问的 Azure OpenAI 资源。");
      }
      const endpointGroups = new Map();
      environments.forEach((environment, index) => {
        let group = endpointGroups.get(environment.resource_name);
        if (!group) {
          group = document.createElement("optgroup");
          group.label = environment.resource_name;
          endpointGroups.set(environment.resource_name, group);
          endpointSelect.append(group);
        }
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = `${environment.endpoint_type} — ${environment.endpoint}`;
        group.append(option);
      });
      const usableIndex = environments.findIndex((environment) => environment.deployments.length > 0);
      endpointSelect.value = String(usableIndex >= 0 ? usableIndex : 0);
      endpointSelect.disabled = false;
      populateDeployments();
      const resourceCount = new Set(
        environments.map((environment) => environment.resource_name),
      ).size;
      const deploymentCount = environments.reduce(
        (deploymentsByResource, environment) => {
          if (!deploymentsByResource.has(environment.resource_name)) {
            deploymentsByResource.set(environment.resource_name, environment.deployments.length);
          }
          return deploymentsByResource;
        },
        new Map(),
      );
      setResourceStatus(
        "success",
        `资源已就绪：发现 ${resourceCount} 个资源、${environments.length} 个 Endpoint、`
          + `${[...deploymentCount.values()].reduce((total, count) => total + count, 0)} 个可用部署。`,
      );
    } catch (error) {
      if (signal.aborted || requestId !== environmentRequestId) return;
      environments = [];
      const endpointOption = document.createElement("option");
      endpointOption.textContent = "未发现可用 Azure OpenAI 资源";
      endpointSelect.append(endpointOption);
      const deploymentOption = document.createElement("option");
      deploymentOption.textContent = "无可用部署";
      deploymentSelect.append(deploymentOption);
      endpointDetail.textContent = "资源加载失败。";
      deploymentDetail.textContent = "无可用部署。";
      setCopyValue(copyEndpointButton, null);
      setCopyValue(copyModelButton, null);
      setResourceStatus("error", error.message);
    } finally {
      if (requestId === environmentRequestId) {
        environmentAbortController = null;
        refreshEnvironmentsButton.disabled = false;
        setDiscoveryLoading(false);
      }
    }
  };
  const loadAccounts = async () => {
    const authOutput = document.getElementById("auth-result");
    const previousSubscriptionId = accounts.length ? selectedAccount().subscription_id : null;
    refreshAccountsButton.disabled = true;
    clearPortalLinks();
    accountSelect.disabled = true;
    favoriteAccountButton.disabled = true;
    authButton.disabled = true;
    setResourceStatus("loading", "正在读取 Azure CLI 登录账户…");
    try {
      const response = await fetch("/api/azure-accounts", {
        headers: { "X-Lab-Session": status.session_token },
      });
      const result = await readLocalJson(response);
      accounts = result.accounts;
      if (!accounts.length) {
        throw new Error("Azure CLI 中没有可用订阅。请先运行 az login。");
      }
      renderAccountOptions(previousSubscriptionId);
      accountSelect.disabled = false;
      updateFavoriteButton();
      authButton.disabled = false;
      const account = selectedAccount();
      updateAccountDetails();
      authOutput.className = "";
      authOutput.textContent = `当前账户：${account.subscription_name}。获取令牌不会调用模型或产生推理费用。`;
      clearToken();
      await loadEnvironments();
    } catch (error) {
      accounts = [];
      accountSelect.replaceChildren();
      const option = document.createElement("option");
      option.textContent = "未找到可用 Azure CLI 账户";
      accountSelect.append(option);
      updateFavoriteButton();
      updateAccountDetails();
      setResourceStatus("error", error.message);
      authOutput.className = "error";
      authOutput.textContent = "请先在终端运行 az login，然后刷新账户。";
      setConnectionState("error", "账户读取失败");
    } finally {
      refreshAccountsButton.disabled = false;
    }
  };
  accountSelect.addEventListener("change", async () => {
    const account = selectedAccount();
    updateFavoriteButton();
    updateAccountDetails();
    const output = document.getElementById("auth-result");
    output.className = "";
    output.textContent = `已切换到 ${account.subscription_name}，请重新获取访问令牌。`;
    clearToken();
    await loadEnvironments();
  });
  favoriteAccountButton.addEventListener("click", () => {
    const account = selectedAccount();
    if (!account.subscription_id) return;
    const wasFavorite = favoriteSubscriptions.has(account.subscription_id);
    if (wasFavorite) {
      favoriteSubscriptions.delete(account.subscription_id);
      favoriteOrder.splice(favoriteOrder.indexOf(account.subscription_id), 1);
    } else {
      favoriteSubscriptions.add(account.subscription_id);
      favoriteOrder.push(account.subscription_id);
    }
    persistFavoriteOrder();
    renderAccountOptions(account.subscription_id);
    setResourceStatus(
      "success",
      wasFavorite
        ? `已取消收藏 ${account.subscription_name}。`
        : `已收藏 ${account.subscription_name}，该订阅将显示在列表最前面。`,
    );
  });
  const moveFavoriteAccount = (offset) => {
    const account = selectedAccount();
    const currentIndex = favoriteOrder.indexOf(account.subscription_id);
    const targetIndex = currentIndex + offset;
    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= favoriteOrder.length) return;
    [favoriteOrder[currentIndex], favoriteOrder[targetIndex]] = [
      favoriteOrder[targetIndex],
      favoriteOrder[currentIndex],
    ];
    persistFavoriteOrder();
    renderAccountOptions(account.subscription_id);
    setResourceStatus(
      "success",
      `已将 ${account.subscription_name} ${offset < 0 ? "上移" : "下移"}。`,
    );
  };
  favoriteAccountUpButton.addEventListener("click", () => moveFavoriteAccount(-1));
  favoriteAccountDownButton.addEventListener("click", () => moveFavoriteAccount(1));
  endpointSelect.addEventListener("change", () => {
    populateDeployments();
    const environment = selectedEnvironment();
    setResourceStatus(
      "success",
      `当前 Endpoint：${environment.resource_name} / ${environment.endpoint_type}。请选择部署后即可测试 API。`,
    );
  });
  deploymentSelect.addEventListener("change", updateDeploymentDetails);
  refreshEnvironmentsButton.addEventListener("click", loadEnvironments);
  refreshAccountsButton.addEventListener("click", loadAccounts);
  authButton.addEventListener("click", async () => {
    const output = document.getElementById("auth-result");
    const account = selectedAccount();
    clearToken();
    authButton.disabled = true;
    authButtonLabel.textContent = "正在验证…";
    setConnectionState("loading", "正在验证");
    output.className = "";
    output.textContent = "正在通过 Azure CLI 获取令牌…";
    try {
      const auth = await fetch("/api/auth", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Lab-Session": status.session_token,
        },
        body: JSON.stringify({
          tenant_id: account.tenant_id,
          subscription_id: account.subscription_id,
        }),
      });
      const result = await readLocalJson(auth);
      if (typeof result.access_token !== "string" || !result.access_token) {
        throw new Error("认证响应未包含有效的访问令牌。");
      }
      accessToken = result.access_token;
      copyTokenButton.disabled = false;
      copyBearerTokenButton.disabled = false;
      updateFloatingTokenActions();
      output.className = "success";
      output.textContent = `${account.subscription_name}：${result.message} 有效期至 ${new Date(result.expires_at).toLocaleString()}。`;
      setConnectionState("success", "访问令牌已就绪");
    } catch (error) {
      output.className = "error";
      output.textContent = error.message;
      setConnectionState("error", "验证失败");
    } finally {
      authButton.disabled = false;
      authButtonLabel.textContent = "刷新访问令牌";
    }
  });
  const copyAccessToken = async (button, label, value, defaultLabel) => {
    const output = document.getElementById("auth-result");
    if (!accessToken) {
      output.className = "error";
      output.textContent = "请先验证 Entra ID，再复制 Token。";
      return;
    }
    button.disabled = true;
    syncFloatingTokenButtons();
    try {
      await copyTextToClipboard(value);
      label.textContent = "已复制";
    } catch (error) {
      label.textContent = defaultLabel;
      output.className = "error";
      output.textContent = `无法写入剪贴板：${error.message}`;
    } finally {
      button.disabled = false;
      syncFloatingTokenButtons();
    }
  };
  const syncFloatingTokenButtons = () => {
    floatingCopyTokenButton.disabled = copyTokenButton.disabled;
    floatingCopyBearerTokenButton.disabled = copyBearerTokenButton.disabled;
    floatingCopyTokenLabel.textContent = copyTokenLabel.textContent;
    floatingCopyBearerTokenLabel.textContent = copyBearerTokenLabel.textContent;
  };
  let floatingTokenUpdatePending = false;
  const updateFloatingTokenActions = () => {
    floatingTokenUpdatePending = false;
    if (!accessToken) {
      floatingTokenActions.hidden = true;
      return;
    }
    const headerHeight = document.querySelector(".app-header")?.getBoundingClientRect().height || 0;
    floatingTokenActions.hidden = (
      copyTokenActions.getBoundingClientRect().bottom > headerHeight + 8
    );
    syncFloatingTokenButtons();
  };
  const scheduleFloatingTokenUpdate = () => {
    if (floatingTokenUpdatePending) return;
    floatingTokenUpdatePending = true;
    window.requestAnimationFrame(updateFloatingTokenActions);
  };
  copyTokenButton.addEventListener("click", () => (
    copyAccessToken(copyTokenButton, copyTokenLabel, accessToken, "复制 Token")
  ));
  copyBearerTokenButton.addEventListener("click", () => (
    copyAccessToken(
      copyBearerTokenButton,
      copyBearerTokenLabel,
      `Bearer ${accessToken}`,
      "复制 Bearer Token",
    )
  ));
  floatingCopyTokenButton.addEventListener("click", () => copyTokenButton.click());
  floatingCopyBearerTokenButton.addEventListener("click", () => copyBearerTokenButton.click());
  window.addEventListener("scroll", scheduleFloatingTokenUpdate, { passive: true });
  window.addEventListener("resize", scheduleFloatingTokenUpdate);
  syncFloatingTokenButtons();

  const mutationToggle = document.getElementById("allow-mutations");
  const safetyCard = document.getElementById("safety-card");
  const safetyChip = document.getElementById("safety-chip");
  mutationToggle.addEventListener("change", () => {
    safetyCard.classList.toggle("unlocked", mutationToggle.checked);
    safetyChip.textContent = mutationToggle.checked ? "已解锁" : "已锁定";
  });

  const guideToggle = document.getElementById("guide-toggle");
  const guideContent = document.getElementById("guide-content");
  guideToggle.addEventListener("click", () => {
    const expanded = guideToggle.getAttribute("aria-expanded") === "true";
    guideToggle.setAttribute("aria-expanded", String(!expanded));
    guideToggle.textContent = expanded ? "展开指南" : "收起指南";
    guideContent.classList.toggle("is-collapsed", expanded);
  });

  const documentSelect = document.getElementById("api-document");
  const collectionSelect = document.getElementById("api-collection");
  const explorerRetry = document.getElementById("explorer-retry");
  const explorerError = document.getElementById("page-error");
  const collectionStatus = document.getElementById("collection-status");
  const swaggerContainer = document.getElementById("swagger-ui");
  const commandPanel = document.getElementById("request-command-panel");
  const commandStatus = document.getElementById("request-command-status");
  const copyBashCommand = document.getElementById("copy-bash-command");
  const copyPowerShellCommand = document.getElementById("copy-powershell-command");
  const copyPythonCommand = document.getElementById("copy-python-command");
  const historyRetentionMode = document.getElementById("history-retention-mode");
  const historyRetentionValue = document.getElementById("history-retention-value");
  const historyRetentionValueLabel = document.getElementById("history-retention-value-label");
  const saveHistorySettings = document.getElementById("save-history-settings");
  const historySettingsStatus = document.getElementById("history-settings-status");
  let documents = [];
  let activeDocument = null;
  let activeCollection = null;
  let swagger = null;
  let explorerBusy = false;
  let requestCount = 0;
  let hasEdits = false;
  let requestCommands = null;
  const initialSwaggerState = {};

  const setRequestCommands = (commands, error = null) => {
    requestCommands = commands;
    copyBashCommand.disabled = !commands;
    copyPowerShellCommand.disabled = !commands;
    copyPythonCommand.disabled = !commands;
    commandStatus.className = error ? "error" : commands ? "success" : "";
    if (error) {
      commandStatus.textContent = `无法生成请求命令：${error.message}`;
    } else if (commands) {
      commandStatus.textContent = commands.hasFilePlaceholders
        ? `已生成 ${commands.targetUrl} 的命令，已包含当前临时 Token。复制后请替换文件路径，且不要分享命令。`
        : `已生成 ${commands.targetUrl} 的命令，已包含当前内存中的临时 Token；请勿分享。`;
    } else {
      commandStatus.textContent = "获取访问令牌并执行请求后，可复制包含当前临时 Token 的 Bash curl、Windows PowerShell 5.x 命令或 Python 脚本。";
    }
  };
  const copyRequestCommand = async (button, key, defaultLabel) => {
    if (!requestCommands) return;
    const label = button.querySelector("span:last-child");
    button.disabled = true;
    try {
      await copyTextToClipboard(requestCommands[key]);
      label.textContent = "已复制";
      window.setTimeout(() => { label.textContent = defaultLabel; }, 1400);
    } catch (error) {
      commandStatus.className = "error";
      commandStatus.textContent = `无法写入剪贴板：${error.message}`;
    } finally {
      button.disabled = false;
    }
  };
  copyBashCommand.addEventListener("click", () => (
    copyRequestCommand(copyBashCommand, "bash", "复制 Bash curl")
  ));
  copyPowerShellCommand.addEventListener("click", () => (
    copyRequestCommand(copyPowerShellCommand, "powershell", "复制 PowerShell 5.x")
  ));
  copyPythonCommand.addEventListener("click", () => (
    copyRequestCommand(copyPythonCommand, "python", "复制 Python 脚本")
  ));

  const updateExplorerControls = () => {
    const disabled = explorerBusy || requestCount > 0 || !documents.length;
    documentSelect.disabled = disabled;
    collectionSelect.disabled = disabled;
    explorerRetry.disabled = explorerBusy || requestCount > 0;
    swaggerContainer.inert = explorerBusy;
    swaggerContainer.setAttribute("aria-busy", String(explorerBusy));
    if (requestCount > 0) collectionStatus.textContent = "请求执行中，请等待完成后再切换集合。";
    else if (!explorerBusy && activeCollection) {
      collectionStatus.textContent = `${activeCollection.title} · ${activeCollection.operation_count} 个操作 · 手动执行`;
    }
  };
  // Reset request state as well as the schema, so shared operations cannot retain old bodies/results.
  const collectionLifecycle = (system) => ({
    statePlugins: Object.fromEntries(["spec", "oas3", "layout", "err"].map((namespace) => [
      namespace,
      {
        actions: { resetForCollection: () => ({ type: "resetForCollection" }) },
        reducers: { resetForCollection: () => initialSwaggerState[namespace] },
        ...(namespace === "spec" ? {
          wrapActions: {
            executeRequest: (original) => async (...args) => {
              requestCount++;
              updateExplorerControls();
              try {
                return await original(...args);
              } finally {
                requestCount--;
                updateExplorerControls();
              }
            },
          },
        } : {}),
      },
    ])),
    wrapComponents: {
      App: (Original) => (props) => system.React.createElement(Original, {
        ...props,
        key: JSON.stringify(system.specSelectors.specJson().get("x-lab-collection")),
      }),
    },
  });
  const swaggerOptions = {
    dom_id: "#swagger-ui",
    plugins: [collectionLifecycle],
    deepLinking: true,
    filter: true,
    docExpansion: "none",
    defaultModelsExpandDepth: -1,
    defaultModelExpandDepth: 2,
    displayRequestDuration: true,
    displayOperationId: false,
    persistAuthorization: false,
    validatorUrl: null,
    showExtensions: false,
    tryItOutEnabled: false,
    // Keep the per-process local session secret out of generated curl snippets.
    showMutatedRequest: false,
    requestInterceptor(request) {
      const url = new URL(request.url, window.location.origin);
      if (url.origin !== window.location.origin) {
        throw new Error("仅允许向本机代理发送请求。");
      }
      if (url.pathname.startsWith("/proxy/openai/v1/")) {
        const environment = selectedEnvironment();
        const deployment = selectedDeployment();
        if (!environment) {
          throw new Error("请先选择 Azure Endpoint。");
        }
        request.headers["X-Lab-Session"] = status.session_token;
        request.headers["X-Lab-Environment"] = environment.environment_id;
        if (deployment) {
          request.headers["X-Lab-Deployment"] = deployment.name;
        }
        if (
          ["/responses", "/chat/completions", "/completions", "/embeddings"].some(
            (path) => url.pathname === `/proxy/openai/v1${path}`,
          )
          && !deployment
        ) {
          throw new Error("当前操作需要先选择模型部署。");
        }
        try {
          setRequestCommands(window.AzureOpenAICommandCopy.buildRequestCommands({
            request,
            endpoint: environment.endpoint,
            deploymentName: deployment?.name || null,
            accessToken,
          }));
        } catch (error) {
          setRequestCommands(null, error);
        }
        if (document.getElementById("allow-mutations").checked) {
          request.headers["X-Lab-Allow-Mutations"] = "true";
        }
      }
      return request;
    },
  };

  const sessionFetch = window.AzureOpenAISessionFetch.createSessionFetch({
    fetchImpl: window.fetch.bind(window),
    getToken: () => status.session_token,
    setToken: (token) => {
      status.session_token = token;
      setHeaderStatus("ready", "本地代理已重新连接");
    },
  });
  const fetchLocalJson = async (url, options = {}) => {
    const response = await sessionFetch(url, options);
    return readLocalJson(response);
  };
  const startupToggle = document.getElementById("startup-enabled");
  const startupRefresh = document.getElementById("refresh-startup");
  const startupResult = document.getElementById("startup-result");
  const startupChip = document.getElementById("startup-chip");
  const startupCard = document.getElementById("startup-card");
  let confirmedStartupEnabled = false;
  const showStartupSettings = (settings) => {
    if (
      typeof settings.supported !== "boolean"
      || typeof settings.enabled !== "boolean"
      || settings.mode !== "current-user-logon"
    ) {
      throw new Error("服务端返回的自启动设置格式无效。");
    }
    confirmedStartupEnabled = settings.enabled;
    startupToggle.checked = settings.enabled;
    startupToggle.disabled = !settings.supported;
    startupChip.textContent = !settings.supported ? "不支持" : settings.enabled ? "已开启" : "已关闭";
    startupResult.className = "";
    startupResult.textContent = !settings.supported
      ? "当前系统不支持此功能，仅支持 Windows 用户登录后自动启动。"
      : settings.enabled
        ? "已开启：下次登录 Windows 后将自动在后台启动本应用。"
        : "已关闭：下次登录 Windows 时不会自动启动本应用。";
  };
  const loadStartupSettings = async () => {
    startupToggle.disabled = true;
    startupRefresh.disabled = true;
    startupCard.setAttribute("aria-busy", "true");
    startupChip.textContent = "读取中";
    startupResult.className = "";
    startupResult.textContent = "正在读取 Windows 自启动设置…";
    try {
      showStartupSettings(await fetchLocalJson("/api/settings/startup"));
    } catch (error) {
      startupChip.textContent = "状态未知";
      startupResult.className = "error";
      startupResult.textContent = `无法读取自启动设置：${error.message} 请点击“刷新状态”重试。`;
    } finally {
      startupRefresh.disabled = false;
      startupCard.setAttribute("aria-busy", "false");
    }
  };
  startupRefresh.addEventListener("click", loadStartupSettings);
  startupToggle.addEventListener("change", async () => {
    const enabled = startupToggle.checked;
    startupToggle.disabled = true;
    startupRefresh.disabled = true;
    startupCard.setAttribute("aria-busy", "true");
    startupChip.textContent = "保存中";
    startupResult.className = "";
    startupResult.textContent = "正在保存 Windows 自启动设置…";
    try {
      const settings = await fetchLocalJson("/api/settings/startup", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (settings.enabled !== enabled) throw new Error("保存后的状态与选择不一致。");
      showStartupSettings(settings);
    } catch (error) {
      startupToggle.checked = confirmedStartupEnabled;
      startupChip.textContent = "待确认";
      startupResult.className = "error";
      startupResult.textContent = `自启动设置保存结果未确认：${error.message} 请刷新状态后重试。`;
    } finally {
      startupRefresh.disabled = false;
      startupCard.setAttribute("aria-busy", "false");
    }
  });
  const updateRetentionValue = () => {
    const mode = historyRetentionMode.value;
    const visible = mode !== "forever";
    historyRetentionValue.hidden = !visible;
    historyRetentionValueLabel.hidden = !visible;
    if (mode === "max_count") {
      historyRetentionValue.max = "10000";
      historyRetentionValueLabel.textContent = "每个 API 保留条数（1–10,000）";
    } else if (mode === "max_age_days") {
      historyRetentionValue.max = "3650";
      historyRetentionValueLabel.textContent = "保留天数（1–3,650）";
    }
  };
  const loadHistorySettings = async () => {
    try {
      const settings = await fetchLocalJson("/api/history/settings");
      historyRetentionMode.value = settings.retention_mode;
      if (settings.retention_value !== null) {
        historyRetentionValue.value = String(settings.retention_value);
      }
      updateRetentionValue();
      historySettingsStatus.className = "";
      historySettingsStatus.textContent = settings.retention_mode === "forever"
        ? "当前永久保留。"
        : `当前保留值：${settings.retention_value}。`;
    } catch (error) {
      historySettingsStatus.className = "error";
      historySettingsStatus.textContent = error.message;
    }
  };
  historyRetentionMode.addEventListener("change", updateRetentionValue);
  saveHistorySettings.addEventListener("click", async () => {
    saveHistorySettings.disabled = true;
    historySettingsStatus.className = "";
    historySettingsStatus.textContent = "正在保存并清理历史…";
    try {
      const mode = historyRetentionMode.value;
      const settings = await fetchLocalJson("/api/history/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          retention_mode: mode,
          retention_value: mode === "forever" ? null : Number(historyRetentionValue.value),
        }),
      });
      historySettingsStatus.className = "success";
      historySettingsStatus.textContent = `策略已保存，本次清理 ${settings.deleted_count} 条记录。`;
      swaggerContainer.querySelectorAll(".api-history.is-open").forEach((panel) => {
        panel.querySelector(".api-history-toggle").click();
        panel.querySelector(".api-history-toggle").click();
      });
    } catch (error) {
      historySettingsStatus.className = "error";
      historySettingsStatus.textContent = error.message;
    } finally {
      saveHistorySettings.disabled = false;
    }
  });
  loadHistorySettings();

  const operationIdentity = (block) => {
    const method = block.querySelector(".opblock-summary-method")?.textContent.trim().toUpperCase();
    const pathElement = block.querySelector(".opblock-summary-path");
    const path = pathElement?.dataset.path || pathElement?.textContent.trim();
    return method && path ? { method, path } : null;
  };
  const setNativeValue = (element, value) => {
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : element instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(element, value);
    else element.value = value;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const parameterRows = (block) => [...block.querySelectorAll("tr")].map((row) => {
    const nameElement = row.querySelector(".parameter__name");
    const location = row.querySelector(".parameter__in")?.textContent.trim().toLowerCase();
    const input = row.querySelector("input:not([type=file]), textarea, select");
    if (!nameElement || !input || !location) return null;
    const name = nameElement.childNodes[0]?.textContent.trim()
      || nameElement.textContent.trim().split(/\s+/)[0];
    return { name, location, input };
  }).filter(Boolean);
  const restoreHistoryRequest = async (block, historyId, message) => {
    message.className = "api-history-message";
    message.textContent = "正在恢复请求…";
    try {
      const snapshot = await fetchLocalJson(`/api/history/${historyId}/restore`);
      const tryButton = block.querySelector(".try-out__btn");
      if (tryButton && /try it out/i.test(tryButton.textContent)) {
        tryButton.click();
        await new Promise((resolve) => window.setTimeout(resolve, 50));
      }
      const valuesByLocation = {
        path: snapshot.path_parameters,
        query: snapshot.query_parameters,
        header: snapshot.headers,
      };
      parameterRows(block).forEach(({ name, location, input }) => {
        const values = valuesByLocation[location]?.[name];
        if (values === undefined) return;
        setNativeValue(input, Array.isArray(values) ? values.join(",") : values);
      });
      if (snapshot.body_text !== null) {
        const textarea = block.querySelector(".opblock-section-request-body textarea");
        if (textarea) {
          let bodyText = snapshot.body_text;
          try {
            bodyText = JSON.stringify(JSON.parse(bodyText), null, 2);
          } catch {
            // Preserve non-JSON text exactly.
          }
          setNativeValue(textarea, bodyText);
        }
      }
      for (const [name, values] of Object.entries(snapshot.form_fields)) {
        const row = parameterRows(block).find((item) => item.name === name);
        if (row) setNativeValue(row.input, values.join(","));
      }
      hasEdits = true;
      const fileNotice = snapshot.files.length
        ? ` 已恢复其他字段；请重新选择 ${snapshot.files.map((file) => file.filename).join("、")}。`
        : "";
      message.className = "api-history-message success";
      message.textContent = `请求已恢复，但不会自动执行。${fileNotice}`;
      block.scrollIntoView({ block: "nearest" });
    } catch (error) {
      message.className = "api-history-message error";
      message.textContent = `恢复失败：${error.message}`;
    }
  };
  const downloadHistoryContent = async (historyId, direction, message) => {
    message.className = "api-history-message";
    message.textContent = "正在准备下载…";
    try {
      const response = await sessionFetch(
        `/api/history/${historyId}/${direction}?download=true`,
      );
      if (!response.ok) await readLocalJson(response);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${historyId}-${direction}.bin`;
      link.click();
      URL.revokeObjectURL(url);
      message.textContent = "下载已开始。";
    } catch (error) {
      message.className = "api-history-message error";
      message.textContent = `下载失败：${error.message}`;
    }
  };
  const JSON_HIGHLIGHT_LIMIT = 512 * 1024;
  const renderHistoryCode = (container, source, contentType = "", truncated = false) => {
    let text = source || "";
    const looksLikeJson = /json/i.test(contentType) || /^\s*[\[{]/.test(text);
    if (looksLikeJson && !truncated) {
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        // Keep malformed or streaming JSON unchanged.
      }
    }
    container.replaceChildren();
    container.classList.toggle("is-json", looksLikeJson);
    if (!looksLikeJson || text.length > JSON_HIGHLIGHT_LIMIT) {
      container.textContent = text;
      if (looksLikeJson && text.length > JSON_HIGHLIGHT_LIMIT) {
        container.dataset.highlightNotice = "响应较大，已关闭语法高亮";
      } else {
        delete container.dataset.highlightNotice;
      }
      return text;
    }
    delete container.dataset.highlightNotice;
    const tokenPattern = /"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|null)\b|[{}[\],:]/g;
    let position = 0;
    for (const match of text.matchAll(tokenPattern)) {
      if (match.index > position) {
        container.append(document.createTextNode(text.slice(position, match.index)));
      }
      const token = match[0];
      const span = document.createElement("span");
      span.className = "json-token";
      if (token.startsWith('"')) {
        const remainder = text.slice(match.index + token.length);
        span.classList.add(/^\s*:/.test(remainder) ? "json-key" : "json-string");
      } else if (/^-?\d/.test(token)) {
        span.classList.add("json-number");
      } else if (token === "true" || token === "false") {
        span.classList.add("json-boolean");
      } else if (token === "null") {
        span.classList.add("json-null");
      } else {
        span.classList.add("json-punctuation");
      }
      span.textContent = token;
      container.append(span);
      position = match.index + token.length;
    }
    if (position < text.length) container.append(document.createTextNode(text.slice(position)));
    return text;
  };
  const showHistoryDetail = async (record, container, message) => {
    container.replaceChildren();
    try {
      const detail = await fetchLocalJson(`/api/history/${record.id}`);
      const detailHead = document.createElement("div");
      detailHead.className = "api-history-detail-head";
      const detailTitle = document.createElement("strong");
      detailTitle.textContent = "请求与响应元数据";
      const detailId = document.createElement("span");
      detailId.textContent = `ID ${record.id.slice(0, 8)}`;
      detailHead.append(detailTitle, detailId);
      const metadata = document.createElement("pre");
      renderHistoryCode(metadata, JSON.stringify({
        request: {
          method: detail.method,
          path: detail.actual_path,
          query: detail.query_string,
          content_type: detail.request_content_type,
          size: detail.request_size,
        },
        response: {
          status: detail.response_status,
          content_type: detail.response_content_type,
          size: detail.response_size,
          completed: detail.completed,
          storage_error: detail.storage_error,
        },
      }), "application/json");
      container.append(detailHead, metadata);
      const requestHeadersHead = document.createElement("div");
      requestHeadersHead.className = "api-history-detail-head";
      const requestHeadersTitle = document.createElement("strong");
      requestHeadersTitle.textContent = "请求 Headers";
      const requestHeadersNotice = document.createElement("span");
      requestHeadersNotice.textContent = "敏感值不保存";
      requestHeadersHead.append(requestHeadersTitle, requestHeadersNotice);
      const requestHeaders = document.createElement("pre");
      renderHistoryCode(
        requestHeaders,
        JSON.stringify(detail.request_headers || {}),
        "application/json",
      );
      const forwardedHead = document.createElement("div");
      forwardedHead.className = "api-history-detail-head";
      const forwardedTitle = document.createElement("strong");
      forwardedTitle.textContent = "实际发往 Azure 的请求";
      const forwardedNotice = document.createElement("span");
      forwardedNotice.textContent = "敏感 Header 值已脱敏";
      forwardedHead.append(forwardedTitle, forwardedNotice);
      const forwardedContent = document.createElement("div");
      if (detail.forwarded_url) {
        const forwardedMetadata = document.createElement("pre");
        renderHistoryCode(forwardedMetadata, JSON.stringify({
          method: detail.method,
          url: detail.forwarded_url,
          headers: detail.forwarded_headers,
          content_type: detail.forwarded_content_type,
          size: detail.forwarded_size,
          forwarded_at: detail.forwarded_at,
        }), "application/json");
        forwardedContent.append(forwardedMetadata);
        if (/^(application\/(.+\+)?json|application\/x-ndjson|text\/)/i.test(detail.forwarded_content_type)) {
          const forwardedResponse = await sessionFetch(
            `/api/history/${record.id}/forwarded-request`,
          );
          if (!forwardedResponse.ok) throw new Error(`HTTP ${forwardedResponse.status}`);
          const forwardedBody = document.createElement("pre");
          renderHistoryCode(
            forwardedBody,
            await forwardedResponse.text() || "无请求 Body",
            detail.forwarded_content_type,
          );
          forwardedContent.append(forwardedBody);
        } else {
          const binary = document.createElement("p");
          binary.className = "api-history-message";
          binary.textContent = "实际请求 Body 为二进制，请使用“下载实际请求”。";
          forwardedContent.append(binary);
        }
      } else if (detail.forwarded_state === 0) {
        const notForwarded = document.createElement("p");
        notForwarded.className = "api-history-message";
        notForwarded.textContent = "请求在本地校验或认证阶段终止，未发往 Azure。";
        forwardedContent.append(notForwarded);
      } else {
        const unavailable = document.createElement("p");
        unavailable.className = "api-history-message";
        unavailable.textContent = "该记录生成于实际转发日志功能启用前，未采集最终请求快照。";
        forwardedContent.append(unavailable);
      }
      const responseHeadersHead = document.createElement("div");
      responseHeadersHead.className = "api-history-detail-head";
      const responseHeadersTitle = document.createElement("strong");
      responseHeadersTitle.textContent = "响应 Headers";
      responseHeadersHead.append(responseHeadersTitle);
      const responseHeaders = document.createElement("pre");
      renderHistoryCode(
        responseHeaders,
        JSON.stringify(detail.response_headers || {}),
        "application/json",
      );
      container.append(
        requestHeadersHead,
        requestHeaders,
        forwardedHead,
        forwardedContent,
        responseHeadersHead,
        responseHeaders,
      );
      if (/^(application\/(.+\+)?json|application\/x-ndjson|text\/)/i.test(detail.response_content_type)) {
        const response = await sessionFetch(`/api/history/${record.id}/response`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const text = await response.text();
        const bodyHead = document.createElement("div");
        bodyHead.className = "api-history-detail-head";
        const bodyTitle = document.createElement("strong");
        bodyTitle.textContent = "完整响应";
        const bodySize = document.createElement("span");
        bodySize.textContent = formatHistoryBytes(detail.response_size);
        bodyHead.append(bodyTitle, bodySize);
        const body = document.createElement("pre");
        renderHistoryCode(body, text, detail.response_content_type);
        container.append(bodyHead, body);
      } else {
        const binary = document.createElement("p");
        binary.className = "api-history-message";
        binary.textContent = "二进制响应未内联显示，请使用“下载响应”。";
        container.append(binary);
      }
      container.hidden = false;
      message.textContent = "";
    } catch (error) {
      message.className = "api-history-message error";
      message.textContent = `读取详情失败：${error.message}`;
    }
  };
  const formatHistoryBytes = (bytes) => {
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB"];
    let value = bytes / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && value >= 1024; index++) {
      value /= 1024;
      unit = units[index];
    }
    return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${unit}`;
  };
  const formatHistoryTime = (value) => {
    const date = new Date(value);
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const ranges = [
      [60, "minute"],
      [60, "hour"],
      [24, "day"],
    ];
    let amount = seconds;
    let unit = "second";
    for (const [boundary, nextUnit] of ranges) {
      if (Math.abs(amount) < boundary) break;
      amount = Math.round(amount / boundary);
      unit = nextUnit;
    }
    if (Math.abs(amount) >= 7 && unit === "day") return date.toLocaleDateString();
    return new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" }).format(amount, unit);
  };
  const makeHistoryButton = (label, handler, variant = "") => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `api-history-button${variant ? ` ${variant}` : ""}`;
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
  };
  const loadOperationHistory = async (block, panel, offset = 0) => {
    const content = panel.querySelector(".api-history-content");
    const identity = operationIdentity(block);
    if (!identity) return;
    content.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "api-history-message";
    loading.textContent = "正在读取历史…";
    content.append(loading);
    try {
      const query = new URLSearchParams({
        document_id: activeDocument?.id || "azure-openai-v1",
        method: identity.method,
        path_template: identity.path,
        limit: "10",
        offset: String(offset),
      });
      const result = await fetchLocalJson(`/api/history?${query}`);
      content.replaceChildren();
      const toolbar = document.createElement("div");
      toolbar.className = "api-history-toolbar";
      const count = document.createElement("span");
      count.textContent = result.total
        ? `${result.total} 条记录 · 显示 ${result.offset + 1}–${Math.min(result.offset + result.records.length, result.total)}`
        : "尚无记录";
      toolbar.append(
        count,
        makeHistoryButton("刷新", () => loadOperationHistory(block, panel, offset), "quiet"),
        makeHistoryButton("清空当前 API", async () => {
          if (!window.confirm(`确定删除 ${identity.method} ${identity.path} 的全部历史吗？`)) return;
          try {
            await fetchLocalJson(`/api/history?${query}`, { method: "DELETE" });
            await loadOperationHistory(block, panel, 0);
          } catch (error) {
            count.className = "error";
            count.textContent = error.message;
          }
        }, "danger quiet"),
      );
      content.append(toolbar);
      const badge = panel.querySelector(".api-history-count");
      badge.textContent = result.total > 99 ? "99+" : String(result.total);
      badge.hidden = result.total === 0;
      const list = document.createElement("div");
      list.className = "api-history-list";
      if (!result.records.length) {
        const empty = document.createElement("div");
        empty.className = "api-history-empty";
        const emptyIcon = document.createElement("span");
        emptyIcon.className = "api-history-empty-icon";
        const emptyTitle = document.createElement("strong");
        emptyTitle.textContent = "暂无请求记录";
        const emptyDescription = document.createElement("span");
        emptyDescription.textContent = "执行此 API 后，请求和响应会安全地保存在本机。";
        empty.append(emptyIcon, emptyTitle, emptyDescription);
        list.append(empty);
      }
      result.records.forEach((record) => {
        const item = document.createElement("article");
        item.className = "api-history-record";
        const head = document.createElement("div");
        head.className = "api-history-record-head";
        const statusCode = document.createElement("span");
        const statusState = record.response_status === null
          ? "pending"
          : record.response_status >= 400
            ? "error"
            : record.response_status >= 300
              ? "warning"
              : "success";
        statusCode.className = `api-history-status ${statusState}`;
        statusCode.textContent = record.response_status === null ? "未完成" : `HTTP ${record.response_status}`;
        const created = document.createElement("span");
        created.className = "api-history-time";
        created.textContent = formatHistoryTime(record.created_at);
        created.title = new Date(record.created_at).toLocaleString();
        head.append(statusCode, created);
        const meta = document.createElement("div");
        meta.className = "api-history-meta";
        const duration = document.createElement("span");
        duration.textContent = record.duration_ms === null ? "耗时 —" : `耗时 ${record.duration_ms} ms`;
        const requestSize = document.createElement("span");
        requestSize.textContent = `请求 ${formatHistoryBytes(record.request_size)}`;
        const responseSize = document.createElement("span");
        responseSize.textContent = `响应 ${formatHistoryBytes(record.response_size)}`;
        meta.append(duration, requestSize, responseSize);
        if (!record.completed) {
          const incomplete = document.createElement("span");
          incomplete.className = "api-history-incomplete";
          incomplete.textContent = "数据不完整";
          meta.append(incomplete);
        }
        const preview = document.createElement("div");
        preview.className = "api-history-preview";
        const previewHead = document.createElement("div");
        previewHead.className = "api-history-preview-head";
        const previewLabel = document.createElement("span");
        const previewType = /json/i.test(record.response_content_type) ? "JSON" : "响应";
        previewLabel.textContent = record.response_summary_truncated
          ? `${previewType} 预览 · 已截断`
          : `${previewType} 预览`;
        const previewActions = document.createElement("div");
        previewActions.className = "api-history-preview-actions";
        const expandPreview = makeHistoryButton("展开", () => {
          const expanded = preview.classList.toggle("is-expanded");
          expandPreview.textContent = expanded ? "收起" : "展开";
        }, "quiet");
        const copyPreview = makeHistoryButton("复制", async () => {
          try {
            await copyTextToClipboard(summary.dataset.formattedText || record.response_summary || "");
            copyPreview.textContent = "已复制";
            window.setTimeout(() => { copyPreview.textContent = "复制"; }, 1200);
          } catch (error) {
            copyPreview.textContent = "复制失败";
          }
        }, "quiet");
        copyPreview.disabled = !record.response_summary;
        previewActions.append(expandPreview, copyPreview);
        previewHead.append(previewLabel, previewActions);
        const summary = document.createElement("pre");
        summary.className = "api-history-summary";
        const summaryText = renderHistoryCode(
          summary,
          record.response_summary || "无响应 Body",
          record.response_content_type,
          record.response_summary_truncated,
        );
        summary.dataset.formattedText = summaryText;
        preview.append(previewHead, summary);
        const actions = document.createElement("div");
        actions.className = "api-history-actions";
        const detail = document.createElement("div");
        detail.className = "api-history-detail";
        detail.hidden = true;
        const message = document.createElement("p");
        message.className = "api-history-message";
        const detailButton = makeHistoryButton("查看详情", () => {
          if (!detail.hidden) {
            detail.hidden = true;
            detailButton.textContent = "查看详情";
            return;
          }
          detailButton.textContent = "收起详情";
          showHistoryDetail(record, detail, message);
        });
        actions.append(
          makeHistoryButton("恢复请求", () => restoreHistoryRequest(block, record.id, message), "primary"),
          detailButton,
          makeHistoryButton("下载请求", () => downloadHistoryContent(record.id, "request", message), "quiet"),
          (() => {
            const downloadForwarded = makeHistoryButton(
            "下载实际请求",
            () => downloadHistoryContent(record.id, "forwarded-request", message),
            "quiet",
            );
            downloadForwarded.disabled = !record.forwarded;
            return downloadForwarded;
          })(),
          makeHistoryButton("下载响应", () => downloadHistoryContent(record.id, "response", message), "quiet"),
          makeHistoryButton("删除", async () => {
            if (!window.confirm("确定删除这条请求历史吗？")) return;
            try {
              await fetchLocalJson(`/api/history/${record.id}`, { method: "DELETE" });
              await loadOperationHistory(block, panel, offset);
            } catch (error) {
              message.className = "api-history-message error";
              message.textContent = error.message;
            }
          }, "danger quiet"),
        );
        item.append(head, meta, preview, actions, message, detail);
        list.append(item);
      });
      content.append(list);
      if (result.total > 10) {
        const pagination = document.createElement("div");
        pagination.className = "api-history-pagination";
        const previous = makeHistoryButton("上一页", () => loadOperationHistory(
          block, panel, Math.max(0, offset - 10),
        ));
        previous.disabled = offset === 0;
        const next = makeHistoryButton("下一页", () => loadOperationHistory(
          block, panel, offset + 10,
        ));
        next.disabled = offset + result.records.length >= result.total;
        const page = document.createElement("span");
        page.textContent = `${Math.floor(offset / 10) + 1} / ${Math.ceil(result.total / 10)}`;
        pagination.append(previous, page, next);
        content.append(pagination);
      }
    } catch (error) {
      loading.className = "api-history-message error";
      loading.textContent = `历史加载失败：${error.message}`;
      if (!loading.isConnected) content.replaceChildren(loading);
    }
  };
  const injectHistoryPanels = () => {
    swaggerContainer.querySelectorAll(".opblock").forEach((block) => {
      const body = block.querySelector(".opblock-body");
      if (!body || body.querySelector(":scope > .api-history")) return;
      const panel = document.createElement("section");
      panel.className = "api-history";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "api-history-toggle";
      toggle.setAttribute("aria-expanded", "false");
      const icon = document.createElement("span");
      icon.className = "api-history-toggle-icon";
      const label = document.createElement("span");
      label.className = "api-history-toggle-label";
      const title = document.createElement("strong");
      title.textContent = "请求历史";
      const subtitle = document.createElement("small");
      subtitle.textContent = "查看、恢复或下载此前请求";
      label.append(title, subtitle);
      const count = document.createElement("span");
      count.className = "api-history-count";
      count.hidden = true;
      const chevron = document.createElement("span");
      chevron.className = "api-history-chevron";
      toggle.append(icon, label, count, chevron);
      const content = document.createElement("div");
      content.className = "api-history-content";
      content.hidden = true;
      toggle.addEventListener("click", () => {
        const open = !panel.classList.contains("is-open");
        panel.classList.toggle("is-open", open);
        toggle.setAttribute("aria-expanded", String(open));
        content.hidden = !open;
        if (open) loadOperationHistory(block, panel);
      });
      panel.append(toggle, content);
      body.append(panel);
    });
  };
  const historyObserver = new MutationObserver(injectHistoryPanels);
  historyObserver.observe(swaggerContainer, { childList: true, subtree: true });

  swaggerContainer.addEventListener("input", () => { hasEdits = true; });
  swaggerContainer.addEventListener("change", () => { hasEdits = true; });
  swaggerContainer.addEventListener("click", (event) => {
    if (event.target.closest(".try-out__btn, .execute")) hasEdits = true;
  });

  const populateCollections = (document) => {
    collectionSelect.replaceChildren();
    for (const collection of document.collections) {
      const option = new Option(collection.title, collection.id);
      collectionSelect.add(option);
    }
  };
  const showCollectionDetails = (documentInfo, collection) => {
    const bindNavigation = (link, tag, operationId = null) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        swagger.layoutActions.updateFilter("");
        swagger.layoutActions.show(["operations-tag", tag], true);
        const target = operationId ? ["operations", tag, operationId] : ["operations-tag", tag];
        swagger.layoutActions.show(target, true);
        swaggerContainer.scrollIntoView({ block: "start" });
      });
    };
    document.getElementById("collection-description").textContent = collection.description;
    const source = document.getElementById("document-source");
    const sourceLink = document.createElement("a");
    sourceLink.href = documentInfo.source_url;
    sourceLink.textContent = "官方固定快照";
    sourceLink.target = "_blank";
    sourceLink.rel = "noopener noreferrer";
    source.replaceChildren(
      sourceLink,
      ` · API ${documentInfo.api_version} · OpenAPI ${documentInfo.schema_version} · ${documentInfo.retrieved}`
      + ` · commit ${documentInfo.source_commit.slice(0, 12)} · 其他 API 版本尚未接入`,
    );
    const links = document.getElementById("collection-links");
    links.replaceChildren();
    for (const tag of collection.tags) {
      const link = document.createElement("a");
      link.href = `#/${encodeURIComponent(tag)}`;
      link.textContent = tag;
      bindNavigation(link, tag);
      links.append(link);
    }
    const steps = document.getElementById("collection-steps");
    steps.replaceChildren();
    for (const step of collection.steps) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `#/${encodeURIComponent(step.tag)}/${encodeURIComponent(step.operation_id)}`;
      link.textContent = `${step.method} ${step.path}`;
      bindNavigation(link, step.tag, step.operation_id);
      item.append(link, step.instruction);
      steps.append(item);
    }
  };
  const switchCollection = async (documentInfo, collection) => {
    if (activeDocument?.id === documentInfo.id && activeCollection?.id === collection.id) return;
    if (hasEdits && !window.confirm("切换集合会清除当前 API 请求编辑、上传文件选择和响应结果。连接配置与令牌会保留。是否继续？")) {
      documentSelect.value = activeDocument.id;
      populateCollections(activeDocument);
      collectionSelect.value = activeCollection.id;
      return;
    }
    explorerBusy = true;
    updateExplorerControls();
    collectionStatus.textContent = `正在加载 ${collection.title}…`;
    explorerError.hidden = true;
    explorerRetry.hidden = true;
    try {
      const response = await fetch(collection.schema_url);
      const spec = await readLocalJson(response);
      if (spec["x-lab-collection"]?.document_id !== documentInfo.id
        || spec["x-lab-collection"]?.id !== collection.id || !spec.paths) {
        throw new Error("返回的 Schema 与所选集合不匹配。");
      }
      if (swagger) {
        if (window.location.hash.startsWith("#/")) {
          window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#api-explorer`);
        }
        for (const namespace of ["spec", "oas3", "layout", "err"]) {
          swagger[`${namespace}Actions`].resetForCollection();
        }
        swagger.specActions.updateSpec(JSON.stringify(spec));
      } else {
        swagger = SwaggerUIBundle({ ...swaggerOptions, spec });
        for (const namespace of ["spec", "oas3", "layout", "err"]) {
          initialSwaggerState[namespace] = swagger.getState().get(namespace);
        }
      }
      activeDocument = documentInfo;
      activeCollection = collection;
      hasEdits = false;
      setRequestCommands(null);
      showCollectionDetails(documentInfo, collection);
    } catch (error) {
      explorerError.hidden = false;
      explorerError.textContent = `集合加载失败：${error.message}。${activeCollection ? "已保留原集合，可重新选择后重试。" : "请点击重试加载。"}`;
      if (activeDocument) {
        documentSelect.value = activeDocument.id;
        populateCollections(activeDocument);
        collectionSelect.value = activeCollection.id;
      } else {
        explorerRetry.hidden = false;
        collectionStatus.textContent = "集合加载失败";
      }
    } finally {
      explorerBusy = false;
      updateExplorerControls();
    }
  };
  const loadExplorer = async () => {
    explorerBusy = true;
    updateExplorerControls();
    try {
      const response = await fetch("/api/explorer");
      const catalog = await readLocalJson(response);
      if (!catalog.documents?.length) throw new Error("没有可用的 API 文档。");
      documents = catalog.documents;
      documentSelect.replaceChildren();
      for (const documentInfo of documents) {
        documentSelect.add(new Option(documentInfo.title, documentInfo.id));
      }
      populateCollections(documents[0]);
      await switchCollection(documents[0], documents[0].collections[0]);
    } catch (error) {
      explorerError.hidden = false;
      explorerError.textContent = `无法加载文档目录：${error.message}。请重试。`;
      explorerRetry.hidden = false;
      collectionStatus.textContent = "文档目录加载失败";
    } finally {
      explorerBusy = false;
      updateExplorerControls();
    }
  };
  documentSelect.addEventListener("change", () => {
    const documentInfo = documents.find((item) => item.id === documentSelect.value);
    populateCollections(documentInfo);
    void switchCollection(documentInfo, documentInfo.collections[0]);
  });
  collectionSelect.addEventListener("change", () => {
    const documentInfo = documents.find((item) => item.id === documentSelect.value);
    const collection = documentInfo.collections.find((item) => item.id === collectionSelect.value);
    void switchCollection(documentInfo, collection);
  });
  explorerRetry.addEventListener("click", loadExplorer);
  await Promise.all([loadAccounts(), loadExplorer(), loadStartupSettings()]);
}

initialize().catch((error) => {
  document.getElementById("header-status-dot").className = "status-dot error";
  document.getElementById("header-status-text").textContent = "初始化失败";
  const output = document.getElementById("page-error");
  output.hidden = false;
  output.textContent = `站点加载失败：${error.message}。请检查服务端日志及本地依赖。`;
});
