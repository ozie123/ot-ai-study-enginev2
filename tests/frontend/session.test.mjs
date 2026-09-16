import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../frontend/app.js", import.meta.url), "utf8");

function setup() {
  const elements = new Map();
  const calls = [];
  const removed = [];
  let resolveSession;
  const pending = new Promise((resolve) => { resolveSession = resolve; });
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", className: "", disabled: false,
      dataset: {}, files: [], lastChild: { textContent: "" },
      classList: { add() {}, remove() {} },
      addEventListener() {}, replaceChildren() {}, append() {},
    });
    return elements.get(id);
  }
  const response = (body, status = 200) => ({
    ok: status < 400, status,
    headers: { get: () => "application/json" }, json: async () => body,
  });
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => element("temporary") },
    localStorage: { removeItem: (key) => removed.push(key) },
    window: { setTimeout, clearTimeout }, AbortController,
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (path === "/session") return pending;
      if (path === "/config") return response({ server_managed_ai: true, max_upload_mb: 25 });
      return response({ documents: 0, chunks: 0, tags: [], ready: false });
    },
  });
  vm.runInContext(source, context);
  return { calls, removed, context, elements, complete: (status = 200) =>
    resolveSession(response(status === 200 ? { session_label: "s_example" } : { detail: "Unavailable" }, status)) };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("study requests wait for session setup and send cookies, not browser IDs", async () => {
  const app = setup();
  assert.deepEqual(app.calls.map((call) => call.path), ["/session", "/config"]);
  assert.equal(app.calls[0].options.headers["X-Study-Request"], "1");
  app.complete();
  await settle();
  const status = app.calls.find((call) => call.path === "/status");
  assert.ok(status);
  assert.equal(status.options.credentials, "same-origin");
  assert.equal(status.options.headers["X-User"], undefined);
  assert.equal(app.elements.get("sessionPreview").textContent, "s_example");
  assert.ok(app.removed.includes("ot_user_id"));
});

test("failed session creation blocks study requests rather than using a fallback ID", async () => {
  const app = setup();
  app.complete(503);
  await settle();
  assert.equal(app.calls.some((call) => call.path === "/status"), false);
  assert.equal(app.elements.get("sessionPreview").textContent, "Unavailable");
  await assert.rejects(vm.runInContext('apiFetch("/upload_zip", {method: "POST"})', app.context));
  assert.equal(app.calls.some((call) => call.path === "/upload_zip"), false);
});
