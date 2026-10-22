"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// змінний стан зібрано в одному об’єкті, щоб обробники не зберігали розрізнені копії даних
const state = {
  token: localStorage.getItem("nexus_token") || "",
  me: null,
  nodeId: "",
  users: [],
  channels: [],
  activeChannel: null,
  channelKeys: new Map(),
  device: null,
  documents: [],
  messages: new Set(),
  selectedRecipients: new Set(),
  socket: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  activeDocument: null,
  documentUrl: null,
  page: 1,
  presenting: false,
  presentationSessionId: null,
  presentationRecipients: [],
  presentationSequence: 0,
  presentationHeartbeatTimer: null,
  lastPresentationCursor: null,
  lastPresentationPointer: null,
  documentZoom: 1,
  remotePresentation: null,
  remotePresentationTimer: null,
  documentLoadId: 0,
  audio: null,
  audioSequence: 0,
  playbackContext: null,
  playbackTimes: new Map(),
  keyRequestInFlight: false,
  peoplePanelOpen: !window.matchMedia("(max-width: 1020px)").matches,
  clientOutboxFlushing: false,
  receiptStates: new Map(),
  receiptInFlight: new Map(),
  messageReceipts: new Map(),
};

function initials(name = "?") {
  return name.trim().split(/\s+/).slice(0, 2).map(part => part[0]?.toUpperCase()).join("") || "?";
}

function formatTime(timestamp) {
  return new Intl.DateTimeFormat("uk-UA", { hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp * 1000));
}

function localDateKey(timestamp) {
  const date = new Date(timestamp * 1000);
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

function formatCalendarDate(timestamp) {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (localDateKey(timestamp) === localDateKey(today.getTime() / 1000)) return "Сьогодні";
  if (localDateKey(timestamp) === localDateKey(yesterday.getTime() / 1000)) return "Вчора";
  return new Intl.DateTimeFormat("uk-UA", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  }).format(date);
}

function formatShortDate(timestamp) {
  return new Intl.DateTimeFormat("uk-UA", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(timestamp * 1000));
}

function formatBytes(value) {
  if (value < 1024) return `${value} Б`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} КБ`;
  return `${(value / 1024 ** 2).toFixed(1)} МБ`;
}

function viewerCountLabel(count) {
  const lastTwo = count % 100;
  const last = count % 10;
  const noun = last === 1 && lastTwo !== 11
    ? "глядач"
    : last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)
      ? "глядачі"
      : "глядачів";
  return `${count} ${noun}`;
}

const diagnosticDefinitions = {
  node: {
    summary: "Унікальне ім’я цього серверного вузла Nexus.",
    purpose: "Допомагає відрізняти сервери розподіленої системи та бачити, на якому з них відкрито поточну сторінку.",
    hint: "У демонстрації node-a і node-b мають різні імена, порти та бази даних.",
  },
  process: {
    summary: "Ідентифікатор процесу Python, призначений операційною системою.",
    purpose: "Дозволяє підтвердити, що вузли справді працюють як окремі процеси, а також помітити перезапуск сервера.",
    hint: "Після перезапуску вузла PID зазвичай змінюється.",
  },
  threads: {
    summary: "Загальна кількість потоків, які зараз існують у процесі вузла.",
    purpose: "Показує багатопотокову роботу сервера: головний потік, службові потоки та працівники для блокувальних операцій.",
    hint: "Кількість може змінюватися під навантаженням — це нормальна поведінка.",
  },
  workers: {
    summary: "Максимальна кількість потоків у робочому пулі Nexus.",
    purpose: "Пул виконує операції з SQLite, хешуванням і файлами поза асинхронним циклом, щоб один повільний запит не зупиняв інших користувачів.",
    hint: "Це ліміт паралельних працівників, а не кількість активних користувачів.",
  },
  lamport: {
    summary: "Логічний годинник для впорядкування подій між незалежними вузлами.",
    purpose: "Кожна локальна або отримана подія збільшує лічильник. Це допомагає встановити причинний порядок без спільного фізичного годинника.",
    hint: "Це не дата й не кількість повідомлень; велике значення не означає помилку.",
  },
  peers: {
    summary: "Кількість інших серверних вузлів, з якими налаштована федерація.",
    purpose: "Peer-вузли обмінюються користувачами, каналами, повідомленнями та документами, утворюючи розподілену систему.",
    hint: "Для демонстрації з двома вузлами тут зазвичай відображається 1.",
  },
  messages: {
    summary: "Кількість повідомлень, збережених у локальній базі цього вузла.",
    purpose: "Показує обсяг локальної репліки чату. Після синхронізації значення на вузлах мають зближуватися.",
    hint: "Повідомлення приватних E2EE-каналів зберігаються у вигляді шифротексту.",
  },
  outbox: {
    summary: "Стійка черга подій, які ще не доставлено peer-вузлам.",
    purpose: "Якщо мережа або інший ноутбук недоступні, Nexus не втрачає подію, а повторює доставку після відновлення зв’язку.",
    hint: "0 означає, що черга порожня. Ненульове значення під час відключеного peer-вузла є очікуваним.",
  },
  websocket: {
    summary: "Кількість активних з’єднань реального часу з браузерами на цьому вузлі.",
    purpose: "WebSocket доставляє нові повідомлення, присутність, аудіо та стан презентації без постійного оновлення сторінки.",
    hint: "Одна відкрита вкладка Nexus зазвичай створює одне з’єднання.",
  },
  audio: {
    summary: "Кількість аудіофрагментів, оброблених вузлом від моменту запуску.",
    purpose: "Дає змогу побачити потокову передачу звуку як послідовність малих подій у реальному часі.",
    hint: "Аудіофрагменти ефемерні: вони не записуються в БД і не потрапляють до outbox.",
  },
  channels: {
    summary: "Кількість каналів у локальній базі вузла.",
    purpose: "Канали ізолюють учасників, повідомлення, ключі та документи в окремі робочі простори.",
    hint: "Загальний канал також входить до цього числа.",
  },
  devices: {
    summary: "Кількість зареєстрованих браузерних пристроїв із публічними E2EE-ключами.",
    purpose: "Окрема пара ключів кожного браузера дає змогу безпечно передати йому ключ приватного каналу.",
    hint: "Повторний вхід з іншим профілем браузера створює інший пристрій.",
  },
  fingerprint: {
    summary: "Короткий відбиток публічного ключа саме цього браузерного пристрою.",
    purpose: "Користувачі можуть порівняти відбитки через інший надійний канал і переконатися, що ключ не було підмінено.",
    hint: "Відбиток не розкриває приватний ключ і безпечно може бути показаний іншому учаснику.",
  },
};

function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = message;
  $("#toast-region").append(item);
  setTimeout(() => item.remove(), 4200);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(payload?.detail || payload || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

const CLIENT_DATABASE_NAME = "nexus-client";
const CLIENT_DATABASE_VERSION = 1;

// локальний outbox зберігає повідомлення до мережевої спроби, тому воно не зникне після втрати wi-fi або перезавантаження вкладки
function openClientDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(CLIENT_DATABASE_NAME, CLIENT_DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains("message_outbox")) {
        const store = database.createObjectStore("message_outbox", { keyPath: "id" });
        store.createIndex("username", "username", { unique: false });
        store.createIndex("channel_id", "channel_id", { unique: false });
        store.createIndex("created_at", "created_at", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Не вдалося відкрити локальну чергу"));
  });
}

async function withClientOutbox(mode, operation) {
  const database = await openClientDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction("message_outbox", mode);
    const store = transaction.objectStore("message_outbox");
    let result;
    try {
      result = operation(store);
    } catch (error) {
      database.close();
      reject(error);
      return;
    }
    transaction.oncomplete = () => { database.close(); resolve(result?.result); };
    transaction.onerror = () => { database.close(); reject(transaction.error || new Error("Помилка локальної черги")); };
    transaction.onabort = () => { database.close(); reject(transaction.error || new Error("Операцію локальної черги скасовано")); };
  });
}

function putClientOutbox(record) {
  return withClientOutbox("readwrite", store => store.put(record));
}

function deleteClientOutbox(messageId) {
  return withClientOutbox("readwrite", store => store.delete(messageId));
}

async function listClientOutbox(username = state.me?.username) {
  if (!username) return [];
  const records = await withClientOutbox("readonly", store => store.index("username").getAll(username));
  return (records || []).sort((first, second) => first.created_at - second.created_at || first.id.localeCompare(second.id));
}

async function refreshClientOutboxStatus() {
  const indicator = $("#client-outbox-status");
  if (!indicator || !state.me) return;
  try {
    const records = await listClientOutbox();
    const failed = records.filter(record => record.failed).length;
    indicator.classList.toggle("hidden", records.length === 0);
    indicator.classList.toggle("error", failed > 0);
    indicator.textContent = failed ? `Черга: ${records.length} · помилок: ${failed}` : `Черга браузера: ${records.length}`;
    indicator.title = failed
      ? "Є повідомлення, відхилені сервером"
      : "Повідомлення збережені у браузері та будуть надіслані після відновлення зв’язку";
  } catch (error) {
    console.warn("Cannot inspect browser outbox", error);
  }
}

function queuedRecordAsMessage(record) {
  return {
    id: record.id,
    room_id: record.channel_id,
    author_id: state.me.id,
    author_name: state.me.display_name,
    body: record.payload.body || "",
    encrypted: record.payload.encrypted ? 1 : 0,
    ciphertext: record.payload.ciphertext || null,
    nonce: record.payload.nonce || null,
    created_at: record.created_at,
    lamport: 0,
    origin_node: "цей пристрій",
    pending: true,
    failed: Boolean(record.failed),
  };
}

function setPendingMessageState(messageId, status, text) {
  const article = $(`.message[data-message-id="${CSS.escape(messageId)}"]`);
  if (!article) return;
  article.classList.toggle("pending", status === "pending");
  article.classList.toggle("failed", status === "failed");
  const delivery = $(".message-delivery", article);
  if (delivery) {
    delivery.className = `message-delivery ${status}`;
    delivery.textContent = text;
  }
}

async function renderPendingMessages() {
  if (!state.activeChannel || !state.me) return;
  const records = await listClientOutbox();
  for (const record of records.filter(item => item.channel_id === state.activeChannel.id)) {
    if (state.messages.has(record.id)) {
      await deleteClientOutbox(record.id);
      continue;
    }
    await addMessage(queuedRecordAsMessage(record));
  }
  await refreshClientOutboxStatus();
}

async function acknowledgeClientMessage(messageId) {
  try { await deleteClientOutbox(messageId); } catch (error) { console.warn("Cannot clear browser outbox", error); }
  setPendingMessageState(messageId, "sent", "✓ Надіслано");
  if (state.messageReceipts.has(messageId)) {
    updateMessageDelivery(messageId, state.messageReceipts.get(messageId));
  }
  await refreshClientOutboxStatus();
}

async function rejectClientMessage(messageId, reason) {
  const records = await listClientOutbox();
  const record = records.find(item => item.id === messageId);
  if (record) await putClientOutbox({ ...record, failed: true, last_error: reason });
  setPendingMessageState(messageId, "failed", "Не надіслано");
  await refreshClientOutboxStatus();
  toast(reason, "error");
}

async function flushClientOutbox() {
  if (state.clientOutboxFlushing || state.socket?.readyState !== WebSocket.OPEN || !state.me) return;
  state.clientOutboxFlushing = true;
  try {
    const records = await listClientOutbox();
    for (const record of records) {
      if (record.failed || state.socket?.readyState !== WebSocket.OPEN) continue;
      const updated = { ...record, attempts: (record.attempts || 0) + 1, last_attempt_at: Date.now() };
      await putClientOutbox(updated);
      state.socket.send(JSON.stringify(record.payload));
      setPendingMessageState(record.id, "pending", "Надсилається…");
    }
  } catch (error) {
    console.warn("Cannot flush browser outbox", error);
  } finally {
    state.clientOutboxFlushing = false;
    await refreshClientOutboxStatus();
  }
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let index = 0; index < bytes.length; index += 8192) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  }
  return btoa(binary);
}

function base64ToBytes(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function openCryptoDatabase() {
  // приватна частина ключа і ключі каналів залишаються в indexeddb цього браузера
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("nexus-e2ee", 1);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains("identity")) database.createObjectStore("identity");
      if (!database.objectStoreNames.contains("channelKeys")) database.createObjectStore("channelKeys");
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function cryptoStoreGet(storeName, key) {
  const database = await openCryptoDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(storeName, "readonly");
    const request = transaction.objectStore(storeName).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    transaction.oncomplete = () => database.close();
  });
}

async function cryptoStorePut(storeName, key, value) {
  const database = await openCryptoDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(storeName, "readwrite");
    transaction.objectStore(storeName).put(value, key);
    transaction.oncomplete = () => { database.close(); resolve(); };
    transaction.onerror = () => reject(transaction.error);
  });
}

async function cryptoStoreDelete(storeName, key) {
  const database = await openCryptoDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(storeName, "readwrite");
    transaction.objectStore(storeName).delete(key);
    transaction.oncomplete = () => { database.close(); resolve(); };
    transaction.onerror = () => reject(transaction.error);
  });
}

async function deviceFingerprint(publicKey) {
  const canonical = JSON.stringify({ crv: publicKey.crv, kty: publicKey.kty, x: publicKey.x, y: publicKey.y });
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical)));
  return [...digest.slice(0, 8)].map(value => value.toString(16).padStart(2, "0")).join("").match(/.{1,4}/g).join(" ");
}

async function ensureDeviceIdentity() {
  // кожен профіль браузера має власну пару ecdh-ключів і сталий ідентифікатор пристрою
  if (!window.crypto?.subtle || !window.indexedDB) throw new Error("Браузер не підтримує Web Crypto/IndexedDB");
  let deviceId = localStorage.getItem("nexus_device_id");
  if (!deviceId) {
    deviceId = crypto.randomUUID();
    localStorage.setItem("nexus_device_id", deviceId);
  }
  let identity = await cryptoStoreGet("identity", deviceId);
  if (!identity) {
    const generated = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
    const publicKey = await crypto.subtle.exportKey("jwk", generated.publicKey);
    const privateJwk = await crypto.subtle.exportKey("jwk", generated.privateKey);
    const privateKey = await crypto.subtle.importKey(
      "jwk", privateJwk, { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]
    );
    identity = { deviceId, publicKey, privateKey };
    await cryptoStorePut("identity", deviceId, identity);
  }
  identity.fingerprint = await deviceFingerprint(identity.publicKey);
  state.device = identity;
  const known = await api(`/api/keys/devices?usernames=${encodeURIComponent(state.me.username)}`);
  const existing = known.devices.find(item => item.device_id === deviceId);
  if (
    !existing
    || existing.public_key.x !== identity.publicKey.x
    || existing.public_key.y !== identity.publicKey.y
  ) {
    await api("/api/keys/devices", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, public_key: identity.publicKey }),
    });
  }
  return identity;
}

async function deriveWrappingKey(otherPublicKey, channelId) {
  // ecdh створює спільний секрет, а hkdf перетворює його на окремий aes-ключ для конкретного каналу
  const importedPublic = await crypto.subtle.importKey(
    "jwk", otherPublicKey, { name: "ECDH", namedCurve: "P-256" }, false, []
  );
  const sharedSecret = await crypto.subtle.deriveBits(
    { name: "ECDH", public: importedPublic }, state.device.privateKey, 256
  );
  const keyMaterial = await crypto.subtle.importKey("raw", sharedSecret, "HKDF", false, ["deriveKey"]);
  const salt = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`nexus:${channelId}`));
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt, info: new TextEncoder().encode("nexus-channel-key-v1") },
    keyMaterial,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

async function storeChannelKey(channelId, rawKey) {
  const key = await crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, true, ["encrypt", "decrypt"]);
  state.channelKeys.set(channelId, key);
  await cryptoStorePut("channelKeys", channelId, key);
  return key;
}

async function wrapChannelKey(channelId, rawKey, recipientDevice) {
  // симетричний ключ каналу загортається окремо для кожного пристрою, а сервер зберігає лише загорнуту версію
  const wrappingKey = await deriveWrappingKey(recipientDevice.public_key, channelId);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const wrapped = await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, wrappingKey, rawKey);
  return {
    recipient_device_id: recipientDevice.device_id,
    recipient_username: recipientDevice.username,
    sender_device_id: state.device.deviceId,
    sender_public_key: state.device.publicKey,
    wrapped_key: bytesToBase64(new Uint8Array(wrapped)),
    nonce: bytesToBase64(nonce),
  };
}

async function loadChannelKey(channelId, forceServer = false) {
  if (!forceServer && state.channelKeys.has(channelId)) return state.channelKeys.get(channelId);
  if (!forceServer) {
    const stored = await cryptoStoreGet("channelKeys", channelId);
    if (stored) {
      state.channelKeys.set(channelId, stored);
      return stored;
    }
  }
  if (!state.device) return null;
  try {
    const result = await api(
      `/api/channels/${encodeURIComponent(channelId)}/key?device_id=${encodeURIComponent(state.device.deviceId)}`
    );
    const envelope = result.key_envelope;
    const wrappingKey = await deriveWrappingKey(envelope.sender_public_key, channelId);
    const rawKey = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: base64ToBytes(envelope.nonce) },
      wrappingKey,
      base64ToBytes(envelope.wrapped_key),
    );
    return await storeChannelKey(channelId, rawKey);
  } catch (error) {
    if (error.status !== 404) console.warn("Cannot load channel key", error);
    return null;
  }
}

async function encryptChannelMessage(channelId, text) {
  // aes-gcm одночасно приховує текст і дає змогу виявити будь-яку зміну шифротексту
  const key = await loadChannelKey(channelId);
  if (!key) throw new Error("Ключ каналу ще не отримано");
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const additionalData = new TextEncoder().encode(`nexus:${channelId}:v1`);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData }, key, new TextEncoder().encode(text)
  );
  return { ciphertext: bytesToBase64(new Uint8Array(ciphertext)), nonce: bytesToBase64(nonce) };
}

async function decryptChannelMessage(message) {
  const key = await loadChannelKey(message.room_id);
  if (!key) throw new Error("Немає ключа цього пристрою");
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: base64ToBytes(message.nonce),
      additionalData: new TextEncoder().encode(`nexus:${message.room_id}:v1`),
    },
    key,
    base64ToBytes(message.ciphertext),
  );
  return new TextDecoder().decode(plaintext);
}

function showAuth() {
  $("#auth-screen").classList.remove("hidden");
  $("#app").classList.add("hidden");
}

function showApp() {
  $("#auth-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#profile-name").textContent = state.me.display_name;
  $("#profile-node").textContent = `@${state.me.username} · ${state.nodeId}`;
  $("#profile-avatar").textContent = initials(state.me.display_name);
}

let registerMode = false;
function updateAuthMode() {
  $("#display-name-field").classList.toggle("hidden", !registerMode);
  $("#display-name").required = registerMode;
  $("#auth-title").textContent = registerMode ? "Створіть простір для себе" : "З поверненням";
  $("#auth-subtitle").textContent = registerMode ? "Один обліковий запис працюватиме на всіх вузлах" : "Увійдіть до свого робочого простору";
  $("#auth-submit").textContent = registerMode ? "Створити обліковий запис" : "Увійти";
  $("#auth-switch").textContent = registerMode ? "Уже маєте обліковий запис? Увійти" : "Ще немає облікового запису? Створити";
  $("#password").autocomplete = registerMode ? "new-password" : "current-password";
  $("#auth-error").textContent = "";
}

async function handleAuth(event) {
  event.preventDefault();
  const button = $("#auth-submit");
  button.disabled = true;
  $("#auth-error").textContent = "";
  try {
    const payload = await api(registerMode ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#username").value.trim(),
        password: $("#password").value,
        display_name: registerMode ? $("#display-name").value.trim() : undefined,
      }),
    });
    state.token = payload.token;
    state.me = payload.user;
    state.nodeId = payload.node_id;
    localStorage.setItem("nexus_token", state.token);
    await enterApplication();
  } catch (error) {
    $("#auth-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function restoreSession() {
  if (!state.token) return showAuth();
  try {
    const result = await api("/api/me");
    state.me = result.user;
    state.nodeId = result.node_id;
    await enterApplication();
  } catch {
    localStorage.removeItem("nexus_token");
    state.token = "";
    showAuth();
  }
}

async function enterApplication() {
  showApp();
  await loadUsers();
  try {
    await ensureDeviceIdentity();
  } catch (error) {
    toast(`E2EE недоступне: ${error.message}`, "error");
  }
  await loadChannels();
  await selectChannel(state.activeChannel?.id || "lobby");
  connectSocket();
  setTimeout(() => $("#message-input").focus(), 50);
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* session may already be gone */ }
  if (state.presenting) stopPresentation();
  state.socket?.close();
  stopAudio();
  localStorage.removeItem("nexus_token");
  location.reload();
}

function socketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws`;
}

