const API_TIMEOUT_MS = 65_000;
const INGEST_TIMEOUT_MS = 180_000;

const elements = {
  aiStatus: document.getElementById("aiStatus"),
  apiKey: document.getElementById("apiKey"),
  keyPanel: document.getElementById("keyPanel"),
  zipFile: document.getElementById("zipFile"),
  fileName: document.getElementById("fileName"),
  dropZone: document.getElementById("dropZone"),
  tagFilter: document.getElementById("tagFilter"),
  mode: document.getElementById("mode"),
  status: document.getElementById("status"),
  uploadBtn: document.getElementById("uploadBtn"),
  ingestBtn: document.getElementById("ingestBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  documentCount: document.getElementById("documentCount"),
  documentFiles: document.getElementById("documentFiles"),
  chunkCount: document.getElementById("chunkCount"),
  indexState: document.getElementById("indexState"),
  tagSummary: document.getElementById("tagSummary"),
  sessionPreview: document.getElementById("sessionPreview"),
  uploadLimit: document.getElementById("uploadLimit"),
  question: document.getElementById("question"),
  askBtn: document.getElementById("askBtn"),
  copyAnswerBtn: document.getElementById("copyAnswerBtn"),
  answer: document.getElementById("answer"),
  answerBadge: document.getElementById("answerBadge"),
  sources: document.getElementById("sources"),
  flashcardTopic: document.getElementById("flashcardTopic"),
  flashcardBtn: document.getElementById("flashcardBtn"),
  flashcards: document.getElementById("flashcards"),
};

let publicConfig = { server_managed_ai: false };

// Legacy identifiers cannot prove ownership of a server-side session.
localStorage.removeItem("ot_user_id");
localStorage.removeItem("ot_api_key");
localStorage.removeItem("ot_accounts");
localStorage.removeItem("ot_logged_in");

function setNotice(message, type = "info") {
  elements.status.className = `notice ${type}`;
  elements.status.textContent = message;
}

function setLoading(button, isLoading, label) {
  if (isLoading) {
    button.dataset.label = button.textContent;
    button.replaceChildren();
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    button.append(spinner, document.createTextNode(label));
    button.disabled = true;
    return;
  }
  button.textContent = button.dataset.label || button.textContent;
  button.disabled = false;
}

function sessionHeaders({ json = false } = {}) {
  const headers = { "X-Study-Request": "1" };
  const key = elements.apiKey.value.trim();
  if (key) headers.Authorization = `Bearer ${key}`;
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

async function apiFetch(path, options = {}, timeoutMs = API_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    if (path !== "/config" && path !== "/session") await sessionReady;
    const response = await fetch(path, { ...options, credentials: "same-origin", signal: controller.signal });
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json")
      ? await response.json()
      : { detail: await response.text() };
    if (!response.ok) {
      const error = new Error(body.detail || "The request could not be completed.");
      error.status = response.status;
      throw error;
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request timed out. It may still finish on the server. Refresh the library to check before retrying.");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function ensureAIConfigured() {
  if (publicConfig.server_managed_ai || elements.apiKey.value.trim()) return true;
  elements.keyPanel.hidden = false;
  elements.keyPanel.open = true;
  elements.apiKey.focus();
  setNotice("Enter a restricted development API key for this browser session.", "warning");
  return false;
}

function validTag() {
  const tag = elements.tagFilter.value.trim();
  if (!tag) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/.test(tag)) {
    setNotice("Tags can use letters, numbers, underscores, and hyphens only.", "warning");
    return false;
  }
  return tag.toLowerCase();
}

function renderSources(sources = []) {
  elements.sources.replaceChildren();
  if (!sources.length) {
    const empty = document.createElement("p");
    empty.className = "empty-copy";
    empty.textContent = "No sources returned.";
    elements.sources.append(empty);
    return;
  }
  sources.forEach((source) => {
    const chip = document.createElement("span");
    chip.className = "source-chip";
    chip.textContent = String(source);
    elements.sources.append(chip);
  });
}

function renderFlashcards(cards = []) {
  elements.flashcards.replaceChildren();
  if (!cards.length) {
    const empty = document.createElement("p");
    empty.className = "empty-copy";
    empty.textContent = "No flashcards were generated.";
    elements.flashcards.append(empty);
    return;
  }

  cards.forEach((card, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "flashcard";
    button.setAttribute("aria-expanded", "false");

    const question = document.createElement("span");
    question.className = "flashcard-question";
    const questionText = document.createElement("span");
    const number = document.createElement("span");
    number.className = "flashcard-number";
    number.textContent = `${index + 1}. `;
    questionText.append(number, document.createTextNode(String(card.question)));
    const hint = document.createElement("span");
    hint.setAttribute("aria-hidden", "true");
    hint.textContent = "+";
    question.append(questionText, hint);

    const answer = document.createElement("p");
    answer.className = "flashcard-answer";
    answer.textContent = String(card.answer);
    answer.hidden = true;
    button.append(question, answer);
    button.addEventListener("click", () => {
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!expanded));
      answer.hidden = expanded;
      hint.textContent = expanded ? "+" : "−";
    });
    elements.flashcards.append(button);
  });
}

