(function exposeSessionFetch(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AzureOpenAISessionFetch = api;
}(typeof globalThis === "object" ? globalThis : this, function sessionFetchFactory() {
  "use strict";

  const createSessionFetch = ({ fetchImpl, getToken, setToken }) => {
    if (typeof fetchImpl !== "function"
      || typeof getToken !== "function"
      || typeof setToken !== "function") {
      throw new TypeError("fetchImpl, getToken, and setToken are required.");
    }

    const withSessionHeader = (options) => {
      const headers = new Headers(options.headers || {});
      headers.set("X-Lab-Session", getToken());
      return { ...options, headers };
    };

    return async (url, options = {}) => {
      let response = await fetchImpl(url, withSessionHeader(options));
      if (response.status !== 403) return response;

      const statusResponse = await fetchImpl("/api/status", { cache: "no-store" });
      if (!statusResponse.ok) {
        throw new Error(`本地服务重启后无法刷新会话：HTTP ${statusResponse.status}`);
      }
      const status = await statusResponse.json();
      if (!status.session_token) {
        throw new Error("本地服务返回的会话信息无效。");
      }
      setToken(status.session_token);
      response = await fetchImpl(url, withSessionHeader(options));
      return response;
    };
  };

  return { createSessionFetch };
}));