function updateConnection(status) {
  const dot = $("#connection-dot");
  dot.className = `status-dot ${status === "online" ? "" : status}`;
  dot.title = status === "online" ? "Підключено" : status === "connecting" ? "Підключення…" : "Немає з’єднання";
  dot.setAttribute("aria-label", dot.title);
}

function connectSocket() {
  // після розриву затримка повтору поступово зростає, а після відновлення з’єднання outbox надсилається автоматично
  clearTimeout(state.reconnectTimer);
  updateConnection("connecting");
  const socket = new WebSocket(socketUrl(), ["nexus", `auth.${state.token}`]);
  state.socket = socket;
  socket.addEventListener("open", () => {
    state.reconnectAttempts = 0;
    updateConnection("online");
    requestMissingChannelKeys();
    if (state.presenting) sendPresentationEvent("start", state.lastPresentationCursor);
    Promise.resolve((async () => {
      if (state.activeChannel) await loadMessages();
      await flushClientOutbox();
    })()).catch(error => console.error(error));
  });
  socket.addEventListener("message", event => {
    try {
      Promise.resolve(handleSocketEvent(JSON.parse(event.data))).catch(error => console.error(error));
    } catch (error) { console.error(error); }
  });
  socket.addEventListener("close", event => {
    updateConnection("offline");
    state.receiptInFlight.clear();
    if (event.code === 4401) return logout();
    const delay = Math.min(12000, 700 * (2 ** state.reconnectAttempts++));
    state.reconnectTimer = setTimeout(connectSocket, delay);
  });
}

const receiptRanks = { delivered: 1, read: 2 };

// ранг не дозволяє пізнішому стану «прочитано» відкотитися назад до «доставлено»
function sendMessageReceipt(messageId, authorId, status) {
  if (!messageId || !authorId || !state.device?.deviceId || authorId === state.me?.id || !receiptRanks[status]) return false;
  if (state.socket?.readyState !== WebSocket.OPEN) return false;
  const requestedRank = receiptRanks[status];
  if ((state.receiptStates.get(messageId) || 0) >= requestedRank) return false;
  if ((state.receiptInFlight.get(messageId) || 0) >= requestedRank) return false;
  state.receiptInFlight.set(messageId, requestedRank);
  state.socket.send(JSON.stringify({
    type: "message_receipt",
    message_id: messageId,
    device_id: state.device.deviceId,
    status,
  }));
  return true;
}

function confirmMessageReceipt(messageId, status) {
  const rank = receiptRanks[status] || 0;
  state.receiptStates.set(messageId, Math.max(state.receiptStates.get(messageId) || 0, rank));
  if ((state.receiptInFlight.get(messageId) || 0) <= rank) state.receiptInFlight.delete(messageId);
}

