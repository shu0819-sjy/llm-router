(() => {
  const TOKEN_KEY = "llm_router_admin_token";

  function token() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }

  function setStatus(msg, isError) {
    const el = document.getElementById("authStatus");
    el.textContent = msg || "";
    el.style.color = isError ? "#f07178" : "";
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    const t = token();
    if (t) headers.Authorization = "Bearer " + t;
    const res = await fetch(path, { ...opts, headers });
    let body = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) body = await res.json();
    else body = await res.text();
    if (!res.ok) {
      const msg = (body && body.detail && body.detail.error && body.detail.error.message)
        || (body && body.detail)
        || res.statusText;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return body;
  }

  async function loadKeys() {
    const data = await api("/panel/api/keys");
    const tb = document.getElementById("keysBody");
    tb.innerHTML = "";
    for (const k of data.keys || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${k.id}</td>
        <td>${escapeHtml(k.name)}</td>
        <td><code>${escapeHtml(k.key_prefix)}</code></td>
        <td>${escapeHtml(k.provider_id || "—")}</td>
        <td>${k.is_active ? "yes" : "no"}</td>
        <td>${k.is_active ? `<button class="danger" data-del="${k.id}">Deactivate</button>` : ""}</td>`;
      tb.appendChild(tr);
    }
    tb.querySelectorAll("button[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api("/panel/api/keys/" + btn.dataset.del, { method: "DELETE" });
        await loadKeys();
      });
    });
  }

  async function loadProviders() {
    const data = await api("/panel/api/providers");
    const tb = document.getElementById("providersBody");
    tb.innerHTML = "";
    for (const p of data.providers || []) {
      const tr = document.createElement("tr");
      const prefixes = (p.supported_prefixes || []).join(", ");
      tr.innerHTML = `
        <td>${escapeHtml(p.id)}</td>
        <td>${p.enabled ? "yes" : "no"}</td>
        <td><span class="pill ${escapeHtml(p.circuit)}">${escapeHtml(p.circuit)}</span></td>
        <td><code>${escapeHtml(p.base_url || "")}</code></td>
        <td>${escapeHtml(prefixes)}</td>
        <td><button data-toggle="${escapeHtml(p.id)}" data-enabled="${p.enabled ? "1" : "0"}">
          ${p.enabled ? "Disable" : "Enable"}
        </button></td>`;
      tb.appendChild(tr);
    }
    tb.querySelectorAll("button[data-toggle]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const enabled = btn.dataset.enabled !== "1";
        await api("/panel/api/providers/" + encodeURIComponent(btn.dataset.toggle), {
          method: "PATCH",
          body: JSON.stringify({ enabled }),
        });
        await loadProviders();
      });
    });
  }

  async function loadUsage() {
    const summary = await api("/panel/api/usage");
    const s = summary.summary || {};
    document.getElementById("usageSummary").innerHTML = `
      <div class="stat"><span class="muted">Requests</span><strong>${s.requests || 0}</strong></div>
      <div class="stat"><span class="muted">Total tokens</span><strong>${s.total_tokens || 0}</strong></div>
      <div class="stat"><span class="muted">Cost USD</span><strong>${Number(s.cost_usd || 0).toFixed(6)}</strong></div>`;

    const recent = await api("/panel/api/usage/recent?limit=50");
    const tb = document.getElementById("usageBody");
    tb.innerHTML = "";
    for (const e of recent.events || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${e.id}</td>
        <td>${e.api_key_id ?? "—"}</td>
        <td>${escapeHtml(e.provider_id)}</td>
        <td>${escapeHtml(e.model)}</td>
        <td>${e.total_tokens}</td>
        <td>${Number(e.cost_usd).toFixed(6)}</td>
        <td>${escapeHtml(e.status)}</td>
        <td>${escapeHtml(e.created_at || "")}</td>`;
      tb.appendChild(tr);
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refreshAll() {
    try {
      await Promise.all([loadKeys(), loadProviders(), loadUsage()]);
      setStatus("Loaded " + new Date().toLocaleTimeString());
    } catch (err) {
      setStatus(String(err.message || err), true);
    }
  }

  document.getElementById("saveToken").addEventListener("click", () => {
    const v = document.getElementById("adminToken").value.trim();
    localStorage.setItem(TOKEN_KEY, v);
    setStatus(v ? "Token saved locally (not sent to HTML templates)." : "Token cleared.");
  });
  document.getElementById("refreshAll").addEventListener("click", refreshAll);
  document.getElementById("createKeyForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    try {
      const created = await api("/panel/api/keys", {
        method: "POST",
        body: JSON.stringify({
          name: fd.get("name"),
          provider_id: fd.get("provider_id") || null,
        }),
      });
      document.getElementById("createdKeyOnce").textContent =
        "New key (copy now): " + created.key + "\n" + (created.warning || "");
      ev.target.reset();
      await loadKeys();
    } catch (err) {
      setStatus(String(err.message || err), true);
    }
  });

  const existing = token();
  if (existing) document.getElementById("adminToken").value = existing;
  refreshAll();
})();
