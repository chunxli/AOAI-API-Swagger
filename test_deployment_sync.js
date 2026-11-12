"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { usesDeployment, replaceModel } = require("./static/deployment-sync.js");

test("only the four model-overridden POST operations use the selected deployment", () => {
  for (const path of ["/responses", "/chat/completions", "/completions", "/embeddings"]) {
    assert.equal(usesDeployment(path, "post"), true);
    assert.equal(usesDeployment(path, "POST"), true);
    assert.equal(usesDeployment(path, "get"), false);
  }
  for (const path of ["/batches", "/fine_tuning/jobs", "/audio/transcriptions", "/responses/{response_id}"]) {
    assert.equal(usesDeployment(path, "post"), false);
  }
});

test("replaces model while preserving other edited request fields", () => {
  const body = { model: "YOUR-CHAT-DEPLOYMENT", input: "edited prompt", stream: false, extra: { model: "nested" } };
  const updated = replaceModel(JSON.stringify(body), "deployment-one");
  assert.deepEqual(JSON.parse(updated), { ...body, model: "deployment-one" });
  assert.equal(JSON.parse(replaceModel(updated, "deployment-two")).model, "deployment-two");
  assert.equal(replaceModel(updated, "deployment-one"), updated);
  assert.deepEqual(JSON.parse(replaceModel('{"input":"no model yet"}', "deployment")), {
    input: "no model yet", model: "deployment",
  });
});

test("preserves incomplete JSON, non-object bodies and missing deployment selections", () => {
  for (const body of [undefined, null, "", '{"model":', "null", "[]", '"text"']) {
    assert.equal(replaceModel(body, "deployment"), body);
  }
  assert.equal(replaceModel('{"model":"original"}', null), '{"model":"original"}');
});