function deliveryPresentation(summary = {}) {
  const readCount = Number(summary.read_count || 0);
  const deliveredCount = Number(summary.delivered_count || 0);
  const readUsers = Array.isArray(summary.read_users) ? summary.read_users.join(", ") : "";
  const deliveredUsers = Array.isArray(summary.delivered_users) ? summary.delivered_users.join(", ") : "";
  if (readCount > 0) {
    return {
      status: "read",
      text: `✓✓ Прочитано${readCount > 1 ? ` · ${readCount}` : ""}`,
      title: `Прочитали: ${readUsers || readCount} · доставлено на пристроїв: ${deliveredCount}`,
    };
  }
  if (deliveredCount > 0) {
    return {
      status: "delivered",
      text: `✓✓ Доставлено${deliveredCount > 1 ? ` · ${deliveredCount}` : ""}`,
      title: `Доставлено: ${deliveredUsers || deliveredCount} · пристроїв: ${deliveredCount}`,
    };
  }
  return { status: "sent", text: "✓ Надіслано", title: "Повідомлення збережене сервером" };
}

function updateMessageDelivery(messageId, summary) {
  state.messageReceipts.set(messageId, summary || {});
  const delivery = $(`.message[data-message-id="${CSS.escape(messageId)}"] .message-delivery`);
  if (!delivery) return;
  const presentation = deliveryPresentation(summary);
  delivery.className = `message-delivery ${presentation.status}`;
  delivery.textContent = presentation.text;
  delivery.title = presentation.title;
}

function markActiveChannelRead() {
  if (document.visibilityState !== "visible" || !state.activeChannel) return;
  for (const article of $$("#message-list .message")) {
    sendMessageReceipt(article.dataset.messageId, article.dataset.authorId, "read");
  }
}

function sendSocket(payload) {
  if (state.socket?.readyState !== WebSocket.OPEN) {
    toast("З’єднання із сервером відновлюється", "error");
    return false;
  }
  state.socket.send(JSON.stringify(payload));
  return true;
}

async function receiveServerMessage(message) {
  const ownMessage = message.author_id === state.me?.id;
  if (ownMessage) await acknowledgeClientMessage(message.id);
  if (!ownMessage) sendMessageReceipt(message.id, message.author_id, "delivered");
  if (message.room_id !== state.activeChannel?.id) return;
  const existing = $(`.message[data-message-id="${CSS.escape(message.id)}"]`);
  if (existing && ownMessage) {
    existing.remove();
    state.messages.delete(message.id);
  }
  await addMessage(message);
  if (!ownMessage && document.visibilityState === "visible") {
    sendMessageReceipt(message.id, message.author_id, "read");
  }
}

async function handleSocketEvent(event) {
  switch (event.type) {
    case "hello":
      state.nodeId = event.node_id;
      $("#profile-node").textContent = `@${state.me.username} · ${state.nodeId}`;
      break;
    case "message":
      await receiveServerMessage(event.message);
      break;
    case "message_ack":
      await acknowledgeClientMessage(event.client_id);
      break;
    case "message_receipt":
      updateMessageDelivery(event.message_id, event.summary);
      break;
    case "message_receipt_ack":
      confirmMessageReceipt(event.message_id, event.status);
      break;
    case "message_rejected":
      await rejectClientMessage(event.client_id, event.message);
      break;
    case "users":
      state.users = event.users;
      renderUsers();
      requestMissingChannelKeys();
      break;
    case "document_added":
      if (event.document.channel_id === state.activeChannel?.id) {
        if (!state.documents.some(item => item.id === event.document.id)) state.documents.unshift(event.document);
        renderDocuments();
      }
      break;
    case "document_deleted":
      state.documents = state.documents.filter(item => item.id !== event.document_id);
      renderDocuments();
      if (state.activeDocument?.id === event.document_id) {
        closeStage({ force: true });
        toast("Документ видалено, перегляд завершено", "error");
      }
      break;
    case "channel_added":
      if (!state.channels.some(item => item.id === event.channel.id)) state.channels.push(event.channel);
      else state.channels = state.channels.map(item => item.id === event.channel.id ? event.channel : item);
      renderChannels();
      if (event.channel.encrypted) await loadChannelKey(event.channel.id, true);
      break;
    case "channel_key":
      await loadChannelKey(event.channel_id, true);
      if (state.activeChannel?.id === event.channel_id) await loadMessages();
      break;
    case "device_key_added":
      await shareExistingChannelKeys(event.device);
      break;
    case "channel_key_request":
      await shareRequestedChannelKey(event.channel_id, event.device);
      break;
    case "presentation_state": await receivePresentation(event); break;
    case "audio_chunk": playAudioChunk(event); break;
    case "error": toast(event.message, "error"); break;
  }
}

async function loadChannels() {
  const result = await api("/api/channels");
  state.channels = result.channels;
  if (!state.activeChannel || !state.channels.some(item => item.id === state.activeChannel.id)) {
    state.activeChannel = state.channels.find(item => item.id === "lobby") || state.channels[0] || null;
  }
  renderChannels();
}

function renderChannels() {
  const list = $("#channel-list");
  list.replaceChildren();
  for (const channel of state.channels) {
    const button = document.createElement("button");
    button.className = `channel ${state.activeChannel?.id === channel.id ? "active" : ""}`;
    const icon = document.createElement("span");
    icon.textContent = channel.encrypted ? "◆" : "#";
    const name = document.createElement("span");
    name.className = "channel-name";
    name.textContent = channel.name;
    button.append(icon, name);
    if (channel.encrypted) {
      const lock = document.createElement("span");
      lock.className = "channel-lock";
      lock.textContent = "E2EE";
      button.append(lock);
    }
    button.addEventListener("click", () => selectChannel(channel.id));
    list.append(button);
  }
}

async function selectChannel(channelId) {
  const channel = state.channels.find(item => item.id === channelId);
  if (!channel) return;
  const changedChannel = state.activeChannel?.id !== channel.id;
  if (changedChannel && state.activeDocument) closeStage({ force: true });
  state.activeChannel = channel;
  if (changedChannel) state.selectedRecipients.clear();
  renderChannels();
  $("#channel-name").textContent = channel.name;
  $("#channel-description").textContent = channel.description || "Командне обговорення в реальному часі";
  $("#channel-icon").textContent = channel.encrypted ? "◆" : "#";
  $("#message-input").placeholder = `Написати у #${channel.name}${channel.encrypted ? " · E2EE" : ""}`;
  if (channel.encrypted) {
    const key = await loadChannelKey(channel.id);
    if (!key) toast("Очікується ключ цього захищеного каналу", "error");
  }
  $("#documents-channel-name").textContent = `#${channel.name}`;
  renderUsers();
  await Promise.all([loadMessages(), loadDocuments()]);
  $("#sidebar").classList.remove("open");
}

function updateChannelMemberCount() {
  const selected = $$("#channel-member-list input:checked").length;
  $("#channel-member-count").textContent = `${selected} вибрано`;
}

function renderChannelMemberList({ reset = false } = {}) {
  const list = $("#channel-member-list");
  const selected = reset
    ? new Set()
    : new Set($$("input:checked", list).map(input => input.value));
  list.replaceChildren();
  for (const user of state.users) {
    if (user.id === state.me.id) continue;
    const label = document.createElement("label");
    label.className = "channel-member";
    const avatar = document.createElement("div");
    avatar.className = `avatar ${user.online ? "online" : ""}`;
    avatar.textContent = initials(user.display_name);
    const copy = document.createElement("span");
    copy.className = "channel-member-copy";
    const name = document.createElement("strong");
    name.textContent = user.display_name;
    const username = document.createElement("small");
    username.textContent = `@${user.username}${user.online ? " · онлайн" : ""}`;
    copy.append(name, username);
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = user.username;
    checkbox.checked = selected.has(user.username);
    checkbox.addEventListener("change", updateChannelMemberCount);
    label.append(avatar, copy, checkbox);
    list.append(label);
  }
  if (!list.children.length) {
    const empty = document.createElement("div");
    empty.className = "channel-members-empty";
    empty.textContent = "Інші користувачі ще не зареєстровані.";
    list.append(empty);
  }
  updateChannelMemberCount();
}

function openChannelModal() {
  $("#channel-form").reset();
  $("#new-channel-encrypted").checked = true;
  renderChannelMemberList({ reset: true });
  $("#channel-error").textContent = "";
  $("#channel-modal").classList.remove("hidden");
  setTimeout(() => $("#new-channel-name").focus(), 30);
}

function closeChannelModal() {
  $("#channel-modal").classList.add("hidden");
}

