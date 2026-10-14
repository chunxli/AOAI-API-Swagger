"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createSessionFetch } = require("./static/session-fetch.js");

test("refreshes a stale local session after the site restarts", async () => {
  let token = "old-session";
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({
      url,
      token: options?.headers instanceof Headers
        ? options.headers.get("X-Lab-Session")
        : null,
    });
    if (url === "/api/status") {
      return new Response(JSON.stringify({ session_token: "new-session" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(
      calls.filter((call) => call.url === "/api/history").length === 1
        ? JSON.stringify({ error: { message: "stale session" } })
        : JSON.stringify({ total: 36 }),
      {
        status: calls.filter((call) => call.url === "/api/history").length === 1 ? 403 : 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  };
  const sessionFetch = createSessionFetch({
    fetchImpl,
    getToken: () => token,
    setToken: (value) => { token = value; },
  });

  const response = await sessionFetch("/api/history");

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { total: 36 });
  assert.deepEqual(calls, [
    { url: "/api/history", token: "old-session" },
    { url: "/api/status", token: null },
    { url: "/api/history", token: "new-session" },
  ]);
});

test("does not refresh a valid local session", async () => {
  let statusCalls = 0;
  const sessionFetch = createSessionFetch({
    fetchImpl: async (url) => {
      if (url === "/api/status") statusCalls++;
      return new Response("{}", { status: 200 });
    },
    getToken: () => "current-session",
    setToken: () => {},
  });

  const response = await sessionFetch("/api/history");

  assert.equal(response.status, 200);
  assert.equal(statusCalls, 0);
});
