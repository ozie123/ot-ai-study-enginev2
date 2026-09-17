import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../frontend/app.js", import.meta.url), "utf8");

function setup({ files = [], deleteStatus = 200, confirm = true } = {}) {
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
      listeners: {}, attributes: {}, children: [],
      addEventListener(name, handler) { this.listeners[name] = handler; },
      setAttribute(name, value) { this.attributes[name] = value; },
      replaceChildren() { this.children = []; },
      append(...children) { this.children.push(...children); },
    });
    return elements.get(id);
  }
  const response = (body, status = 200) => ({
    ok: status < 400, status,
    headers: { get: () => "application/json" }, json: async () => body,
  });
  let created = 0;
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => element(`created-${created++}`) },
    localStorage: { removeItem: (key) => removed.push(key) },
    window: { setTimeout, clearTimeout, confirm: () => confirm }, AbortController,
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (path === "/session") return pending;
      if (path === "/config") return response({ server_managed_ai: true, max_upload_mb: 25 });
      if (options.method === "DELETE") {
        if (deleteStatus >= 400) return response({ detail: "Changes could not be saved." }, deleteStatus);
        files = files.filter((file) => file.name !== decodeURIComponent(path.split("/").pop()));
        return response({ deleted: true });
      }
      return response({ documents: 0, chunks: 0, tags: [], ready: false, document_files: files });
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

test("library labels pending replacements and deletes with session credentials", async () => {
  const app = setup({ files: [{ name: "week 1.pdf", indexed: true, pending: true }] });
  app.complete();
  await settle();
  const row = app.elements.get("documentFiles").children[0];
  assert.equal(row.children[0].textContent, "week 1.pdf · Replacement pending");
  await row.children[1].listeners.click();
  const deletion = app.calls.find((call) => call.options.method === "DELETE");
  assert.equal(deletion.path, "/documents/week%201.pdf");
  assert.equal(deletion.options.credentials, "same-origin");
  assert.equal(deletion.options.headers["X-Study-Request"], "1");
  assert.equal(deletion.options.headers["X-User"], undefined);
  assert.equal(app.elements.get("documentFiles").children.length, 0);
});

test("failed deletion retains library entry and allows retry", async () => {
  const app = setup({ files: [{ name: "one.pdf", indexed: true }], deleteStatus: 500 });
  app.complete();
  await settle();
  const button = app.elements.get("documentFiles").children[0].children[1];
  await button.listeners.click();
  assert.equal(app.elements.get("documentFiles").children.length, 1);
  assert.equal(button.disabled, false);
  assert.match(app.elements.get("status").textContent, /Deletion failed/);
});

test("cancelled deletion sends no mutation", async () => {
  const app = setup({ files: [{ name: "one.pdf", indexed: true }], confirm: false });
  app.complete();
  await settle();
  await app.elements.get("documentFiles").children[0].children[1].listeners.click();
  assert.equal(app.calls.some((call) => call.options.method === "DELETE"), false);
});