async function createChannel(event) {
  event.preventDefault();
  const submit = $("#channel-submit");
  submit.disabled = true;
  $("#channel-error").textContent = "";
  const channelId = crypto.randomUUID();
  const encrypted = $("#new-channel-encrypted").checked;
  const selected = $$("#channel-member-list input:checked").map(item => item.value);
  const memberUsernames = [state.me.username, ...selected];
  let rawKey = null;
  try {
    if (encrypted && !state.device) throw new Error("E2EE-ідентичність пристрою недоступна");
    let keyEnvelopes = [];
    if (encrypted) {
      const result = await api(`/api/keys/devices?usernames=${encodeURIComponent(memberUsernames.join(","))}`);
      const missing = memberUsernames.filter(name => !result.devices.some(device => device.username === name));
      if (missing.length) throw new Error(`Ці користувачі мають спочатку увійти в Nexus: ${missing.join(", ")}`);
      rawKey = crypto.getRandomValues(new Uint8Array(32));
      await storeChannelKey(channelId, rawKey);
      keyEnvelopes = await Promise.all(result.devices.map(device => wrapChannelKey(channelId, rawKey, device)));
    }
    const result = await api("/api/channels", {
      method: "POST",
      body: JSON.stringify({
        id: channelId,
        name: $("#new-channel-name").value.trim(),
        description: $("#new-channel-description").value.trim(),
        member_usernames: selected,
        encrypted,
        key_envelopes: keyEnvelopes,
      }),
    });
    if (!state.channels.some(item => item.id === result.channel.id)) state.channels.push(result.channel);
    $("#channel-form").reset();
    $("#new-channel-encrypted").checked = true;
    closeChannelModal();
    await selectChannel(result.channel.id);
    toast(encrypted ? "Захищений E2EE-канал створено" : "Канал створено");
  } catch (error) {
    if (rawKey) {
      state.channelKeys.delete(channelId);
      await cryptoStoreDelete("channelKeys", channelId);
    }
    $("#channel-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
}

async function shareExistingChannelKeys(device) {
  if (!state.device || device.device_id === state.device.deviceId) return;
  for (const channel of state.channels) {
    if (!channel.encrypted || !channel.members?.some(member => member.username === device.username)) continue;
    await shareRequestedChannelKey(channel.id, device);
  }
}

async function shareRequestedChannelKey(channelId, device) {
  if (!state.device || device.device_id === state.device.deviceId) return;
  const channel = state.channels.find(item => item.id === channelId);
  if (!channel?.encrypted || !channel.members?.some(member => member.username === device.username)) return;
  const key = await loadChannelKey(channelId);
  if (!key) return;
  try {
    const rawKey = new Uint8Array(await crypto.subtle.exportKey("raw", key));
    const envelope = await wrapChannelKey(channelId, rawKey, device);
    await api(`/api/channels/${encodeURIComponent(channelId)}/keys`, {
      method: "POST",
      body: JSON.stringify(envelope),
    });
  } catch (error) {
    console.warn("Cannot share channel key with a device", error);
  }
}

async function requestMissingChannelKeys() {
  if (
    state.keyRequestInFlight
    || !state.device
    || state.socket?.readyState !== WebSocket.OPEN
  ) return;
  state.keyRequestInFlight = true;
  try {
    for (const channel of state.channels.filter(item => item.encrypted)) {
      if (!await loadChannelKey(channel.id)) {
        state.socket.send(JSON.stringify({
          type: "channel_key_request",
          channel_id: channel.id,
          device_id: state.device.deviceId,
        }));
      }
    }
  } finally {
    state.keyRequestInFlight = false;
  }
}

async function loadMessages() {
  if (!state.activeChannel) return;
  state.messages.clear();
  $$("#message-list .message, #message-list .message-date-separator").forEach(item => item.remove());
  const result = await api(`/api/messages?limit=150&room_id=${encodeURIComponent(state.activeChannel.id)}`);
  for (const message of result.messages) {
    await addMessage(message);
    sendMessageReceipt(message.id, message.author_id, "delivered");
  }
  await renderPendingMessages();
  markActiveChannelRead();
  scrollMessages();
}

function colorFor(value) {
  const colors = ["#7c6cff", "#48b9d5", "#d978aa", "#d49b58", "#54b880", "#6f8fe8"];
  let hash = 0;
  for (const char of value) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return colors[Math.abs(hash) % colors.length];
}

function compareMessageOrder(first, second) {
  // реальний час зберігає дату, годинник лампорта розв’язує одночасні події, а uuid дає остаточно стабільний порядок
  const createdDifference = Number(first.created_at || 0) - Number(second.created_at || 0);
  if (createdDifference) return createdDifference;
  const lamportDifference = Number(first.lamport || 0) - Number(second.lamport || 0);
  if (lamportDifference) return lamportDifference;
  return String(first.id || "").localeCompare(String(second.id || ""));
}

function messageOrderFromElement(element) {
  return {
    id: element.dataset.messageId,
    created_at: Number(element.dataset.createdAt),
    lamport: Number(element.dataset.lamport),
  };
}

function rebuildDateSeparators(list) {
  $$(".message-date-separator", list).forEach(separator => separator.remove());
  let previousDateKey = null;
  for (const messageElement of $$(".message", list)) {
    const createdAt = Number(messageElement.dataset.createdAt);
    const dateKey = localDateKey(createdAt);
    if (dateKey === previousDateKey) continue;
    const separator = document.createElement("div");
    separator.className = "message-date-separator";
    separator.dataset.dateKey = dateKey;
    separator.setAttribute("role", "separator");
    separator.setAttribute("aria-label", formatCalendarDate(createdAt));
    const label = document.createElement("span");
    label.textContent = formatCalendarDate(createdAt);
    separator.append(label);
    messageElement.before(separator);
    previousDateKey = dateKey;
  }
}

async function addMessage(message) {
  if (!message?.id || state.messages.has(message.id)) return;
  if (message.receipt_summary) state.messageReceipts.set(message.id, message.receipt_summary);
  state.messages.add(message.id);
  const list = $("#message-list");
  const dateKey = localDateKey(message.created_at);
  const article = document.createElement("article");
  article.className = "message";
  article.classList.toggle("pending", Boolean(message.pending && !message.failed));
  article.classList.toggle("failed", Boolean(message.failed));
  article.dataset.messageId = message.id;
  article.dataset.dateKey = dateKey;
  article.dataset.createdAt = String(message.created_at);
  article.dataset.lamport = String(message.lamport || 0);
  article.dataset.authorId = message.author_id || "";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.style.background = `linear-gradient(135deg, ${colorFor(message.author_name)}, #222c43)`;
  avatar.textContent = initials(message.author_name);
  const body = document.createElement("div");
  body.className = "message-body";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const author = document.createElement("strong");
  author.textContent = message.author_name;
  const time = document.createElement("time");
  time.textContent = formatTime(message.created_at);
  const fullDate = new Date(message.created_at * 1000);
  time.dateTime = fullDate.toISOString();
  time.title = new Intl.DateTimeFormat("uk-UA", { dateStyle: "full", timeStyle: "medium" }).format(fullDate);
  time.setAttribute("aria-label", time.title);
  const node = document.createElement("span");
  node.textContent = `${message.origin_node}${message.encrypted ? " · E2EE" : ""}`;
  const delivery = document.createElement("span");
  const deliveryState = message.failed
    ? { status: "failed", text: "Не надіслано", title: "Сервер відхилив повідомлення" }
    : message.pending
      ? { status: "pending", text: "Очікує надсилання", title: "Повідомлення перебуває в outbox браузера" }
      : deliveryPresentation(state.messageReceipts.get(message.id) || message.receipt_summary);
  delivery.className = `message-delivery ${deliveryState.status}`;
  delivery.textContent = deliveryState.text;
  delivery.title = deliveryState.title;
  const text = document.createElement("div");
  text.className = "message-text";
  text.textContent = message.encrypted ? "Розшифрування…" : message.body;
  meta.append(author, time, node);
  if (message.pending || message.author_id === state.me?.id) meta.append(delivery);
  body.append(meta, text);
  article.append(avatar, body);
  const insertionPoint = $$(".message", list).find(existing => (
    compareMessageOrder(message, messageOrderFromElement(existing)) < 0
  ));
  list.insertBefore(article, insertionPoint || null);
  rebuildDateSeparators(list);
  if (message.encrypted) {
    try {
      text.textContent = await decryptChannelMessage(message);
    } catch (error) {
      text.textContent = `🔒 Неможливо розшифрувати: ${error.message}`;
      text.classList.add("muted");
    }
  }
  scrollMessages();
}

function scrollMessages() {
  const list = $("#message-list");
  requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
}

async function sendMessage(event) {
  // повідомлення спочатку отримує uuid і транзакційно зберігається локально, і лише потім надсилається в мережу
  event.preventDefault();
  const input = $("#message-input");
  const body = input.value.trim();
  if (!body) return;
  if (!state.activeChannel) return;
  const clientId = crypto.randomUUID();
  const createdAt = Date.now() / 1000;
  let payload = {
    type: "message_send",
    client_id: clientId,
    client_created_at: createdAt,
    channel_id: state.activeChannel.id,
    body,
  };
  if (state.activeChannel.encrypted) {
    try {
      const encrypted = await encryptChannelMessage(state.activeChannel.id, body);
      payload = {
        type: "message_send",
        client_id: clientId,
        client_created_at: createdAt,
        channel_id: state.activeChannel.id,
        encrypted: true,
        ...encrypted,
      };
    } catch (error) {
      toast(error.message, "error");
      return;
    }
  }
  const record = {
    id: clientId,
    username: state.me.username,
    channel_id: state.activeChannel.id,
    created_at: createdAt,
    attempts: 0,
    failed: false,
    payload,
  };
  try {
    await putClientOutbox(record);
    input.value = "";
    resizeComposer();
    await addMessage(queuedRecordAsMessage(record));
    await refreshClientOutboxStatus();
    if (state.socket?.readyState === WebSocket.OPEN) {
      await flushClientOutbox();
    } else {
      toast("Повідомлення збережено у браузері й буде надіслано після відновлення зв’язку");
    }
  } catch (error) {
    toast(`Не вдалося зберегти повідомлення локально: ${error.message}`, "error");
  }
}

function resizeComposer() {
  const input = $("#message-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
}

async function loadUsers() {
  const result = await api("/api/users");
  state.users = result.users;
  renderUsers();
}

function availableRecipients() {
  const channel = state.activeChannel;
  const memberNames = channel?.is_private
    ? new Set((channel.members || []).map(member => member.username))
    : null;
  return state.users.filter(user => (
    user.id !== state.me.id && (!memberNames || memberNames.has(user.username))
  ));
}

function renderUsers() {
  const sidebar = $("#sidebar-users");
  const recipients = $("#recipient-list");
  sidebar.replaceChildren();
  recipients.replaceChildren();
  const onlineCount = state.users.filter(user => user.online).length;
  $("#online-count").textContent = onlineCount;
  const audienceNames = new Set(availableRecipients().map(user => user.username));
  for (const user of state.users) {
    const row = document.createElement("div");
    row.className = "sidebar-user";
    row.innerHTML = `<div class="avatar ${user.online ? "online" : ""}">${initials(user.display_name)}</div>`;
    const name = document.createElement("span");
    name.textContent = user.display_name + (user.id === state.me.id ? " (ви)" : "");
    row.append(name);
    sidebar.append(row);

    if (user.id === state.me.id || !audienceNames.has(user.username)) continue;
    const label = document.createElement("label");
    label.className = "recipient";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedRecipients.has(user.username);
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.selectedRecipients.add(user.username) : state.selectedRecipients.delete(user.username);
      updateSelection();
    });
    const avatar = document.createElement("div");
    avatar.className = `avatar ${user.online ? "online" : ""}`;
    avatar.textContent = initials(user.display_name);
    const copy = document.createElement("div");
    copy.className = "recipient-copy";
    const strong = document.createElement("strong");
    strong.textContent = user.display_name;
    const small = document.createElement("small");
    small.textContent = user.online ? `онлайн · ${user.origin_node}` : "не в мережі";
    copy.append(strong, small);
    label.append(checkbox, avatar, copy);
    recipients.append(label);
  }
  updateSelection();
  if (!$("#channel-modal").classList.contains("hidden")) renderChannelMemberList();
}

function updateSelection() {
  const available = availableRecipients();
  for (const username of [...state.selectedRecipients]) {
    if (!available.some(user => user.username === username)) state.selectedRecipients.delete(username);
  }
  $("#selected-count").textContent = state.selectedRecipients.size;
  $("#select-all-users").checked = available.length > 0 && available.every(user => state.selectedRecipients.has(user.username));
  $("#select-all-users").indeterminate = state.selectedRecipients.size > 0 && !$("#select-all-users").checked;
}

function selectAllUsers(event) {
  state.selectedRecipients.clear();
  if (event.target.checked) availableRecipients().forEach(user => state.selectedRecipients.add(user.username));
  renderUsers();
}

function peoplePanelUsesOverlay() {
  return window.matchMedia("(max-width: 1020px)").matches;
}

function applyPeoplePanelState() {
  const panel = $("#people-panel");
  const overlay = peoplePanelUsesOverlay();
  panel.classList.toggle("open", overlay && state.peoplePanelOpen);
  panel.classList.toggle("collapsed", !overlay && !state.peoplePanelOpen);
  panel.setAttribute("aria-hidden", String(!state.peoplePanelOpen));
  panel.inert = !state.peoplePanelOpen;
  const toggle = $("#toggle-people");
  toggle.setAttribute("aria-expanded", String(state.peoplePanelOpen));
  toggle.setAttribute("aria-label", state.peoplePanelOpen ? "Сховати аудиторію трансляції" : "Показати аудиторію трансляції");
}

function setPeoplePanelOpen(open) {
  state.peoplePanelOpen = Boolean(open);
  applyPeoplePanelState();
}

let previousPeoplePanelOverlay = peoplePanelUsesOverlay();
function handlePeoplePanelResize() {
  const overlay = peoplePanelUsesOverlay();
  if (overlay !== previousPeoplePanelOverlay) {
    state.peoplePanelOpen = !overlay;
    previousPeoplePanelOverlay = overlay;
  }
  applyPeoplePanelState();
}

async function loadDocuments() {
  // бібліотека запитує лише файли активного каналу, тому чужі приватні документи не потрапляють до інтерфейсу
  if (!state.activeChannel) {
    state.documents = [];
    renderDocuments();
    return;
  }
  const result = await api(`/api/documents?channel_id=${encodeURIComponent(state.activeChannel.id)}`);
  state.documents = result.documents;
  renderDocuments();
}

function renderDocuments() {
  const list = $("#document-list");
  list.replaceChildren();
  $("#document-count").textContent = state.documents.length;
  if (!state.documents.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Бібліотека порожня. Додайте перший матеріал.";
    list.append(empty);
    return;
  }
  for (const doc of state.documents) {
    const card = document.createElement("article");
    card.className = "document-card";
    const icon = document.createElement("div");
    icon.className = "document-icon";
    icon.textContent = doc.media_type === "application/pdf" ? "PDF" : doc.media_type.startsWith("image/") ? "IMG" : "TXT";
    const info = document.createElement("div");
    info.className = "document-info";
    const name = document.createElement("strong");
    name.textContent = doc.name;
    const meta = document.createElement("small");
    const pageMeta = doc.media_type === "application/pdf" ? ` · ${doc.page_count} стор.` : "";
    meta.textContent = `${doc.owner_name} · ${formatShortDate(doc.created_at)}${pageMeta} · ${formatBytes(doc.size || 0)}`;
    info.append(name, meta);
    card.append(icon, info);
    if (doc.owner_id === state.me.id) {
      const remove = document.createElement("button");
      remove.className = "icon-button delete-document";
      remove.textContent = "×";
      remove.title = "Видалити";
      remove.setAttribute("aria-label", `Видалити ${doc.name}`);
      remove.addEventListener("click", async event => {
        event.stopPropagation();
        if (!confirm(`Видалити «${doc.name}»?`)) return;
        try { await api(`/api/documents/${doc.id}`, { method: "DELETE" }); } catch (error) { toast(error.message, "error"); }
      });
      card.append(remove);
    } else {
      const open = document.createElement("span");
      open.textContent = "›";
      open.setAttribute("aria-hidden", "true");
      card.append(open);
    }
    card.addEventListener("click", () => openDocument(doc));
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", `Відкрити ${doc.name}`);
    card.addEventListener("keydown", event => {
      if (!["Enter", " "].includes(event.key) || event.target !== card) return;
      event.preventDefault();
      openDocument(doc);
    });
    list.append(card);
  }
}

async function uploadDocument(event) {
  event.preventDefault();
  const input = $("#document-file");
  if (!input.files[0] || !state.activeChannel) return;
  const progress = $("#upload-progress");
  progress.classList.add("active");
  const data = new FormData();
  data.append("file", input.files[0]);
  data.append("channel_id", state.activeChannel.id);
  try {
    const result = await api("/api/documents", { method: "POST", body: data });
    if (!state.documents.some(doc => doc.id === result.document.id)) state.documents.unshift(result.document);
    renderDocuments();
    input.value = "";
    toast(`Документ додано до #${state.activeChannel.name}`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    progress.classList.remove("active");
  }
}

async function openDocuments() {
  try {
    await loadDocuments();
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  $("#documents-drawer").classList.add("open");
  $("#documents-drawer").setAttribute("aria-hidden", "false");
}

function closeDocuments() {
  $("#documents-drawer").classList.remove("open");
  $("#documents-drawer").setAttribute("aria-hidden", "true");
}

function refreshPageControls() {
  const locked = Boolean(state.remotePresentation);
  const pages = state.activeDocument?.page_count || 1;
  $("#current-page").disabled = locked;
  $("#page-prev").disabled = locked || state.page <= 1;
  $("#page-next").disabled = locked || state.page >= pages;
  $("#zoom-out").disabled = locked || state.documentZoom <= 0.5;
  $("#zoom-reset").disabled = locked;
  $("#zoom-in").disabled = locked || state.documentZoom >= 2;
  $("#zoom-reset").textContent = `${Math.round(state.documentZoom * 100)}%`;
}

function setStageMode(mode) {
  const stage = $("#stage");
  stage.classList.toggle("presenter-mode", mode === "presenter");
  stage.classList.toggle("viewer-mode", mode === "viewer");
  stage.classList.toggle(
    "text-pointer-through",
    mode === "presenter" && Boolean(state.activeDocument?.media_type?.startsWith("text/")),
  );
  const viewerMode = mode === "viewer";
  $("#share-document").classList.toggle("hidden", viewerMode);
  $("#close-stage").classList.toggle("hidden", viewerMode);
  const status = $("#presentation-status");
  if (mode === "presenter") {
    status.textContent = `Наживо · ${viewerCountLabel(state.presentationRecipients.length)}`;
    status.classList.remove("hidden");
  } else if (viewerMode) {
    status.textContent = `Наживо · керує ${state.remotePresentation?.senderName || "демонстратор"}`;
    status.classList.remove("hidden");
  } else {
    status.classList.add("hidden");
  }
  if (viewerMode) applyRemoteViewportFit();
  else clearRemoteViewportFit();
  updatePresentationSurfaceInsets();
  refreshPageControls();
}

function updatePresentationSurfaceInsets() {
  const stage = $("#stage");
  const viewer = $("#document-viewer");
  const surface = $("#presentation-surface");
  if (!stage || !viewer || !surface) return;

  // прозорий шар відстежує курсор, але не повинен перекривати системні смуги прокручування
  const presenterMode = stage.classList.contains("presenter-mode");
  const horizontalOverflow = presenterMode && viewer.scrollWidth - viewer.clientWidth > 1;
  const verticalOverflow = presenterMode && viewer.scrollHeight - viewer.clientHeight > 1;
  const horizontalScrollbar = Math.max(viewer.offsetHeight - viewer.clientHeight, 16);
  const verticalScrollbar = Math.max(viewer.offsetWidth - viewer.clientWidth, 16);
  surface.style.bottom = horizontalOverflow ? `${horizontalScrollbar}px` : "0px";
  surface.style.right = verticalOverflow ? `${verticalScrollbar}px` : "0px";
}

function clearRemoteViewportFit() {
  const viewer = $("#document-viewer");
  viewer.classList.remove("remote-fitted");
  for (const property of ["width", "height", "left", "top", "transform"]) {
    viewer.style.removeProperty(property);
  }
}

function applyRemoteViewportFit() {
  const viewer = $("#document-viewer");
  const shell = $("#viewer-shell");
  const remote = state.remotePresentation;
  const sourceWidth = Number(remote?.viewportWidth);
  const sourceHeight = Number(remote?.viewportHeight);
  if (!remote || !Number.isFinite(sourceWidth) || !Number.isFinite(sourceHeight)
    || sourceWidth <= 0 || sourceHeight <= 0 || shell.clientWidth <= 0 || shell.clientHeight <= 0) {
    clearRemoteViewportFit();
    return 1;
  }
  // область стрімера відтворюється у вихідному розмірі, а потім цілком вписується у вікно глядача
  const scale = Math.min(shell.clientWidth / sourceWidth, shell.clientHeight / sourceHeight);
  const renderedWidth = sourceWidth * scale;
  const renderedHeight = sourceHeight * scale;
  viewer.classList.add("remote-fitted");
  viewer.style.width = `${sourceWidth}px`;
  viewer.style.height = `${sourceHeight}px`;
  viewer.style.left = `${Math.max((shell.clientWidth - renderedWidth) / 2, 0)}px`;
  viewer.style.top = `${Math.max((shell.clientHeight - renderedHeight) / 2, 0)}px`;
  viewer.style.transform = `scale(${scale})`;
  return scale;
}

function presentationScrollRatio(axis = "y") {
  // надсилається частка прокрученого вмісту, а не пікселі, щоб скрол збігався на екранах різного розміру
  const viewer = $("#document-viewer");
  const horizontal = axis === "x";
  const available = horizontal
    ? viewer.scrollWidth - viewer.clientWidth
    : viewer.scrollHeight - viewer.clientHeight;
  const position = horizontal ? viewer.scrollLeft : viewer.scrollTop;
  return available > 0 ? clampRatio(position / available) : 0;
}

function applyPresentationScroll(value, horizontalValue = 0) {
  if (!Number.isFinite(value) || !Number.isFinite(horizontalValue)) return;
  const viewer = $("#document-viewer");
  const verticalAvailable = viewer.scrollHeight - viewer.clientHeight;
  const horizontalAvailable = viewer.scrollWidth - viewer.clientWidth;
  viewer.scrollTop = clampRatio(value) * Math.max(verticalAvailable, 0);
  viewer.scrollLeft = clampRatio(horizontalValue) * Math.max(horizontalAvailable, 0);
}

function clampRatio(value) {
  return Math.min(Math.max(Number(value) || 0, 0), 1);
}

function cursorViewportPosition(clientX, clientY) {
  const rect = $("#document-viewer").getBoundingClientRect();
  return {
    x: clampRatio((clientX - rect.left) / Math.max(rect.width, 1)),
    y: clampRatio((clientY - rect.top) / Math.max(rect.height, 1)),
  };
}

function textOffsetAtPoint(element, clientX, clientY) {
  // для тексту зберігається позиція символу, тому курсор не з’їжджає після зміни ширини або масштабу
  try {
    let container = null;
    let offset = 0;
    if (document.caretPositionFromPoint) {
      const caret = document.caretPositionFromPoint(clientX, clientY);
      container = caret?.offsetNode || null;
      offset = caret?.offset || 0;
    } else if (document.caretRangeFromPoint) {
      const range = document.caretRangeFromPoint(clientX, clientY);
      container = range?.startContainer || null;
      offset = range?.startOffset || 0;
    }
    if (!container || !element.contains(container)) return null;
    const prefix = document.createRange();
    prefix.selectNodeContents(element);
    prefix.setEnd(container, offset);
    return prefix.toString().length;
  } catch {
    return null;
  }
}

function textOffsetIsNearPointer(element, offset, clientX, clientY) {
  // браузер повертає найближчий символ навіть у порожній частині рядка, тому перевіряємо реальну відстань до тексту
  if (!Number.isInteger(offset)) return false;
  const style = getComputedStyle(element);
  const fontSize = Number.parseFloat(style.fontSize) || 16;
  const parsedLineHeight = Number.parseFloat(style.lineHeight);
  const lineHeight = Number.isFinite(parsedLineHeight) ? parsedLineHeight : fontSize * 1.6;
  const zoom = Number(state.documentZoom) || 1;
  const horizontalTolerance = Math.max(fontSize * zoom * 0.7, 8);
  const verticalTolerance = Math.max(lineHeight * zoom * 0.6, 10);
  const points = [offset, offset - 1, offset + 1]
    .filter((candidate) => candidate >= 0)
    .map((candidate) => textPointFromOffset(element, candidate))
    .filter(Boolean);
  return points.some((point) => (
    Math.abs(point.x - clientX) <= horizontalTolerance
    && Math.abs(point.y - clientY) <= verticalTolerance
  ));
}

function presentationCursorFromPoint(clientX, clientY) {
  // формат документа визначає систему координат: область перегляду, зображення або позиція в тексті
  const viewport = cursorViewportPosition(clientX, clientY);
  if (state.activeDocument?.media_type === "application/pdf") {
    return { space: "viewport", ...viewport };
  }

  const image = $("#document-viewer img");
  if (image) {
    const rect = image.getBoundingClientRect();
    const inside = clientX >= rect.left && clientX <= rect.right
      && clientY >= rect.top && clientY <= rect.bottom;
    if (inside) {
      return {
        space: "image",
        x: clampRatio((clientX - rect.left) / Math.max(rect.width, 1)),
        y: clampRatio((clientY - rect.top) / Math.max(rect.height, 1)),
        viewport_x: viewport.x,
        viewport_y: viewport.y,
      };
    }
  }

  const text = $("#document-viewer .text-document");
  if (text) {
    const rect = text.getBoundingClientRect();
    const inside = clientX >= rect.left && clientX <= rect.right
      && clientY >= rect.top && clientY <= rect.bottom;
    if (inside) {
      const payload = {
        space: "text",
        x: clampRatio((clientX - rect.left) / Math.max(rect.width, 1)),
        y: clampRatio((clientY - rect.top) / Math.max(rect.height, 1)),
        viewport_x: viewport.x,
        viewport_y: viewport.y,
      };
      const textOffset = textOffsetAtPoint(text, clientX, clientY);
      if (textOffsetIsNearPointer(text, textOffset, clientX, clientY)) payload.text_offset = textOffset;
      return payload;
    }
  }
  return { space: "viewport", ...viewport };
}

function refreshPresentationCursorAtPointer() {
  if (!state.lastPresentationPointer) return state.lastPresentationCursor;
  const rect = $("#document-viewer").getBoundingClientRect();
  const clientX = rect.left + state.lastPresentationPointer.x * rect.width;
  const clientY = rect.top + state.lastPresentationPointer.y * rect.height;
  state.lastPresentationCursor = presentationCursorFromPoint(clientX, clientY);
  return state.lastPresentationCursor;
}

function applyDocumentZoom() {
  const viewer = $("#document-viewer");
  const content = $("#document-content");
  const layer = $("#document-zoom-layer");
  if (!viewer || !content || !layer) {
    refreshPageControls();
    return;
  }

  // полотно має фактичний масштабований розмір, тому крайні значення zoom завжди змінюють документ і смуги прокручування
  const zoom = Math.min(Math.max(Number(state.documentZoom) || 1, 0.5), 2);
  layer.style.left = "0px";
  layer.style.top = "0px";
  layer.style.transform = "none";
  content.style.width = "0px";
  content.style.height = "0px";
  // коротке згортання полотна прибирає старі смуги прокручування з вимірювання нового базового розміру
  const baseWidth = Math.max(viewer.clientWidth, 1);
  const baseHeight = Math.max(viewer.clientHeight, 1);
  const fixedHeight = state.activeDocument?.media_type === "application/pdf"
    || Boolean(state.activeDocument?.media_type?.startsWith("image/"));

  layer.style.width = `${baseWidth}px`;
  layer.style.minHeight = `${baseHeight}px`;
  layer.style.height = fixedHeight ? `${baseHeight}px` : "auto";

  const naturalHeight = Math.max(layer.scrollHeight, baseHeight);
  const scaledWidth = baseWidth * zoom;
  const scaledHeight = naturalHeight * zoom;
  const canvasWidth = Math.max(baseWidth, scaledWidth);
  const canvasHeight = Math.max(baseHeight, scaledHeight);
  content.style.width = `${Math.ceil(canvasWidth)}px`;
  content.style.height = `${Math.ceil(canvasHeight)}px`;
  layer.style.left = `${Math.max((canvasWidth - scaledWidth) / 2, 0)}px`;
  layer.style.top = `${fixedHeight ? Math.max((canvasHeight - scaledHeight) / 2, 0) : 0}px`;
  layer.style.transform = `scale(${zoom})`;
  updatePresentationSurfaceInsets();
  refreshPageControls();
}

function setDocumentZoom(value, { remote = false, announce = true } = {}) {
  // перед зміною масштабу зберігаються відносні скрол і курсор, а після перерахунку вони відновлюються
  if (state.remotePresentation && !remote) return;
  const normalized = Math.min(Math.max(Math.round((Number(value) || 1) * 10) / 10, 0.5), 2);
  if (normalized === state.documentZoom) return;
  const vertical = presentationScrollRatio();
  const horizontal = presentationScrollRatio("x");
  state.documentZoom = normalized;
  applyDocumentZoom();
  requestAnimationFrame(() => {
    applyPresentationScroll(vertical, horizontal);
    if (state.presenting && !remote) {
      refreshPresentationCursorAtPointer();
      sendPresentationEvent("update", state.lastPresentationCursor);
    }
    if (announce && !remote) showStageNotice(`Масштаб ${Math.round(normalized * 100)}%`);
  });
}

function textPointFromOffset(element, requestedOffset) {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  let remaining = Math.max(Number(requestedOffset) || 0, 0);
  while (node && remaining > node.data.length) {
    remaining -= node.data.length;
    node = walker.nextNode();
  }
  if (!node) {
    node = element.lastChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) return null;
    remaining = node.data.length;
  }
  const offset = Math.min(remaining, node.data.length);
  const range = document.createRange();
  range.setStart(node, offset);
  range.setEnd(node, Math.min(offset + 1, node.data.length));
  let rect = range.getBoundingClientRect();
  if (!rect.width && !rect.height && offset > 0) {
    range.setStart(node, offset - 1);
    range.setEnd(node, offset);
    rect = range.getBoundingClientRect();
  }
  return rect.width || rect.height
    ? { x: offset === node.data.length ? rect.right : rect.left, y: rect.top + Math.max(rect.height / 2, 1) }
    : null;
}

let remoteCursorFrame = 0;
function renderRemoteCursor(payload, senderName) {
  // одержані координати щоразу проєктуються в поточну геометрію вікна глядача
  cancelAnimationFrame(remoteCursorFrame);
  const cursor = $("#remote-cursor");
  if (!payload || !Number.isFinite(payload.x) || !Number.isFinite(payload.y)) {
    cursor.classList.add("hidden");
    return;
  }
  remoteCursorFrame = requestAnimationFrame(() => {
    const shell = $("#viewer-shell");
    const viewer = $("#document-viewer");
    const shellRect = shell.getBoundingClientRect();
    const viewerRect = viewer.getBoundingClientRect();
    let point = null;

    if (payload.space === "image") {
      const image = $("#document-viewer img");
      if (image) {
        const rect = image.getBoundingClientRect();
        point = { x: rect.left + clampRatio(payload.x) * rect.width, y: rect.top + clampRatio(payload.y) * rect.height };
      }
    } else if (payload.space === "text") {
      const text = $("#document-viewer .text-document");
      if (text) {
        if (Number.isInteger(payload.text_offset)) point = textPointFromOffset(text, payload.text_offset);
        if (!point && Number.isFinite(payload.viewport_x) && Number.isFinite(payload.viewport_y)) {
          // у порожньому місці зберігається вільна позиція миші, а не найближча літера
          point = {
            x: viewerRect.left + clampRatio(payload.viewport_x) * viewerRect.width,
            y: viewerRect.top + clampRatio(payload.viewport_y) * viewerRect.height,
          };
        } else if (!point) {
          const rect = text.getBoundingClientRect();
          point = { x: rect.left + clampRatio(payload.x) * rect.width, y: rect.top + clampRatio(payload.y) * rect.height };
        }
        const outsideViewport = point.x < viewerRect.left || point.x > viewerRect.right
          || point.y < viewerRect.top || point.y > viewerRect.bottom;
        if (outsideViewport && Number.isFinite(payload.viewport_x) && Number.isFinite(payload.viewport_y)) {
          // курсор не змінює скрол: якщо перенесення рядків відрізняється, використовується його екранна позиція
          point = {
            x: viewerRect.left + clampRatio(payload.viewport_x) * viewerRect.width,
            y: viewerRect.top + clampRatio(payload.viewport_y) * viewerRect.height,
          };
        }
      }
    }

    point ||= {
      x: viewerRect.left + clampRatio(payload.x) * viewerRect.width,
      y: viewerRect.top + clampRatio(payload.y) * viewerRect.height,
    };
    const tolerance = 2;
    if (
      point.x < viewerRect.left - tolerance || point.x > viewerRect.right + tolerance
      || point.y < viewerRect.top - tolerance || point.y > viewerRect.bottom + tolerance
    ) {
      cursor.classList.add("hidden");
      return;
    }
    cursor.style.left = `${Math.min(Math.max(point.x - shellRect.left, 0), shellRect.width)}px`;
    cursor.style.top = `${Math.min(Math.max(point.y - shellRect.top, 0), shellRect.height)}px`;
    $("span", cursor).textContent = senderName;
    cursor.classList.remove("hidden");
  });
}

function createPdfFrame() {
  const frame = document.createElement("iframe");
  frame.title = state.activeDocument?.name || "PDF";
  frame.src = `${state.documentUrl}#page=${state.page}&view=Fit&toolbar=0&navpanes=0&scrollbar=0`;
  return frame;
}

function resetViewportPosition() {
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  const app = $("#app");
  if (app) {
    app.scrollTop = 0;
    app.scrollLeft = 0;
  }
}

async function openDocument(doc, remoteInfo = null) {
  resetViewportPosition();
  closeDocuments();
  if (state.presenting && state.activeDocument?.id !== doc.id) stopPresentation();
  const loadId = ++state.documentLoadId;
  state.activeDocument = doc;
  state.page = remoteInfo?.page || 1;
  state.documentZoom = Math.min(Math.max(Number(remoteInfo?.zoom) || 1, 0.5), 2);
  state.lastPresentationPointer = null;
  state.remotePresentation = remoteInfo;
  state.presenting = false;
  $("#share-document").textContent = "Транслювати";
  $("#share-document").classList.remove("live");
  $("#stage").classList.remove("hidden");
  $("#stage-name").textContent = doc.name;
  $("#stage-owner").textContent = remoteInfo
    ? `${remoteInfo.senderName} · віддалена трансляція`
    : `${doc.owner_name} · ${doc.origin_node}`;
  $("#stage-kind").textContent = doc.media_type === "application/pdf" ? "PDF" : doc.media_type.startsWith("image/") ? "IMG" : "DOC";
  $("#current-page").value = state.page;
  $("#current-page").max = doc.page_count;
  $("#total-pages").textContent = doc.page_count;
  $("#page-controls").classList.toggle("hidden", doc.media_type !== "application/pdf");
  setStageMode(remoteInfo ? "viewer" : "local");
  const viewer = $("#document-viewer");
  viewer.classList.toggle("pdf-document", doc.media_type === "application/pdf");
  viewer.textContent = "Завантаження…";
  if (state.documentUrl) URL.revokeObjectURL(state.documentUrl);
  state.documentUrl = null;
  try {
    const response = await fetch(`/api/documents/${doc.id}/content`, { headers: { Authorization: `Bearer ${state.token}` } });
    if (!response.ok) throw new Error("Не вдалося відкрити документ");
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    if (loadId !== state.documentLoadId || state.activeDocument?.id !== doc.id) {
      URL.revokeObjectURL(objectUrl);
      return;
    }
    state.documentUrl = objectUrl;
    viewer.replaceChildren();
    const content = document.createElement("div");
    content.id = "document-content";
    content.className = [
      "document-content",
      doc.media_type === "application/pdf" ? "pdf-content" : "",
      doc.media_type.startsWith("image/") ? "image-content" : "",
    ].filter(Boolean).join(" ");
    const zoomLayer = document.createElement("div");
    zoomLayer.id = "document-zoom-layer";
    zoomLayer.className = "document-zoom-layer";
    if (doc.media_type.startsWith("text/")) {
      const text = document.createElement("div");
      text.className = "text-document";
      text.textContent = await blob.text();
      if (loadId !== state.documentLoadId) return;
      zoomLayer.append(text);
    } else if (doc.media_type === "application/pdf") {
      zoomLayer.append(createPdfFrame());
    } else {
      const image = document.createElement("img");
      image.alt = doc.name;
      image.src = state.documentUrl;
      zoomLayer.append(image);
    }
    content.append(zoomLayer);
    viewer.append(content);
    applyDocumentZoom();
    if (remoteInfo) {
      const latestRemote = state.remotePresentation?.sessionId === remoteInfo.sessionId
        ? state.remotePresentation
        : remoteInfo;
      applyPresentationScroll(latestRemote.scroll, latestRemote.scrollX);
    }
  } catch (error) {
    if (loadId !== state.documentLoadId) return;
    viewer.textContent = error.message;
    toast(error.message, "error");
  }
}

function updatePage(nextPage, fromRemote = false) {
  if (!state.activeDocument || (state.remotePresentation && !fromRemote)) return;
  const normalizedPage = Math.min(Math.max(Number(nextPage) || 1, 1), state.activeDocument.page_count);
  const pageChanged = normalizedPage !== state.page;
  state.page = normalizedPage;
  $("#current-page").value = state.page;
  refreshPageControls();
  const frame = $("#document-viewer iframe");
  if (pageChanged && frame && state.documentUrl) {
    frame.replaceWith(createPdfFrame());
  }
  if (state.presenting && !fromRemote) {
    state.lastPresentationCursor = null;
    state.lastPresentationPointer = null;
    sendPresentationEvent("update", null);
  }
}

function startPresentation() {
  // демонстратор стає авторитетним джерелом сторінки, масштабу, скролу і курсора для вибраних одержувачів
  if (!state.activeDocument) return;
  if (state.selectedRecipients.size === 0) {
    toast("Спочатку оберіть одержувачів у панелі праворуч", "error");
    setPeoplePanelOpen(true);
    return;
  }
  state.presenting = true;
  state.presentationSessionId = crypto.randomUUID();
  state.presentationRecipients = [...state.selectedRecipients];
  state.presentationSequence = 0;
  state.lastPresentationCursor = null;
  state.lastPresentationPointer = null;
  $("#share-document").textContent = "Зупинити";
  $("#share-document").classList.add("live");
  setStageMode("presenter");
  sendPresentationEvent("start", null);
  clearInterval(state.presentationHeartbeatTimer);
  state.presentationHeartbeatTimer = setInterval(() => {
    if (state.presenting && state.socket?.readyState === WebSocket.OPEN) {
      sendPresentationEvent("update", state.lastPresentationCursor);
    }
  }, 2000);
  showStageNotice(`Демонстрація для ${state.presentationRecipients.length} учасн.`);
}

function stopPresentation() {
  if (!state.presenting) return;
  sendPresentationEvent("stop", null);
  state.presenting = false;
  clearInterval(state.presentationHeartbeatTimer);
  state.presentationHeartbeatTimer = null;
  state.presentationSessionId = null;
  state.presentationRecipients = [];
  state.lastPresentationCursor = null;
  state.lastPresentationPointer = null;
  $("#share-document").textContent = "Транслювати";
  $("#share-document").classList.remove("live");
  if (state.activeDocument) setStageMode("local");
  showStageNotice("Демонстрацію зупинено");
}

function togglePresentation() {
  if (state.remotePresentation) return;
  state.presenting ? stopPresentation() : startPresentation();
}

function sendPresentationEvent(action, cursor) {
  // номер сеансу відокремлює трансляції, а зростаюча послідовність дозволяє глядачу відкинути запізнілий пакет
  if (!state.activeDocument || !state.presentationSessionId) return false;
  state.presentationSequence += 1;
  const viewer = $("#document-viewer");
  return sendSocket({
    type: "presentation_state",
    action,
    session_id: state.presentationSessionId,
    sequence: state.presentationSequence,
    document_id: state.activeDocument.id,
    channel_id: state.activeDocument.channel_id,
    page: state.page,
    scroll: presentationScrollRatio(),
    scroll_x: presentationScrollRatio("x"),
    zoom: state.documentZoom,
    viewport_width: viewer.offsetWidth,
    viewport_height: viewer.offsetHeight,
    cursor,
    recipients: state.presentationRecipients,
  });
}

let cursorThrottle = 0;
function trackCursor(event) {
  // обмеження частоти зменшує мережеве навантаження без помітної втрати плавності
  if (!state.presenting) return;
  const now = performance.now();
  if (now - cursorThrottle < 50) return;
  cursorThrottle = now;
  state.lastPresentationPointer = cursorViewportPosition(event.clientX, event.clientY);
  state.lastPresentationCursor = presentationCursorFromPoint(event.clientX, event.clientY);
  sendPresentationEvent("update", state.lastPresentationCursor);
}

function clearPresentationCursor() {
  if (!state.presenting || state.lastPresentationCursor === null) return;
  state.lastPresentationCursor = null;
  state.lastPresentationPointer = null;
  sendPresentationEvent("update", null);
}

let wheelPageThrottle = 0;
let wheelPageAccumulator = 0;
let lastWheelEventAt = 0;

function wheelDeltaPixels(event, axis = "y") {
  const value = axis === "x" ? event.deltaX : event.deltaY;
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) return value * 16;
  if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) {
    const viewer = $("#document-viewer");
    return value * (axis === "x" ? viewer.clientWidth : viewer.clientHeight);
  }
  return value;
}

