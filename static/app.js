const queryInput = document.getElementById("queryInput");
const resolveBtn = document.getElementById("resolveBtn");
const pasteBtn = document.getElementById("pasteBtn");
const clearBtn = document.getElementById("clearBtn");
const copyBtn = document.getElementById("copyBtn");
const resultBox = document.getElementById("resultBox");
const statusEl = document.getElementById("status");
const metaEl = document.getElementById("meta");

function setStatus(message, mode = "") {
  statusEl.textContent = message;
  statusEl.classList.remove("ok", "error");
  if (mode) {
    statusEl.classList.add(mode);
  }
}

function setBusy(isBusy) {
  resolveBtn.disabled = isBusy;
  resolveBtn.textContent = isBusy ? "Resolving..." : "Resolve";
}

async function resolveQuery() {
  const query = queryInput.value.trim();
  if (!query) {
    setStatus("Please enter an airway query first.", "error");
    return;
  }

  setBusy(true);
  setStatus("Computing from live PDFs...", "");
  metaEl.textContent = "";

  try {
    const response = await fetch("/api/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });

    const payload = await response.json();

    if (!response.ok || !payload.ok) {
      const msg = payload && payload.error ? payload.error : "Failed to resolve query";
      resultBox.textContent = "ERROR";
      setStatus(msg, "error");
      return;
    }

    resultBox.textContent = payload.result;
    setStatus("Resolved successfully.", "ok");
    const files = Array.isArray(payload.pdfsUsed) ? payload.pdfsUsed.length : 0;
    metaEl.textContent = `Fresh compute: yes | PDFs used: ${files} | Latency: ${payload.latencyMs} ms`;
  } catch (error) {
    resultBox.textContent = "ERROR";
    setStatus("Server connection failed.", "error");
  } finally {
    setBusy(false);
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      queryInput.value = text.trim();
      setStatus("Pasted from clipboard.", "ok");
    } else {
      setStatus("Clipboard is empty.", "error");
    }
  } catch (error) {
    setStatus("Clipboard access not available. Use Ctrl+V.", "error");
  }
}

async function copyResult() {
  const text = resultBox.textContent.trim();
  if (!text || text === "Waiting for input..." || text === "ERROR") {
    setStatus("No valid output to copy.", "error");
    return;
  }

  try {
    await navigator.clipboard.writeText(text);
    setStatus("Output copied.", "ok");
  } catch (error) {
    setStatus("Copy failed. Select text manually.", "error");
  }
}

resolveBtn.addEventListener("click", resolveQuery);
pasteBtn.addEventListener("click", pasteFromClipboard);
clearBtn.addEventListener("click", () => {
  queryInput.value = "";
  resultBox.textContent = "Waiting for input...";
  metaEl.textContent = "";
  setStatus("Cleared.", "");
  queryInput.focus();
});
copyBtn.addEventListener("click", copyResult);

queryInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    resolveQuery();
  }
});

queryInput.focus();
