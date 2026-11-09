"use strict";

const $ = (id) => document.getElementById(id);
const settings = JSON.parse($("ui-settings").textContent);
const historyPageSize = 24;
let records = [];
let historyTotal = 0;
let database = null;
let currentURL = null;
let currentID = null;
let deletionIDs = null;
let historyBefore = null;
const historyIndex = new Map();
const selectedIDs = new Set();
const collapsedDates = new Set();
let releasing = false;
let thumbnailURLs = [];
const activeJobs = new Map();
let polling = false;
let optionDefinitions = {};
let optionDefaults = {};
let optionsReady = false;
let optionsPending = null;
let optionsKey = null;
let restoreOptions = null;
let modelPresets = {};

const presetsReady = (async () => {
  try {
    const response = await fetch(
      new URL("assets/model-presets.json", document.baseURI),
      { cache: "no-cache" },
    );
    if (!response.ok) throw new Error("Cannot load model presets");
    modelPresets = await response.json();
    updateModelPreset();
  } catch {
    $("preset-description").textContent =
      "模型预设暂不可用，仍可手动配置全部参数。";
  }
})();

function updateModelPreset() {
  const selected = document.getElementById("option-model")?.value;
  const preset = modelPresets[selected];
  $("apply-model-preset").disabled = !preset;
  $("apply-model-preset").textContent = "为所选模型生成默认提示词";
  $("preset-source").hidden = !preset;
  if (preset) $("preset-source").href = preset.source;
}

$("idle-note").textContent =
  `空闲 ${settings.idle_seconds % 60 === 0 ? settings.idle_seconds / 60 + " 分钟" : settings.idle_seconds + " 秒"}自动释放模型。下次生成时重新加载。`;
if (settings.auth_required) {
  $("auth-details").open = true;
  $("advanced-parameters").open = true;
  $("api-key").placeholder = "此服务需要 API key";
  $("api-key").required = true;
}