function scrollPresentationContent(delta, axis = "y") {
  const viewer = $("#document-viewer");
  const horizontal = axis === "x";
  const available = Math.max(
    horizontal ? viewer.scrollWidth - viewer.clientWidth : viewer.scrollHeight - viewer.clientHeight,
    0,
  );
  if (available <= 1) return false;
  const property = horizontal ? "scrollLeft" : "scrollTop";
  const previous = viewer[property];
  viewer[property] = Math.min(Math.max(previous + delta, 0), available);
  if (Math.abs(viewer[property] - previous) < 0.5) return false;
  trackPresentationScroll();
  return true;
}

function handlePresentationWheel(event) {
  if (!state.presenting && !state.remotePresentation) return;
  event.preventDefault();
  event.stopPropagation();
  if (!state.presenting) return;
  const deltaX = wheelDeltaPixels(event, "x");
  const deltaY = wheelDeltaPixels(event, "y");
  const horizontalDelta = Math.abs(deltaX) > 0.5 ? deltaX : deltaY;
  const horizontalIntent = Math.abs(deltaX) > Math.abs(deltaY)
    || (event.shiftKey && Math.abs(deltaY) > 0.5);
  if (horizontalIntent && scrollPresentationContent(horizontalDelta, "x")) {
    wheelPageAccumulator = 0;
    return;
  }
  const delta = deltaY;
  const isPdf = state.activeDocument?.media_type === "application/pdf";
  if (scrollPresentationContent(delta)) {
    wheelPageAccumulator = 0;
    return;
  }
  if (isPdf && (state.activeDocument?.page_count || 1) > 1) {
    const now = performance.now();
    if (now - lastWheelEventAt > 500) wheelPageAccumulator = 0;
    lastWheelEventAt = now;
    wheelPageAccumulator += delta;
    if (Math.abs(wheelPageAccumulator) < 30 || now - wheelPageThrottle < 220) return;
    wheelPageThrottle = now;
    const direction = wheelPageAccumulator > 0 ? 1 : -1;
    wheelPageAccumulator = 0;
    updatePage(state.page + direction);
    showStageNotice(`Сторінка ${state.page} із ${state.activeDocument.page_count}`);
    return;
  }
  scrollPresentationContent(delta);
}

