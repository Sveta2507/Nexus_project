"use strict";

const methods = new Set(["get", "post", "put", "patch", "delete", "options", "head"]);
let endpoints = [];

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function schemaLabel(schema = {}) {
  if (schema.$ref) return schema.$ref.split("/").pop();
  if (schema.type === "array") return `масив<${schemaLabel(schema.items)}>`;
  if (schema.anyOf) return schema.anyOf.map(schemaLabel).join(" | ");
  if (schema.allOf) return schema.allOf.map(schemaLabel).join(" + ");
  return schema.type || "об’єкт";
}

function addDetail(body, title, values) {
  if (!values.length) return;
  const block = createElement("div", "detail-block");
  block.append(createElement("strong", "", title));
  const chips = createElement("div", "chips");
  values.forEach(value => chips.append(createElement("span", "chip", value)));
  block.append(chips);
  body.append(block);
}

function endpointCard(endpoint) {
  const details = createElement("details", "endpoint");
  const heading = document.createElement("summary");
  heading.append(
    createElement("span", `method ${endpoint.method}`, endpoint.method.toUpperCase()),
    createElement("code", "path", endpoint.path),
    createElement("span", "summary", endpoint.operation.summary || endpoint.operation.description || "Без опису"),
  );
  const body = createElement("div", "endpoint-body");
  if (endpoint.operation.description && endpoint.operation.description !== endpoint.operation.summary) {
    body.append(createElement("p", "", endpoint.operation.description));
  }
  const parameters = [...(endpoint.pathItem.parameters || []), ...(endpoint.operation.parameters || [])].map(parameter => {
    const required = parameter.required ? " · обов’язковий" : "";
    return `${parameter.name} (${parameter.in}, ${schemaLabel(parameter.schema)})${required}`;
  });
  addDetail(body, "Параметри", parameters);
  const content = endpoint.operation.requestBody?.content || {};
  addDetail(body, "Тіло запиту", Object.entries(content).map(([type, value]) => `${type}: ${schemaLabel(value.schema)}`));
  addDetail(body, "Відповіді", Object.entries(endpoint.operation.responses || {}).map(([status, response]) => `${status} — ${response.description || "відповідь"}`));
  if (endpoint.operation.security?.length) addDetail(body, "Доступ", ["Bearer-токен"]);
  details.append(heading, body);
  return details;
}

function render() {
  const query = document.querySelector("#endpoint-filter").value.trim().toLocaleLowerCase("uk");
  const visible = endpoints.filter(endpoint => endpoint.search.includes(query));
  const container = document.querySelector("#endpoint-groups");
  container.replaceChildren();
  const groups = Map.groupBy
    ? Map.groupBy(visible, endpoint => endpoint.tag)
    : visible.reduce((map, endpoint) => map.set(endpoint.tag, [...(map.get(endpoint.tag) || []), endpoint]), new Map());
  for (const [tag, items] of groups) {
    const section = createElement("section", "endpoint-group");
    section.append(createElement("h2", "", tag));
    const list = createElement("div", "endpoint-list");
    items.forEach(endpoint => list.append(endpointCard(endpoint)));
    section.append(list);
    container.append(section);
  }
  const status = document.querySelector("#docs-status");
  status.textContent = visible.length ? "" : "Маршрутів за цим запитом не знайдено.";
  status.hidden = Boolean(visible.length);
}

async function loadDocumentation() {
  const status = document.querySelector("#docs-status");
  try {
    const response = await fetch("/openapi.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const schema = await response.json();
    document.querySelector("#api-title").textContent = schema.info?.title || "Nexus API";
    document.querySelector("#api-version").textContent = `Версія ${schema.info?.version || "—"}`;
    document.querySelector("#api-description").textContent = schema.info?.description || "Локальна OpenAPI-документація Nexus.";
    for (const [path, pathItem] of Object.entries(schema.paths || {})) {
      for (const [method, operation] of Object.entries(pathItem)) {
        if (!methods.has(method)) continue;
        const tag = operation.tags?.[0] || path.split("/").filter(Boolean)[1] || "Інше";
        endpoints.push({
          path,
          pathItem,
          method,
          operation,
          tag,
          search: `${method} ${path} ${tag} ${operation.summary || ""} ${operation.description || ""}`.toLocaleLowerCase("uk"),
        });
      }
    }
    document.querySelector("#endpoint-count").textContent = `${endpoints.length} маршрутів`;
    render();
  } catch (error) {
    status.hidden = false;
    status.classList.add("error");
    status.textContent = `Не вдалося завантажити OpenAPI-схему: ${error.message}`;
  }
}

document.querySelector("#endpoint-filter").addEventListener("input", render);
loadDocumentation();
