"use strict";

const $ = (id) => document.getElementById(id);
const settings = JSON.parse($("ui-settings").textContent);
const defaultPrompt = $("prompt").defaultValue;
const historyPageSize = 24;
let records = [];
let historyTotal = 0;
let database = null;
let currentURL = null;
let currentID = null;
let deletionID = null;
let thumbnailURLs = [];
let generating = false;
let polling = false;
let optionDefinitions = {};
let optionDefaults = {};
let optionsReady = false;
let optionsPending = null;
let restoreOptions = null;

$("idle-note").textContent =
  `空闲 ${settings.idle_seconds} 秒自动释放模型。下次生成时重新加载。`;
if (settings.auth_required) {
  $("auth-details").open = true;
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
  } catch (error) {
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

function setOptionValue(name, value) {
  const input = document.getElementById(`option-${name}`);
  if (!input) return;
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
  if (optionsPending) return optionsPending;
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
      for (const [name, definition] of Object.entries(optionDefinitions)) {
        if (!definition.group) continue;
        let group = groups.get(definition.group);
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
        input.addEventListener("invalid", () => {
          group.open = true;
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
        if (definition.description) {
          const note = document.createElement("p");
          note.className = "option-note";
          note.textContent = definition.description;
          group.append(note);
        }
        setOptionValue(name, restoreOptions?.[name] ?? optionDefaults[name]);
      }
      optionsReady = true;
      restoreOptions = null;
      $("options-state").textContent =
        "所有参数随本次请求提交，并保存到历史记录。";
      $("retry-options").hidden = true;
      return true;
    } catch (error) {
      $("options-state").textContent = error.message;
      $("retry-options").hidden = false;
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
      input.closest("details").open = true;
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

function showRecord(record, restore = false) {
  currentID = record.id;
  if (currentURL) URL.revokeObjectURL(currentURL);
  currentURL = URL.createObjectURL(record.blob);
  $("preview").src = currentURL;
  $("preview").alt = record.params.prompt;
  $("preview").hidden = false;
  $("empty-state").hidden = true;
  $("download").href = currentURL;
  $("download").download = `diffusion-${record.seed}-${record.created}.png`;
  $("download").hidden = false;
  $("preview-meta").textContent =
    `${record.params.width} × ${record.params.height} · ${record.params.steps} 步`;
  $("result-info").textContent =
    `Seed ${record.seed} · ${new Date(record.created).toLocaleString("zh-CN")}`;
  if (restore) {
    $("prompt").value = record.params.prompt;
    $("negative-prompt").value = record.params.negative_prompt;
    $("width").value = record.params.width;
    $("height").value = record.params.height;
    syncSizePreset();
    $("steps").value = record.params.steps;
    $("cfg").value = record.params.cfg_scale;
    $("seed").value = record.seed;
    restoreOptions = record.params;
    if (optionsReady) {
      for (const [name, definition] of Object.entries(optionDefinitions)) {
        if (definition.group)
          setOptionValue(name, record.params[name] ?? optionDefaults[name]);
      }
    }
  }
}

function renderHistory() {
  thumbnailURLs.forEach((url) => URL.revokeObjectURL(url));
  thumbnailURLs = [];
  $("history").replaceChildren();
  $("history-count").textContent = historyTotal;
  $("history-empty").hidden = records.length > 0;
  $("clear-history").disabled = historyTotal === 0;
  $("load-history").hidden = !database || records.length >= historyTotal;
  for (const record of records) {
    const card = document.createElement("article");
    card.className = "history-card";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "history-open";
    open.title = `预览并复用参数：${record.params.prompt}`;
    const img = document.createElement("img");
    img.src = URL.createObjectURL(record.blob);
    thumbnailURLs.push(img.src);
    img.alt = record.params.prompt;
    img.loading = "lazy";
    const caption = document.createElement("span");
    caption.textContent = record.params.prompt;
    open.append(img, caption);
    open.addEventListener("click", () => showRecord(record, true));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "history-delete";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", `删除图片：${record.params.prompt}`);
    remove.addEventListener("click", () => confirmDeletion(record.id));
    card.append(open, remove);
    $("history").append(card);
  }
}

const historyReady = (async () => {
  try {
    database = await openHistory();
    const page = await readHistory();
    records = page.items;
    historyTotal = page.total;
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
  const before = records.at(-1);
  try {
    const page = await readHistory(before);
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

$("example").addEventListener("click", () => {
  $("prompt").value =
    `${defaultPrompt}, an adult woman wearing a blue coat in a snowy garden, soft winter sunlight, falling snow`;
  $("prompt").focus();
});

$("generate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (generating) return;
  if (!$("prompt").value.trim()) {
    $("prompt").focus();
    return;
  }
  if (!optionsReady && !(await loadOptions())) return;
  if (generating) return;
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
  generating = true;
  $("generate").disabled = true;
  $("generate-label").textContent = "生成中…";
  $("canvas").setAttribute("aria-busy", "true");
  $("working").hidden = false;
  $("error").hidden = true;
  const started = Date.now();
  const tick = () => {
    $("elapsed").textContent =
      `已等待 ${Math.floor((Date.now() - started) / 1000)} 秒`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const response = await api("generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    if (!response.ok) throw await responseError(response);
    if (!response.headers.get("content-type")?.startsWith("image/png")) {
      throw new Error("服务未返回 PNG 图片，请检查代理配置。");
    }
    const record = {
      id: crypto.randomUUID(),
      created: Date.now(),
      params,
      seed: Number(response.headers.get("X-Seed") ?? params.seed),
      blob: await response.blob(),
    };
    await historyReady;
    showRecord(record);
    records.unshift(record);
    historyTotal += 1;
    renderHistory();
    if (database) {
      try {
        historyTotal = Math.max(await updateHistory(record), records.length);
        renderHistory();
      } catch {
        storageNotice(
          "图片已生成，但本地历史保存失败（可能空间不足）。请先下载图片；本次结果仅在当前页面保留。",
        );
      }
    }
  } catch (error) {
    $("error").textContent =
      error instanceof TypeError
        ? "连接中断或服务不可用，请检查 Tailscale 连接。请求可能仍在服务器执行，可留意右上角状态后再重试。"
        : error.message;
    $("error").hidden = false;
    if ($("error").textContent.includes("API key"))
      $("auth-details").open = true;
  } finally {
    clearInterval(timer);
    generating = false;
    $("generate").disabled = false;
    $("generate-label").textContent = "生成图片";
    $("canvas").setAttribute("aria-busy", "false");
    $("working").hidden = true;
    void pollStatus();
  }
});

function confirmDeletion(id = null) {
  deletionID = id;
  $("delete-title").textContent =
    id === null ? "清空本地历史？" : "删除这张图片？";
  $("delete-description").textContent =
    id === null
      ? "将删除此浏览器保存的所有图片和参数。已下载的文件不受影响。"
      : "将删除此浏览器保存的这张图片和参数。已下载的文件不受影响。";
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
  $("preview-meta").textContent = "等待灵感落笔";
  $("result-info").textContent = "图片只保存在当前浏览器。";
}

$("clear-history").addEventListener("click", () => confirmDeletion());
$("cancel-clear").addEventListener("click", () => $("clear-dialog").close());
$("confirm-clear").addEventListener("click", async () => {
  $("confirm-clear").disabled = true;
  const id = deletionID;
  try {
    if (database) {
      // Clear atomically; keep the in-memory list if storage fails.
      await new Promise((resolve, reject) => {
        const tx = database.transaction("images", "readwrite");
        const store = tx.objectStore("images");
        if (id === null) store.clear();
        else store.delete(id);
        tx.oncomplete = resolve;
        tx.onabort = () => reject(tx.error);
        tx.onerror = () => reject(tx.error);
      });
    }
    records = id === null ? [] : records.filter((record) => record.id !== id);
    historyTotal = id === null ? 0 : Math.max(0, historyTotal - 1);
    renderHistory();
    if (id === null || currentID === id) clearPreview();
  } catch {
    storageNotice("无法删除本地历史，请检查浏览器存储权限后重试。");
  } finally {
    $("confirm-clear").disabled = false;
    $("clear-dialog").close();
  }
});