let scrollThrottle = 0;
let scrollTrailingTimer = null;
function trackPresentationScroll() {
  // під час скролу координати курсора обчислюються заново, щоб курсор не «прокручувався» разом із текстом
  if (!state.presenting) {
    if (state.remotePresentation?.cursor) {
      renderRemoteCursor(state.remotePresentation.cursor, state.remotePresentation.senderName);
    }
    return;
  }
  const now = performance.now();
  if (now - scrollThrottle >= 80) {
    scrollThrottle = now;
    refreshPresentationCursorAtPointer();
    sendPresentationEvent("update", state.lastPresentationCursor);
  }
  clearTimeout(scrollTrailingTimer);
  scrollTrailingTimer = setTimeout(() => {
    if (!state.presenting) return;
    scrollThrottle = performance.now();
    refreshPresentationCursorAtPointer();
    sendPresentationEvent("update", state.lastPresentationCursor);
  }, 100);
}

function armRemotePresentationTimeout(sessionId) {
  // оренда сеансу автоматично закриває зависле вікно, якщо вузол або демонстратор раптово зник
  clearTimeout(state.remotePresentationTimer);
  state.remotePresentationTimer = setTimeout(() => {
    if (state.remotePresentation?.sessionId === sessionId) {
      endRemotePresentation("Трансляцію завершено: зв’язок із демонстратором втрачено");
    }
  }, 7000);
}