async function loadConfig() {
  try {
    publicConfig = await apiFetch("/config", {}, 10_000);
    elements.uploadLimit.textContent = `ZIP · up to ${publicConfig.max_upload_mb} MB`;
    if (publicConfig.server_managed_ai) {
      elements.aiStatus.classList.add("ready");
      elements.aiStatus.lastChild.textContent = " AI service ready";
      elements.keyPanel.hidden = true;
    } else {
      elements.aiStatus.classList.remove("ready");
      elements.aiStatus.lastChild.textContent = " Development key required";
      elements.keyPanel.hidden = false;
    }
  } catch {
    elements.aiStatus.lastChild.textContent = " Service unavailable";
    setNotice("The service configuration could not be loaded. Try refreshing.", "error");
  }
}

async function refreshStatus() {
  elements.refreshBtn.disabled = true;
  try {
    const data = await apiFetch("/status", { headers: sessionHeaders() }, 10_000);
    elements.documentCount.textContent = String(data.documents || 0);
    elements.chunkCount.textContent = String(data.chunks || 0);
    elements.indexState.textContent = data.ready ? "Ready" : "Not ready";
    elements.tagSummary.textContent = data.tags?.length ? data.tags.join(", ") : "None yet";
    renderDocuments(data.document_files || []);
  } catch (error) {
    setNotice(`Library status failed: ${error.message}`, "error");
  } finally {
    elements.refreshBtn.disabled = false;
  }
}