function headers() {
  const key = $("api-key").value.trim();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

function api(path, options = {}) {
  return fetch(new URL(path, document.baseURI), {
    ...options,
    headers: { ...headers(), ...options.headers },
    cache: "no-store",
    credentials: "same-origin",
  });
}

async function responseError(response) {
  let detail;
  try {
    const body = await response.json();
    detail = typeof body.detail === "string" ? body.detail : null;
    if (Array.isArray(body.detail)) {
      detail = body.detail
        .map((item) => `${item.loc.slice(1).join(".")}: ${item.msg}`)
        .join("；");
    }
  } catch {
    /* A proxy may return an HTML error page. */
  }
  const messages = {
    401: "请在“连接凭据”中填写正确的 API key。",
    429: "队列已满或排队超时，请稍后重试。",
    503: "模型加载或生成失败，请查看服务日志后重试。",
    504: "加载或生成超时，请稍后重试。",
  };
  return new Error(
    [messages[response.status] || `请求失败（HTTP ${response.status}）`, detail]
      .filter(Boolean)
      .join(" "),
  );
}

async function pollStatus() {
  if (polling || document.hidden) return;
  polling = true;
  try {
    const response = await api("status", {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw await responseError(response);
    const status = await response.json();
    $("model-status").textContent = status.active
      ? `处理中 · ${Math.max(0, status.pending - 1)} 个排队`
      : status.loaded
        ? "模型已加载 · 等待创作"
        : "模型已释放 · 随时可用";
    $("status-dot").className = `dot ${status.active ? "busy" : "ready"}`;
    $("release-model").disabled =
      releasing || !status.loaded || status.active || status.pending > 0;
  } catch (error) {
    $("release-model").disabled = true;
    $("model-status").textContent =
      error.message.includes("401") || error.message.includes("API key")
        ? "需要 API key"
        : "服务暂时无法连接";
    $("status-dot").className = "dot offline";
  } finally {
    polling = false;
  }
}
setInterval(pollStatus, 4000);
document.addEventListener("visibilitychange", pollStatus);
$("api-key").addEventListener("change", pollStatus);
void pollStatus();

$("release-model").addEventListener("click", async () => {
  releasing = true;
  $("release-model").disabled = true;
  try {
    const response = await api("unload", {
      method: "POST",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw await responseError(response);
  } catch (error) {
    $("error").textContent = error.message;
    $("error").hidden = false;
  } finally {
    releasing = false;
    await pollStatus();
  }
});

// Cancel only wheel events that would change a focused number input, then
// forward their scroll to the panel so the form stays scrollable.
document.addEventListener(
  "wheel",
  (event) => {
    const input = event.target.closest?.('input[type="number"]');
    if (!input || document.activeElement !== input || event.ctrlKey) return;
    event.preventDefault();
    const panel = matchMedia("(min-width: 761px)").matches
      ? input.closest(".controls")
      : document.scrollingElement;
    const unit =
      event.deltaMode === 1
        ? 16
        : event.deltaMode === 2
          ? panel.clientHeight
          : 1;
    panel.scrollBy({ top: event.deltaY * unit, left: event.deltaX * unit });
  },
  { passive: false },
);
for (const button of document.querySelectorAll("[data-adjust]")) {
  button.addEventListener("click", () => {
    const input = $(button.dataset.adjust);
    const value = Number.isFinite(input.valueAsNumber)
      ? input.valueAsNumber
      : Number(input.defaultValue);
    input.value = Math.min(
      Number(input.max),
      Math.max(
        Number(input.min),
        Math.round((value + Number(button.dataset.delta)) * 1000000) / 1000000,
      ),
    );
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
$("reset-seed").addEventListener("click", () => {
  $("seed").value = -1;
});

function revealControl(input) {
  for (let node = input.parentElement; node; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.open = true;
  }
}
$("generate-form").addEventListener(
  "invalid",
  (event) => revealControl(event.target),
  true,
);

function applyModelParameters() {
  const preset = modelPresets[document.getElementById("option-model")?.value];
  if (!preset) return;
  for (const [name, definition] of Object.entries(optionDefinitions)) {
    if (definition.group && definition.group !== "模型与显存") {
      setOptionValue(name, preset.parameters[name] ?? optionDefaults[name]);
    }
  }
  $("width").value = preset.parameters.width;
  $("height").value = preset.parameters.height;
  $("steps").value = preset.parameters.steps;
  $("cfg").value = preset.parameters.cfg_scale;
  syncSizePreset();
}

function setOptionValue(name, value) {
  const input = document.getElementById(`option-${name}`);
  if (!input) return;
  input.setCustomValidity("");
  if (
    input.tagName === "SELECT" &&
    value != null &&
    ![...input.options].some((option) => option.value === value)
  ) {
    const missing = document.createElement("option");
    missing.value = value;
    missing.textContent = `${value}（当前不可用）`;
    input.append(missing);
  }
  if (input.type === "checkbox") input.checked = Boolean(value);
  else if (input.dataset.format === "json")
    input.value = value == null ? "" : JSON.stringify(value);
  else
    input.value =
      value == null
        ? ""
        : value === "" && input.dataset.nullable === "true"
          ? '\"\"'
          : value;
}

async function loadOptions() {
  const key = $("api-key").value.trim();
  if (optionsPending) {
    const pendingKey = optionsKey;
    const result = await optionsPending;
    return pendingKey === key ? result : loadOptions();
  }
  optionsKey = key;
  optionsPending = (async () => {
    try {
      const response = await api("options", {
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw await responseError(response);
      const result = await response.json();
      optionDefinitions = result.schema.properties;
      optionDefaults = Object.fromEntries(
        Object.entries(optionDefinitions).map(([name, field]) => [
          name,
          result.defaults[name] ?? field.default,
        ]),
      );
      const groups = new Map();
      $("generation-options").replaceChildren();
      $("model-selection").replaceChildren();
      for (const [name, definition] of Object.entries(optionDefinitions)) {
        if (!definition.group) continue;
        let group =
          name === "model"
            ? $("model-selection")
            : groups.get(definition.group);
        if (!group) {
          group = document.createElement("details");
          group.className = "option-group";
          const summary = document.createElement("summary");
          summary.textContent = definition.group;
          group.append(summary);
          groups.set(definition.group, group);
          $("generation-options").append(group);
        }
        const schema =
          definition.anyOf?.find((item) => item.type !== "null") || definition;
        const nullable = Boolean(
          definition.anyOf?.some((item) => item.type === "null"),
        );
        const choices = name === "model" ? result.models : schema.enum;
        const input = document.createElement(
          choices
            ? "select"
            : schema.type === "string" ||
                schema.type === "object" ||
                schema.type === "array"
              ? "textarea"
              : "input",
        );
        input.id = `option-${name}`;
        input.dataset.nullable = String(nullable);
        input.dataset.option = name;
        if (name === "model")
          input.addEventListener("change", () => {
            applyModelParameters();
            updateModelPreset();
          });
        input.addEventListener("invalid", () => {
          revealControl(input);
        });
        if (choices) {
          for (const value of choices) {
            const choice = document.createElement("option");
            choice.value = value;
            choice.textContent = value;
            input.append(choice);
          }
        } else if (schema.type === "boolean") input.type = "checkbox";
        else if (schema.type === "number" || schema.type === "integer") {
          input.type = "number";
          input.step = schema.type === "integer" ? "1" : "any";
          if (schema.minimum != null) input.min = schema.minimum;
          if (schema.maximum != null) input.max = schema.maximum;
          input.required = !nullable;
        } else {
          input.rows = 2;
          input.dataset.format = schema.type === "string" ? "text" : "json";
          if (schema.maxLength) input.maxLength = schema.maxLength;
          input.addEventListener("input", () => input.setCustomValidity(""));
        }
        const label = document.createElement("label");
        label.htmlFor = input.id;
        label.textContent = definition.title;
        group.append(label, input);
        if (definition.description && name !== "model") {
          const note = document.createElement("p");
          note.className = "option-note";
          note.textContent = definition.description;
          group.append(note);
        }
        setOptionValue(name, restoreOptions?.[name] ?? optionDefaults[name]);
      }
      optionsReady = true;
      await presetsReady;
      if (!restoreOptions) applyModelParameters();
      updateModelPreset();
      restoreOptions = null;
      $("options-state").textContent =
        "所有参数随本次请求提交，并保存到历史记录。";
      $("retry-options").hidden = true;
      return true;
    } catch (error) {
      $("options-state").textContent = error.message;
      $("retry-options").hidden = false;
      $("advanced-parameters").open = true;
      return false;
    } finally {
      optionsPending = null;
    }
  })();
  return optionsPending;
}

function readOptionValues() {
  const values = {};
  for (const input of document.querySelectorAll("[data-option]")) {
    let value;
    try {
      value =
        input.type === "checkbox"
          ? input.checked
          : input.value === "" && input.dataset.nullable === "true"
            ? null
            : input.type === "number"
              ? Number(input.value)
              : input.dataset.format === "json"
                ? JSON.parse(input.value)
                : input.dataset.nullable === "true" && input.value === '\"\"'
                  ? ""
                  : input.value;
    } catch {
      input.setCustomValidity("请输入有效 JSON");
      revealControl(input);
      input.reportValidity();
      throw new Error("高级参数中有无效 JSON，请修正后重试。");
    }
    values[input.dataset.option] = value;
  }
  return values;
}

$("retry-options").addEventListener("click", loadOptions);
$("api-key").addEventListener("change", () => {
  if (!optionsReady) void loadOptions();
});
void loadOptions();

function syncSizePreset() {
  const value = `${$("width").value}x${$("height").value}`;
  $("size").value = [...$("size").options].some(
    (option) => option.value === value,
  )
    ? value
    : "custom";
}
$("size").addEventListener("change", () => {
  if ($("size").value !== "custom")
    [$("width").value, $("height").value] = $("size").value.split("x");
});
for (const name of ["width", "height"])
  $(name).addEventListener("input", syncSizePreset);

$("apply-model-preset").addEventListener("click", async () => {
  await presetsReady;
  const preset = modelPresets[$("option-model")?.value];
  if (!preset) return;
  $("prompt").value = preset.parameters.prompt;
  $("negative-prompt").value = preset.parameters.negative_prompt;
  $("preset-description").textContent =
    `已填入 ${preset.label} 的默认提示词，可继续编辑；未修改其他参数。`;
});

function storageNotice(message) {
  $("storage-notice").textContent = message;
  $("storage-notice").hidden = false;
}

async function openHistory() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(
      `diffusion-history:${new URL(document.baseURI).pathname}`,
      2,
    );
    request.onupgradeneeded = () => {
      const store = request.result.objectStoreNames.contains("images")
        ? request.transaction.objectStore("images")
        : request.result.createObjectStore("images", { keyPath: "id" });
      store.createIndex("created-id", ["created", "id"], { unique: true });
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("历史数据库被其他页面占用"));
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => {
        db.close();
        database = null;
      };
      resolve(db);
    };
  });
}

function readHistory(before = null) {
  return new Promise((resolve, reject) => {
    const tx = database.transaction("images", "readonly");
    const store = tx.objectStore("images");
    const count = store.count();
    const range = before
      ? IDBKeyRange.upperBound([before.created, before.id], true)
      : null;
    const request = store.index("created-id").openCursor(range, "prev");
    const items = [];
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor && items.length < historyPageSize) {
        items.push(cursor.value);
        if (items.length < historyPageSize) cursor.continue();
      }
    };
    tx.oncomplete = () => resolve({ items, total: count.result });
    tx.onabort = () => reject(tx.error);
    tx.onerror = () => reject(tx.error);
  });
}

function updateHistory(record, removeIDs = []) {
  return new Promise((resolve, reject) => {
    const tx = database.transaction("images", "readwrite");
    const store = tx.objectStore("images");
    if (record) store.put(record);
    for (const id of removeIDs) store.delete(id);
    const count = store.count();
    tx.oncomplete = () => resolve(count.result);
    tx.onabort = () => reject(tx.error);
    tx.onerror = () => reject(tx.error);
  });
}

function showRecord(record, restore = false, includeSeed = false) {
  clearPreview();
  currentID = record.id;
  const pending = activeJobs.has(record.id) && record.status === "pending";
  $("canvas").setAttribute("aria-busy", String(pending));
  $("working").hidden = !pending;
  $("empty-state").hidden = pending || Boolean(record.blob);
  if (record.blob) {
    currentURL = URL.createObjectURL(record.blob);
    $("preview").src = currentURL;
    $("preview").alt = record.params.prompt;
    $("preview").hidden = false;
    $("download").href = currentURL;
    $("download").download = `diffusion-${record.seed}-${record.created}.png`;
    $("download").hidden = false;
  } else if (record.status === "failed") {
    $("empty-title").textContent = "本次生成未完成";
    $("empty-description").textContent =
      "可以复用此条目的参数，修改后重新提交。";
    $("error").textContent = record.error;
    $("error").hidden = false;
  } else if (!pending) {
    $("empty-title").textContent = "请求状态未知";
    $("empty-description").textContent =
      "此请求来自先前页面或其他标签页，无法在这里接收结果。可查看服务状态后重新提交。";
  }
  $("preview-meta").textContent =
    `${record.params.width} × ${record.params.height} · ${record.params.steps} 步`;
  $("result-info").textContent =
    `Seed ${record.seed} · ${new Date(record.created).toLocaleString("zh-CN")}`;
  updateElapsed();
  for (const card of $("history").querySelectorAll(".history-card")) {
    card.classList.toggle("selected", card.dataset.recordId === currentID);
  }
  if (restore) {
    $("prompt").value = record.params.prompt;
    $("negative-prompt").value = record.params.negative_prompt;
    $("width").value = record.params.width;
    $("height").value = record.params.height;
    syncSizePreset();
    $("steps").value = record.params.steps;
    $("cfg").value = record.params.cfg_scale;
    if (includeSeed) $("seed").value = record.seed;
    restoreOptions = record.params;
    if (optionsReady) {
      for (const [name, definition] of Object.entries(optionDefinitions)) {
        if (definition.group)
          setOptionValue(name, record.params[name] ?? optionDefaults[name]);
      }
      updateModelPreset();
    }
  }
}

function renderHistory() {
  thumbnailURLs.forEach((url) => URL.revokeObjectURL(url));
  thumbnailURLs = [];
  $("history").replaceChildren();
  $("history-count").textContent = historyTotal;
  $("history-empty").hidden = historyIndex.size > 0;
  $("clear-history").disabled = historyTotal === 0;
  $("load-history").hidden = !database || records.length >= historyTotal;
  const dateGrids = new Map();
  const dates = [
    ...new Set([...historyIndex.values()].map((item) => dateKey(item.created))),
  ]
    .sort()
    .reverse();
  for (const date of dates) {
    const ids = [...historyIndex.values()]
      .filter((item) => dateKey(item.created) === date)
      .map((item) => item.id);
    const day = document.createElement("details");
    day.className = "history-day";
    day.dataset.date = date;
    day.open = !collapsedDates.has(date);
    day.addEventListener("toggle", () => {
      if (day.open) collapsedDates.delete(date);
      else collapsedDates.add(date);
    });
    const heading = document.createElement("summary");
    heading.textContent = `${date} · ${ids.length} 项`;
    const actions = document.createElement("div");
    actions.className = "day-actions";
    const label = document.createElement("label");
    label.className = "day-selection";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.selectDate = date;
    checkbox.setAttribute("aria-label", `选择 ${date} 的全部记录`);
    checkbox.addEventListener("change", () => {
      for (const id of ids) {
        if (checkbox.checked) selectedIDs.add(id);
        else selectedIDs.delete(id);
      }
      updateSelection();
    });
    label.append(checkbox, "选择整天");
    actions.append(label);
    const loaded = new Set(records.map((record) => record.id));
    if (ids.some((id) => !loaded.has(id))) {
      const load = document.createElement("button");
      load.type = "button";
      load.className = "text-button load-day";
      load.textContent = "显示当天全部记录";
      load.addEventListener("click", async () => {
        load.disabled = true;
        try {
          const items = await getHistoryRecords(
            ids.filter((id) => !loaded.has(id)),
          );
          const existing = new Set(records.map((record) => record.id));
          records.push(
            ...items.filter(
              (item) => !existing.has(item.id) && historyIndex.has(item.id),
            ),
          );
          records.sort(
            (a, b) => b.created - a.created || b.id.localeCompare(a.id),
          );
          renderHistory();
        } catch {
          storageNotice("读取历史失败，请稍后重试。");
          load.disabled = false;
        }
      });
      actions.append(load);
    }
    const grid = document.createElement("div");
    grid.className = "history-grid";
    dateGrids.set(date, grid);
    day.append(heading, actions, grid);
    $("history").append(day);
  }
  for (const record of records) {
    const card = document.createElement("article");
    card.className = "history-card";
    card.dataset.recordId = record.id;
    card.dataset.status = record.blob ? "completed" : record.status;
    card.classList.toggle("selected", record.id === currentID);
    const open = document.createElement("button");
    open.type = "button";
    open.className = "history-open";
    open.title = `预览并复用参数（保留当前种子）：${record.params.prompt}`;
    if (record.blob) {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(record.blob);
      thumbnailURLs.push(img.src);
      img.alt = record.params.prompt;
      img.loading = "lazy";
      open.append(img);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "history-placeholder";
      placeholder.textContent =
        record.status === "failed"
          ? "生成失败"
          : activeJobs.has(record.id)
            ? "生成中 · 排队或处理中"
            : "状态未知";
      open.append(placeholder);
    }
    open.addEventListener("click", () => showRecord(record, true));
    const copySeed = document.createElement("button");
    copySeed.type = "button";
    copySeed.className = "history-copy-seed";
    copySeed.textContent = "复制种子";
    copySeed.title = "复用全部配置，包括种子";
    copySeed.addEventListener("click", () => showRecord(record, true, true));
    const selection = document.createElement("label");
    selection.className = "history-selection";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.selectId = record.id;
    checkbox.checked = selectedIDs.has(record.id);
    checkbox.setAttribute("aria-label", `选择图片：${record.params.prompt}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedIDs.add(record.id);
      else selectedIDs.delete(record.id);
      updateSelection();
    });
    selection.append(checkbox, "选择");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "history-delete";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", `删除图片：${record.params.prompt}`);
    remove.addEventListener("click", () => confirmDeletion(record.id));
    card.append(selection, open, copySeed, remove);
    dateGrids.get(dateKey(record.created)).append(card);
  }
  updateSelection();
}

const historyReady = (async () => {
  try {
    database = await openHistory();
    const page = await readHistory();
    records = page.items;
    historyBefore = page.items.at(-1);
    await readHistoryIndex();
    historyTotal = historyIndex.size;
    renderHistory();
    if (records.length) showRecord(records[0]);
  } catch {
    database = null;
    storageNotice(
      "本地历史存储不可用。本次生成仍可预览和下载，关闭页面后不会保留。",
    );
  }
})();

$("load-history").addEventListener("click", async () => {
  $("load-history").disabled = true;
  const before = historyBefore;
  try {
    const page = await readHistory(before);
    historyBefore = page.items.at(-1) ?? historyBefore;
    const existing = new Set(records.map((record) => record.id));
    records.push(...page.items.filter((record) => !existing.has(record.id)));
    historyTotal = Math.max(page.total, records.length);
    renderHistory();
  } catch {
    storageNotice("读取历史失败，请稍后重试。");
  } finally {
    $("load-history").disabled = false;
  }
});

$("generate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!$("prompt").value.trim()) {
    $("prompt").focus();
    return;
  }
  if (!optionsReady && !(await loadOptions())) return;
  let advanced;
  try {
    advanced = readOptionValues();
  } catch (error) {
    $("error").textContent = error.message;
    $("error").hidden = false;
    return;
  }
  const params = {
    prompt: $("prompt").value,
    negative_prompt: $("negative-prompt").value,
    width: Number($("width").value),
    height: Number($("height").value),
    steps: Number($("steps").value),
    cfg_scale: Number($("cfg").value),
    seed: Number($("seed").value),
    ...advanced,
  };
  await historyReady;
  const record = {
    id: crypto.randomUUID(),
    created: Date.now(),
    params,
    seed: params.seed,
    status: "pending",
  };
  const job = { record, deleted: false, headers: headers() };
  activeJobs.set(record.id, job);
  records.unshift(record);
  indexRecord(record);
  $("release-model").disabled = true;
  historyTotal += 1;
  renderHistory();
  showRecord(record);
  void runJob(job);
});

function updateElapsed() {
  const job = activeJobs.get(currentID);
  if (job && job.record.status === "pending") {
    $("elapsed").textContent =
      `已等待 ${Math.floor((Date.now() - job.record.created) / 1000)} 秒（含排队与模型加载）`;
  }
}
setInterval(updateElapsed, 1000);

async function persistRecord(record) {
  if (!database) return;
  try {
    await updateHistory(record);
  } catch {
    storageNotice(
      "本地历史保存失败（可能空间不足）。本次记录仅在当前页面保留，请及时下载图片。",
    );
  }
}

async function runJob(job) {
  const { record } = job;
  try {
    await persistRecord(record);
    if (job.deleted) return;
    const response = await api("generate", {
      method: "POST",
      headers: { ...job.headers, "Content-Type": "application/json" },
      body: JSON.stringify(record.params),
    });
    if (!response.ok) throw await responseError(response);
    if (!response.headers.get("content-type")?.startsWith("image/png")) {
      throw new Error("服务未返回 PNG 图片，请检查代理配置。");
    }
    const blob = await response.blob();
    if (job.deleted) return;
    record.blob = blob;
    record.seed = Number(response.headers.get("X-Seed") ?? record.params.seed);
    record.status = "completed";
    indexRecord(record);
  } catch (error) {
    if (job.deleted) return;
    record.status = "failed";
    record.error =
      error instanceof TypeError
        ? "连接中断或服务不可用，请检查 Tailscale 连接。请求可能仍在服务器执行，可留意右上角状态后再重试。"
        : error.message;
  } finally {
    // A deleted in-flight result must never recreate its history entry.
    if (!job.deleted) {
      await persistRecord(record);
      if (!job.deleted) {
        renderHistory();
        if (currentID === record.id) showRecord(record);
      }
    }
    activeJobs.delete(record.id);
    void pollStatus();
  }
}

function confirmDeletion(id = null) {
  deletionIDs = id === null ? null : new Set(Array.isArray(id) ? id : [id]);
  $("delete-title").textContent =
    id === null ? "清空本地历史？" : `删除所选 ${deletionIDs.size} 项？`;
  $("delete-description").textContent =
    id === null
      ? "将删除此浏览器保存的所有图片和参数。已下载的文件不受影响。"
      : `将删除此浏览器保存的所选 ${deletionIDs.size} 项记录和参数。已下载的文件不受影响。`;
  if (
    [...activeJobs.keys()].some(
      (key) => deletionIDs === null || deletionIDs.has(key),
    )
  ) {
    $("delete-description").textContent +=
      " 尚未完成的请求可能继续在服务器执行，返回的图片将被丢弃。";
  }
  $("confirm-clear").textContent = id === null ? "清空历史" : "删除图片";
  $("clear-dialog").showModal();
}

function clearPreview() {
  if (currentURL) URL.revokeObjectURL(currentURL);
  currentURL = null;
  currentID = null;
  $("preview").removeAttribute("src");
  $("preview").hidden = true;
  $("download").removeAttribute("href");
  $("download").hidden = true;
  $("empty-state").hidden = false;
  $("empty-title").textContent = "这里，是想象的起点。";
  $("empty-description").textContent = "描述一个画面，让想象成为图像。";
  $("working").hidden = true;
  $("error").hidden = true;
  $("canvas").setAttribute("aria-busy", "false");
  $("preview-meta").textContent = "等待灵感落笔";
  $("result-info").textContent = "图片只保存在当前浏览器。";
}

$("clear-history").addEventListener("click", () => confirmDeletion());
$("cancel-clear").addEventListener("click", () => $("clear-dialog").close());
$("confirm-clear").addEventListener("click", async () => {
  $("confirm-clear").disabled = true;
  const ids = deletionIDs;
  const jobs = [...activeJobs.values()].filter(
    (job) => ids === null || ids.has(job.record.id),
  );
  for (const job of jobs) job.deleted = true;
  try {
    if (database) {
      // Clear atomically; keep the in-memory list if storage fails.
      await new Promise((resolve, reject) => {
        const tx = database.transaction("images", "readwrite");
        const store = tx.objectStore("images");
        if (ids === null) store.clear();
        else for (const id of ids) store.delete(id);
        tx.oncomplete = resolve;
        tx.onabort = () => reject(tx.error);
        tx.onerror = () => reject(tx.error);
      });
    }
    records =
      ids === null ? [] : records.filter((record) => !ids.has(record.id));
    if (ids === null) {
      historyIndex.clear();
      selectedIDs.clear();
    } else
      for (const id of ids) {
        historyIndex.delete(id);
        selectedIDs.delete(id);
      }
    historyTotal = historyIndex.size;
    renderHistory();
    if (ids === null || ids.has(currentID)) clearPreview();
  } catch {
    for (const job of jobs) job.deleted = false;
    storageNotice("无法删除本地历史，请检查浏览器存储权限后重试。");
  } finally {
    $("confirm-clear").disabled = false;
    $("clear-dialog").close();
  }
});

function dateKey(created) {
  const date = new Date(created);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function indexRecord(record) {
  historyIndex.set(record.id, {
    id: record.id,
    created: record.created,
    hasBlob: Boolean(record.blob),
  });
}

// Keep metadata for every date without retaining all PNG blobs in memory.
function readHistoryIndex() {
  return new Promise((resolve, reject) => {
    const tx = database.transaction("images", "readonly");
    const request = tx.objectStore("images").openCursor();
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor) {
        indexRecord(cursor.value);
        cursor.continue();
      }
    };
    tx.oncomplete = resolve;
    tx.onabort = tx.onerror = () => reject(tx.error);
  });
}

async function getHistoryRecords(ids) {
  const loaded = new Map(records.map((record) => [record.id, record]));
  const missing = ids.filter((id) => !loaded.has(id));
  if (database && missing.length) {
    await new Promise((resolve, reject) => {
      const tx = database.transaction("images", "readonly");
      const store = tx.objectStore("images");
      for (const id of missing) {
        const request = store.get(id);
        request.onsuccess = () => {
          if (request.result) loaded.set(id, request.result);
        };
      }
      tx.oncomplete = resolve;
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  }
  return ids.map((id) => loaded.get(id)).filter(Boolean);
}

function updateSelection() {
  $("selection-count").textContent = `已选 ${selectedIDs.size} 项`;
  $("unselect-history").disabled = selectedIDs.size === 0;
  $("delete-selected").disabled = selectedIDs.size === 0;
  $("select-history").disabled = historyIndex.size === 0;
  $("download-selected").disabled = ![...selectedIDs].some(
    (id) => historyIndex.get(id)?.hasBlob,
  );
  for (const checkbox of $("history").querySelectorAll("[data-select-id]")) {
    checkbox.checked = selectedIDs.has(checkbox.dataset.selectId);
    checkbox
      .closest(".history-card")
      .classList.toggle("batch-selected", checkbox.checked);
  }
  for (const checkbox of $("history").querySelectorAll("[data-select-date]")) {
    const ids = [...historyIndex.values()]
      .filter((item) => dateKey(item.created) === checkbox.dataset.selectDate)
      .map((item) => item.id);
    const count = ids.filter((id) => selectedIDs.has(id)).length;
    checkbox.checked = count === ids.length;
    checkbox.indeterminate = count > 0 && count < ids.length;
  }
}

$("select-history").addEventListener("click", () => {
  for (const id of historyIndex.keys()) selectedIDs.add(id);
  updateSelection();
});
$("unselect-history").addEventListener("click", () => {
  selectedIDs.clear();
  updateSelection();
});
$("delete-selected").addEventListener("click", () =>
  confirmDeletion([...selectedIDs]),
);
$("download-selected").addEventListener("click", async () => {
  const button = $("download-selected");
  button.disabled = true;
  button.textContent = "正在打包…";
  let url;
  try {
    const items = (await getHistoryRecords([...selectedIDs])).filter(
      (item) => item.blob,
    );
    if (!items.length) return;
    const zip = await createImageZip(
      items.map((item) => ({
        name: `diffusion-${item.seed}-${item.created}-${item.id}.png`,
        blob: item.blob,
        created: item.created,
      })),
    );
    url = URL.createObjectURL(zip);
    const link = document.createElement("a");
    link.href = url;
    link.download = `diffusion-${dateKey(Date.now())}.zip`;
    document.body.append(link);
    link.click();
    link.remove();
  } catch (error) {
    storageNotice(`批量下载失败：${error.message}`);
  } finally {
    // Give the browser time to start reading the download before releasing it.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 60000);
    button.textContent = "下载所选 ZIP";
    updateSelection();
  }
});