function endRemotePresentation(message = "Демонстрацію завершено") {
  if (!state.remotePresentation) return;
  clearTimeout(state.remotePresentationTimer);
  state.remotePresentationTimer = null;
  state.remotePresentation = null;
  closeStage({ force: true });
  toast(message);
}

async function receivePresentation(event) {
  const action = ["start", "update", "stop"].includes(event.action) ? event.action : "update";
  const sessionId = String(event.session_id || `legacy:${event.sender}`);
  const sequence = Math.max(Number(event.sequence) || 0, 0);
  const current = state.remotePresentation;
  if (action === "stop") {
    if (current?.sessionId === sessionId && current.sender === event.sender) endRemotePresentation();
    return;
  }
  if (state.presenting) return;
  if (current?.sessionId === sessionId && current.sender === event.sender && sequence <= current.sequence) return;

  let doc = state.documents.find(item => item.id === event.document_id);
  if (!doc && event.channel_id) {
    try {
      const result = await api(`/api/documents?channel_id=${encodeURIComponent(event.channel_id)}`);
      doc = result.documents.find(item => item.id === event.document_id);
      if (event.channel_id === state.activeChannel?.id) {
        state.documents = result.documents;
        renderDocuments();
      }
    } catch {
      return;
    }
  } else if (!doc) {
    await loadDocuments();
    doc = state.documents.find(item => item.id === event.document_id);
  }
  if (!doc) return;

  const replacing = !current || current.sessionId !== sessionId || current.sender !== event.sender;
  const remoteInfo = {
    sender: event.sender,
    senderName: event.sender_name,
    sessionId,
    sequence,
    page: event.page,
    scroll: Number(event.scroll) || 0,
    scrollX: Number(event.scroll_x) || 0,
    zoom: Math.min(Math.max(Number(event.zoom) || 1, 0.5), 2),
    viewportWidth: Math.max(Number(event.viewport_width) || 0, 0),
    viewportHeight: Math.max(Number(event.viewport_height) || 0, 0),
    cursor: event.cursor || null,
  };
  state.remotePresentation = remoteInfo;
  if (replacing || state.activeDocument?.id !== doc.id || $("#stage").classList.contains("hidden")) {
    await openDocument(doc, remoteInfo);
  }
  if (state.remotePresentation?.sessionId !== sessionId) return;
  if (state.remotePresentation.sequence > sequence) return;
  state.remotePresentation.sequence = sequence;
  state.remotePresentation.page = event.page;
  state.remotePresentation.scroll = Number(event.scroll) || 0;
  state.remotePresentation.scrollX = Number(event.scroll_x) || 0;
  state.remotePresentation.zoom = Math.min(Math.max(Number(event.zoom) || 1, 0.5), 2);
  state.remotePresentation.viewportWidth = Math.max(Number(event.viewport_width) || 0, 0);
  state.remotePresentation.viewportHeight = Math.max(Number(event.viewport_height) || 0, 0);
  state.remotePresentation.cursor = event.cursor || null;
  updatePage(event.page, true);
  if (state.documentZoom !== state.remotePresentation.zoom) {
    state.documentZoom = state.remotePresentation.zoom;
    applyDocumentZoom();
  }
  applyRemoteViewportFit();
  applyDocumentZoom();
  applyPresentationScroll(state.remotePresentation.scroll, state.remotePresentation.scrollX);
  if (!$("#stage").classList.contains("viewer-mode")) setStageMode("viewer");
  if (replacing || action === "start") showStageNotice(`${event.sender_name} демонструє документ`);

  renderRemoteCursor(event.cursor, event.sender_name);
  armRemotePresentationTimeout(sessionId);
}

let noticeTimer;
function showStageNotice(message) {
  const notice = $("#stage-notice");
  notice.textContent = message;
  notice.classList.remove("hidden");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => notice.classList.add("hidden"), 3000);
}

function closeStage({ force = false } = {}) {
  if (state.remotePresentation && !force) {
    toast("Переглядом керує демонстратор");
    return;
  }
  if (state.presenting) stopPresentation();
  clearTimeout(state.remotePresentationTimer);
  state.remotePresentationTimer = null;
  state.remotePresentation = null;
  state.documentLoadId += 1;
  state.activeDocument = null;
  state.documentZoom = 1;
  state.lastPresentationPointer = null;
  state.lastPresentationCursor = null;
  const stage = $("#stage");
  if (stage.contains(document.activeElement)) document.activeElement.blur();
  stage.classList.add("hidden");
  stage.classList.remove("presenter-mode", "viewer-mode");
  $("#presentation-status").classList.add("hidden");
  $("#share-document").classList.remove("hidden", "live");
  $("#share-document").textContent = "Транслювати";
  $("#close-stage").classList.remove("hidden");
  $("#current-page").disabled = false;
  const cursor = $("#remote-cursor");
  cursor.classList.add("hidden");
  cancelAnimationFrame(remoteCursorFrame);
  cursor.style.left = "";
  cursor.style.top = "";
  if (state.documentUrl) URL.revokeObjectURL(state.documentUrl);
  state.documentUrl = null;
  $("#document-viewer").classList.remove("pdf-document");
  $("#document-viewer").replaceChildren();
  refreshPageControls();
  resetViewportPosition();
  requestAnimationFrame(resetViewportPosition);
}

function floatToBase64(samples) {
  // аудіо стискається до 16-бітних pcm-відліків і надсилається малими блоками без очікування завершення запису
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < samples.length; index++) {
    const value = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 8192) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  }
  return btoa(binary);
}

