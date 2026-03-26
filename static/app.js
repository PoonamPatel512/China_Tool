const queryInput = document.getElementById("queryInput");
const resolveBtn = document.getElementById("resolveBtn");
const pasteBtn = document.getElementById("pasteBtn");
const clearBtn = document.getElementById("clearBtn");
const copyBtn = document.getElementById("copyBtn");
const resultBox = document.getElementById("resultBox");
const statusEl = document.getElementById("status");
const metaEl = document.getElementById("meta");
const chinaTab = document.getElementById("chinaTab");
const nzTab = document.getElementById("nzTab");
const ausTab = document.getElementById("ausTab");
const inputLabel = document.getElementById("inputLabel");

let activeMode = "china";

function applyMode(mode) {
  activeMode = mode;
  const isChina = mode === "china";
  const isNz = mode === "nz";
  const isAus = mode === "aus";
  
  chinaTab.classList.toggle("active", isChina);
  nzTab.classList.toggle("active", isNz);
  ausTab.classList.toggle("active", isAus);
  
  resolveBtn.textContent = isChina ? "Resolve" : "Convert";
  inputLabel.textContent = isChina ? "Input" : "NOTAM Input";
  
  if (isChina) {
      queryInput.placeholder = "Example: B215: N373914E1011858 - N381302E1000042";
  } else if (isNz) {
      queryInput.placeholder = "Paste NZ NOTAM text with lines like: 0600-2035 MON-FRI";
  } else {
      queryInput.placeholder = "Paste Australia NOTAM text with lines like: SUN-FRI 1945-1345";
  }
  
  resultBox.textContent = "Waiting for input...";
  metaEl.textContent = "";
  
  let modeMsg = "China airway mode selected.";
  if (isNz) modeMsg = "NZ time converter mode selected.";
  if (isAus) modeMsg = "Australia time converter mode selected.";
  setStatus(modeMsg, "");
}

function setStatus(message, mode = "") {
  statusEl.textContent = message;
  statusEl.classList.remove("ok", "error");
  if (mode) {
    statusEl.classList.add(mode);
  }
}

function setBusy(isBusy) {
  resolveBtn.disabled = isBusy;
  if (isBusy) {
    resolveBtn.textContent = activeMode === "china" ? "Resolving..." : "Converting...";
  } else {
    resolveBtn.textContent = activeMode === "china" ? "Resolve" : "Convert";
  }
}

async function resolveQuery() {
  const query = queryInput.value.trim();
  if (!query) {
    setStatus("Please enter an airway query first.", "error");
    return;
  }

  setBusy(true);
  let loadingMsg = "Computing from live PDFs...";
  if (activeMode === "nz") loadingMsg = "Converting NZDT to UTC...";
  if (activeMode === "aus") loadingMsg = "Applying Australia day rules...";
  setStatus(loadingMsg, "");
  metaEl.textContent = "";

  try {
    let endpoint = "/api/resolve";
    if (activeMode === "nz") endpoint = "/api/nz-convert";
    if (activeMode === "aus") endpoint = "/api/aus-convert";
    
    const body = activeMode === "china" ? { query } : { text: query };

    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const payload = await response.json();

    if (!response.ok || !payload.ok) {
      const msg = payload && payload.error ? payload.error : "Failed to resolve query";
      resultBox.textContent = "ERROR";
      setStatus(msg, "error");
      return;
    }

    resultBox.textContent = payload.result;
    setStatus(activeMode === "china" ? "Resolved successfully." : "Converted successfully.", "ok");
    if (activeMode === "china") {
      const files = Array.isArray(payload.pdfsUsed) ? payload.pdfsUsed.length : 0;
      metaEl.textContent = `Fresh compute: yes | PDFs used: ${files} | Latency: ${payload.latencyMs} ms`;
    } else if (activeMode === "nz") {
      metaEl.textContent = `Mode: NZDT -> UTC | Latency: ${payload.latencyMs} ms`;
    } else {
      metaEl.textContent = `Mode: Australia Day Shift | Latency: ${payload.latencyMs} ms`;
    }
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
chinaTab.addEventListener("click", () => applyMode("china"));
nzTab.addEventListener("click", () => applyMode("nz"));
ausTab.addEventListener("click", () => applyMode("aus"));
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

applyMode("china");
queryInput.focus();