function renderDocuments(files) {
  elements.documentFiles.replaceChildren();
  files.forEach((file) => {
    const row = document.createElement("div");
    row.className = "document-row";
    const label = document.createElement("span");
    label.textContent = `${file.name} · ${file.pending ? (file.indexed ? "Replacement pending" : "Pending index") : "Indexed"}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    button.textContent = "Delete";
    button.setAttribute("aria-label", `Delete ${file.name}`);
    button.addEventListener("click", async () => {
      if (!window.confirm(`Delete ${file.name} and all its indexed notes?`)) return;
      button.disabled = true;
      try {
        await apiFetch(`/documents/${encodeURIComponent(file.name)}`, {
          method: "DELETE", headers: sessionHeaders(),
        });
        setNotice(`${file.name} deleted.`, "success");
        await refreshStatus();
      } catch (error) {
        setNotice(`Deletion failed: ${error.message}`, "error");
      } finally {
        button.disabled = false;
      }
    });
    row.append(label, button);
    elements.documentFiles.append(row);
  });
}

elements.refreshBtn.addEventListener("click", refreshStatus);

elements.zipFile.addEventListener("change", () => {
  const file = elements.zipFile.files[0];
  elements.fileName.hidden = !file;
  elements.fileName.textContent = file?.name || "";
  if (file) setNotice("File selected. Upload it when ready.");
});

["dragenter", "dragover"].forEach((eventName) => {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("dragover");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragover");
  });
});

elements.dropZone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  elements.zipFile.files = transfer.files;
  elements.zipFile.dispatchEvent(new Event("change"));
});

elements.uploadBtn.addEventListener("click", async () => {
  const file = elements.zipFile.files[0];
  if (!file) {
    setNotice("Choose a ZIP file first.", "warning");
    return;
  }
  if (!file.name.toLowerCase().endsWith(".zip")) {
    setNotice("Only ZIP files are accepted.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  setLoading(elements.uploadBtn, true, "Uploading…");
  setNotice("Uploading and validating the ZIP…");
  try {
    const data = await apiFetch("/upload_zip", {
      method: "POST",
      headers: sessionHeaders(),
      body: formData,
    });
    setNotice(`${data.uploaded_pdfs} PDF file${data.uploaded_pdfs === 1 ? "" : "s"} uploaded safely. Build the index next.`, "success");
    await refreshStatus();
  } catch (error) {
    setNotice(`Upload failed: ${error.message}`, "error");
  } finally {
    setLoading(elements.uploadBtn, false);
  }
});

elements.ingestBtn.addEventListener("click", async () => {
  if (!ensureAIConfigured()) return;
  const tag = validTag();
  if (tag === false) return;
  setLoading(elements.ingestBtn, true, "Building index…");
  setNotice("Extracting text and creating the study index. This may take a moment…");
  try {
    const data = await apiFetch("/ingest_user_pdfs", {
      method: "POST",
      headers: sessionHeaders({ json: true }),
      body: JSON.stringify({ tag }),
    }, INGEST_TIMEOUT_MS);
    const duplicateText = data.already_ingested ? ` ${data.already_ingested} already indexed.` : "";
    setNotice(`${data.message} Added ${data.added_chunks} note sections.${duplicateText}`, "success");
    await refreshStatus();
  } catch (error) {
    setNotice(`Indexing failed: ${error.message}`, "error");
  } finally {
    setLoading(elements.ingestBtn, false);
  }
});

elements.askBtn.addEventListener("click", async () => {
  if (!ensureAIConfigured()) return;
  const question = elements.question.value.trim();
  const tag = validTag();
  if (tag === false) return;
  if (!question) {
    setNotice("Enter a question first.", "warning");
    elements.question.focus();
    return;
  }

  setLoading(elements.askBtn, true, "Generating…");
  elements.answer.className = "answer-text empty";
  elements.answer.textContent = "Searching your notes and preparing an answer…";
  elements.answerBadge.textContent = "Working";
  elements.copyAnswerBtn.disabled = true;
  renderSources();
  try {
    const data = await apiFetch("/chat", {
      method: "POST",
      headers: sessionHeaders({ json: true }),
      body: JSON.stringify({ question, tag, mode: elements.mode.value }),
    });
    elements.answer.className = "answer-text";
    elements.answer.textContent = data.answer || "No answer was returned.";
    const answerStates = {
      answered: "Sources retrieved",
      not_found: "Not found",
      not_ready: "Not ready",
    };
    elements.answerBadge.textContent = answerStates[data.status] || "Unknown";
    elements.copyAnswerBtn.disabled = !data.answer;
    renderSources(data.sources || []);
    setNotice("Answer generated. Review the retrieved pages for important details.", "success");
  } catch (error) {
    elements.answer.className = "answer-text empty";
    elements.answer.textContent = "The answer could not be generated. Your notes are still available.";
    elements.answerBadge.textContent = "Failed";
    setNotice(`Question failed: ${error.message}`, "error");
  } finally {
    setLoading(elements.askBtn, false);
  }
});

elements.copyAnswerBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(elements.answer.textContent);
    setNotice("Answer copied to the clipboard.", "success");
  } catch {
    setNotice("Copy was blocked by the browser. Select the answer text manually.", "warning");
  }
});

elements.flashcardBtn.addEventListener("click", async () => {
  if (!ensureAIConfigured()) return;
  const topic = elements.flashcardTopic.value.trim();
  const tag = validTag();
  if (tag === false) return;
  if (!topic) {
    setNotice("Enter a flashcard topic first.", "warning");
    elements.flashcardTopic.focus();
    return;
  }

  setLoading(elements.flashcardBtn, true, "Creating cards…");
  elements.flashcards.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "empty-copy";
  loading.textContent = "Creating six grounded flashcards…";
  elements.flashcards.append(loading);
  try {
    const data = await apiFetch("/flashcards", {
      method: "POST",
      headers: sessionHeaders({ json: true }),
      body: JSON.stringify({ topic, tag }),
    });
    renderFlashcards(data.flashcards || []);
    setNotice(data.flashcards?.length ? "Flashcards ready. Select a card to reveal its answer." : (data.message || "No cards were generated."), data.flashcards?.length ? "success" : "warning");
  } catch (error) {
    renderFlashcards();
    setNotice(`Flashcards failed: ${error.message}`, "error");
  } finally {
    setLoading(elements.flashcardBtn, false);
  }
});

async function initializeSession() {
  const data = await apiFetch("/session", {
    method: "POST", headers: { "X-Study-Request": "1" },
  }, 10_000);
  elements.sessionPreview.textContent = data.session_label;
}

const sessionReady = initializeSession();
sessionReady.catch(() => {
  elements.sessionPreview.textContent = "Unavailable";
  setNotice("Could not start a study session. Check cookies are enabled and refresh.", "error");
});
Promise.all([loadConfig(), refreshStatus()]);