function setAudioButtonState(live) {
  const button = $("#audio-toggle");
  const label = live ? "Зупинити голосову трансляцію" : "Почати голосову трансляцію";
  button.classList.toggle("live", live);
  button.title = label;
  button.setAttribute("aria-label", label);
  $(".audio-label", button).textContent = live ? "В ефірі" : "Говорити";
}

async function toggleAudio() {
  if (state.audio) return stopAudio();
  if (state.selectedRecipients.size === 0) {
    toast("Оберіть хоча б одного одержувача аудіо", "error");
    setPeoplePanelOpen(true);
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    toast("Мікрофон доступний лише через HTTPS або localhost", "error");
    return;
  }
  let stream = null;
  let context = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    context = new AudioCtx();
    await context.resume();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(2048, 1, 1);
    const silent = context.createGain();
    silent.gain.value = 0;
    processor.onaudioprocess = event => {
      if (!state.audio) return;
      const samples = event.inputBuffer.getChannelData(0);
      sendSocket({
        type: "audio_chunk",
        recipients: [...state.selectedRecipients],
        samples: floatToBase64(samples),
        sample_rate: context.sampleRate,
        sequence: state.audioSequence++,
      });
    };
    source.connect(processor);
    processor.connect(silent);
    silent.connect(context.destination);
    state.audio = { stream, context, source, processor, silent };
    setAudioButtonState(true);
    toast(`Аудіо передається ${state.selectedRecipients.size} учасн.`);
  } catch (error) {
    stream?.getTracks().forEach(track => track.stop());
    if (context && context.state !== "closed") await context.close();
    toast(error.name === "NotAllowedError" ? "Надайте браузеру доступ до мікрофона" : error.message, "error");
  }
}

function stopAudio() {
  if (!state.audio) return;
  state.audio.processor.disconnect();
  state.audio.source.disconnect();
  state.audio.silent.disconnect();
  state.audio.stream.getTracks().forEach(track => track.stop());
  state.audio.context.close();
  state.audio = null;
  setAudioButtonState(false);
}

async function playAudioChunk(event) {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    state.playbackContext ||= new AudioCtx();
    const context = state.playbackContext;
    if (context.state === "suspended") await context.resume();
    const binary = atob(event.samples);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
    const int16 = new Int16Array(bytes.buffer);
    const buffer = context.createBuffer(1, int16.length, event.sample_rate);
    const output = buffer.getChannelData(0);
    for (let index = 0; index < int16.length; index++) output[index] = int16[index] / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    const previous = state.playbackTimes.get(event.sender) || context.currentTime;
    const startAt = Math.max(context.currentTime + 0.025, previous);
    source.start(startAt);
    state.playbackTimes.set(event.sender, startAt + buffer.duration);
    $("#typing-status").textContent = `● ${event.sender_name} говорить…`;
    clearTimeout(playAudioChunk.statusTimer);
    playAudioChunk.statusTimer = setTimeout(() => { $("#typing-status").textContent = ""; }, 900);
  } catch (error) {
    console.warn("Audio playback failed", error);
  }
}

let diagnosticHelpReturnFocus = null;

function openShortcuts() {
  $("#shortcuts-modal").classList.remove("hidden");
  $("#close-shortcuts").focus();
}

function closeShortcuts() {
  $("#shortcuts-modal").classList.add("hidden");
}

function openDiagnosticHelp(key, value, trigger) {
  const definition = diagnosticDefinitions[key];
  if (!definition) return;
  diagnosticHelpReturnFocus = trigger;
  $("#diagnostic-help-title").textContent = trigger.dataset.label;
  $("#diagnostic-help-summary").textContent = definition.summary;
  $("#diagnostic-help-value").textContent = String(value);
  $("#diagnostic-help-purpose").textContent = definition.purpose;
  const hint = $("#diagnostic-help-hint");
  hint.textContent = definition.hint || "";
  hint.classList.toggle("hidden", !definition.hint);
  $("#diagnostic-help-modal").classList.remove("hidden");
  $("#close-diagnostic-help").focus();
}

function closeDiagnosticHelp() {
  const modal = $("#diagnostic-help-modal");
  if (modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  if (diagnosticHelpReturnFocus?.isConnected) diagnosticHelpReturnFocus.focus();
  diagnosticHelpReturnFocus = null;
}

function closeSystem() {
  closeDiagnosticHelp();
  $("#system-modal").classList.add("hidden");
}

async function openSystem() {
  try {
    const data = await api("/api/system");
    const items = [
      { key: "node", label: "Вузол", value: data.node_id },
      { key: "process", label: "Процес", value: data.process_id },
      { key: "threads", label: "Активні потоки", value: data.thread_count },
      { key: "workers", label: "Пул працівників", value: data.worker_threads },
      { key: "lamport", label: "Годинник Лампорта", value: data.lamport_clock },
      { key: "peers", label: "Peer-вузли", value: data.peers.length || "—" },
      { key: "messages", label: "Повідомлення", value: data.database.messages },
      { key: "outbox", label: "Outbox", value: data.database.outbox },
      { key: "websocket", label: "WebSocket", value: data.metrics.websocket_connections },
      { key: "audio", label: "Аудіофрагменти", value: data.metrics.audio_chunks },
      { key: "channels", label: "Канали", value: data.database.channels },
      { key: "devices", label: "E2EE-пристрої", value: data.database.device_keys },
      { key: "fingerprint", label: "Відбиток пристрою", value: state.device?.fingerprint || "недоступний" },
    ];
    const grid = $("#system-grid");
    grid.replaceChildren();
    for (const { key, label, value } of items) {
      const item = document.createElement("div");
      item.className = "system-item";
      const span = document.createElement("span");
      span.className = "system-item-label";
      span.textContent = label;
      const valueRow = document.createElement("div");
      valueRow.className = "system-item-value";
      const info = document.createElement("button");
      info.className = "system-info-button";
      info.type = "button";
      info.textContent = "i";
      info.dataset.label = label;
      info.title = `Що означає «${label}»`;
      info.setAttribute("aria-label", `Пояснення показника «${label}»`);
      info.addEventListener("click", () => openDiagnosticHelp(key, value, info));
      const strong = document.createElement("strong"); strong.textContent = value;
      valueRow.append(strong, info);
      item.append(span, valueRow); grid.append(item);
    }
    $("#system-modal").classList.remove("hidden");
  } catch (error) { toast(error.message, "error"); }
}

function bindEvents() {
  $("#auth-form").addEventListener("submit", handleAuth);
  $("#auth-switch").addEventListener("click", () => { registerMode = !registerMode; updateAuthMode(); });
  $("#logout").addEventListener("click", logout);
  $("#message-form").addEventListener("submit", sendMessage);
  $("#message-input").addEventListener("input", resizeComposer);
  $("#message-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit(); }
  });
  $("#open-documents").addEventListener("click", openDocuments);
  $("#open-documents-rail").addEventListener("click", openDocuments);
  $$('[data-close="documents"]').forEach(button => button.addEventListener("click", closeDocuments));
  $("#upload-form").addEventListener("submit", uploadDocument);
  $("#document-file").addEventListener("change", event => {
    const file = event.target.files[0];
    if (!file) return;
    $(".drop-zone strong").textContent = file.name;
  });
  $("#select-all-users").addEventListener("change", selectAllUsers);
  $("#audio-toggle").addEventListener("click", toggleAudio);
  $("#toggle-people").addEventListener("click", () => setPeoplePanelOpen(!state.peoplePanelOpen));
  $("#close-people").addEventListener("click", () => setPeoplePanelOpen(false));
  $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#page-prev").addEventListener("click", () => updatePage(state.page - 1));
  $("#page-next").addEventListener("click", () => updatePage(state.page + 1));
  $("#current-page").addEventListener("change", event => updatePage(event.target.value));
  $("#zoom-out").addEventListener("click", () => setDocumentZoom(state.documentZoom - 0.1));
  $("#zoom-reset").addEventListener("click", () => setDocumentZoom(1));
  $("#zoom-in").addEventListener("click", () => setDocumentZoom(state.documentZoom + 0.1));
  $("#share-document").addEventListener("click", togglePresentation);
  $("#close-stage").addEventListener("click", closeStage);
  $("#presentation-surface").addEventListener("pointermove", trackCursor);
  $("#presentation-surface").addEventListener("pointerleave", clearPresentationCursor);
  $("#presentation-surface").addEventListener("wheel", handlePresentationWheel, { passive: false });
  $("#document-viewer").addEventListener("pointermove", trackCursor);
  $("#document-viewer").addEventListener("pointerleave", clearPresentationCursor);
  $("#document-viewer").addEventListener("wheel", handlePresentationWheel, { passive: false });
  $("#document-viewer").addEventListener("scroll", trackPresentationScroll, { passive: true });
  $("#open-system").addEventListener("click", openSystem);
  $("#open-shortcuts").addEventListener("click", openShortcuts);
  $("#close-shortcuts").addEventListener("click", closeShortcuts);
  $("#close-system").addEventListener("click", closeSystem);
  $("#close-diagnostic-help").addEventListener("click", closeDiagnosticHelp);
  $("#create-channel-button").addEventListener("click", openChannelModal);
  $("#close-channel-modal").addEventListener("click", closeChannelModal);
  $("#channel-form").addEventListener("submit", createChannel);
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.code === "Slash") {
      event.preventDefault();
      openShortcuts();
      return;
    }
    if (event.altKey && !event.ctrlKey && !event.metaKey) {
      const actions = {
        KeyN: openChannelModal,
        KeyO: openDocuments,
        KeyU: () => setPeoplePanelOpen(!state.peoplePanelOpen),
        KeyI: openSystem,
      };
      const action = actions[event.code];
      if (action) {
        event.preventDefault();
        action();
        return;
      }
    }
    if (!$("#stage").classList.contains("hidden") && !state.remotePresentation
      && (event.ctrlKey || event.metaKey) && ["Equal", "Minus", "Digit0", "NumpadAdd", "NumpadSubtract"].includes(event.code)) {
      event.preventDefault();
      if (event.code === "Digit0") setDocumentZoom(1);
      else if (["Equal", "NumpadAdd"].includes(event.code)) setDocumentZoom(state.documentZoom + 0.1);
      else setDocumentZoom(state.documentZoom - 0.1);
      return;
    }
    if (!$("#stage").classList.contains("hidden") && state.presenting) {
      if (["ArrowLeft", "PageUp"].includes(event.key)) {
        event.preventDefault();
        updatePage(state.page - 1);
        return;
      }
      if (["ArrowRight", "PageDown"].includes(event.key)) {
        event.preventDefault();
        updatePage(state.page + 1);
        return;
      }
    }
    if (event.key === "Escape") {
      if (!$("#diagnostic-help-modal").classList.contains("hidden")) {
        closeDiagnosticHelp();
        return;
      }
      if (!$("#shortcuts-modal").classList.contains("hidden")) {
        closeShortcuts();
        return;
      }
      closeDocuments();
      closeSystem();
      closeChannelModal();
      if (!$("#stage").classList.contains("hidden")) closeStage();
    }
  });
  window.addEventListener("resize", () => {
    handlePeoplePanelResize();
    if (state.remotePresentation) {
      applyRemoteViewportFit();
      applyDocumentZoom();
      applyPresentationScroll(state.remotePresentation.scroll, state.remotePresentation.scrollX);
    } else if (state.activeDocument && !$("#stage").classList.contains("hidden")) {
      const vertical = presentationScrollRatio();
      const horizontal = presentationScrollRatio("x");
      applyDocumentZoom();
      applyPresentationScroll(vertical, horizontal);
      if (state.presenting) {
        refreshPresentationCursorAtPointer();
        sendPresentationEvent("update", state.lastPresentationCursor);
      }
    }
    if (state.remotePresentation?.cursor) {
      renderRemoteCursor(state.remotePresentation.cursor, state.remotePresentation.senderName);
    }
  });
  window.addEventListener("focus", markActiveChannelRead);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") markActiveChannelRead();
  });
  window.addEventListener("beforeunload", () => {
    if (state.presenting) sendPresentationEvent("stop", null);
    stopAudio();
  });
  applyPeoplePanelState();
}

bindEvents();
restoreSession();
