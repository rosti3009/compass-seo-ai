const HEBREW_PERSISTENCE_MESSAGE = "הפעולה הסתיימה ונשמרה. חזרה אחורה בדפדפן לא מבטלת אותה.";

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function countFrom(payload, keys) {
  for (const key of keys) {
    if (typeof payload[key] === "number") return payload[key];
  }
  return 0;
}

function valueFrom(payload, keys) {
  for (const key of keys) {
    if (payload[key] !== undefined && payload[key] !== null) return payload[key];
  }
  return "—";
}

function generatedFixes(payload) {
  return asArray(payload.fixes || payload.results || []).slice(0, 10);
}

function renderFixList(fixes) {
  if (!fixes.length) return "<p class='empty'>לא נוצרו תיקונים להצגה.</p>";
  const items = fixes
    .map((fix) => {
      const url = fix.target_url || fix.page_url || fix.url || fix.target_id || "—";
      const issue = fix.issue_type || fix.fix_type || fix.field_path || "—";
      const newValue = (fix.preview && fix.preview.new_value) || fix.proposed_value || "";
      return `<li><strong>${issue}</strong> <span dir="ltr">${url}</span><br><small>${String(newValue).slice(0, 180)}</small></li>`;
    })
    .join("");
  return `<ol>${items}</ol>`;
}

function renderResult(panel, label, payload, ok) {
  const warnings = asArray(payload.warnings || payload.errors);
  const crawlRun = valueFrom(payload, ["crawl_run_id"]);
  const pages = valueFrom(payload, ["pages_crawled", "pages_scanned"]);
  const average = valueFrom(payload, ["average_score"]);
  const created = countFrom(payload, ["created_count", "fixes_generated", "verified_count"]);
  const duplicates = countFrom(payload, ["duplicates_skipped"]);
  const pending = valueFrom(payload, ["pending_fixes_count", "pending_count"]);
  panel.innerHTML = `
    <h2>${ok ? "פעולה הסתיימה בהצלחה" : "הפעולה נכשלה"}: ${label}</h2>
    <p class="safe-message">${HEBREW_PERSISTENCE_MESSAGE}</p>
    <div class="result-grid">
      <span><strong>created count:</strong> ${created}</span>
      <span><strong>skipped duplicates:</strong> ${duplicates}</span>
      <span><strong>crawl_run_id:</strong> ${crawlRun}</span>
      <span><strong>pages_crawled:</strong> ${pages}</span>
      <span><strong>average_score:</strong> ${average}</span>
      <span><strong>pending fixes count:</strong> ${pending}</span>
    </div>
    ${warnings.length ? `<div class="notice error"><strong>warnings/errors:</strong><ul>${warnings.map((warning) => `<li>${warning}</li>`).join("")}</ul></div>` : ""}
    <h3>Top 10 generated fixes</h3>
    ${renderFixList(generatedFixes(payload))}
    <div class="actions">
      <a class="button-link" href="/crawler/results-view/latest">Open full results</a>
      <a class="button-link" href="/seo/fixes/pending-view">Open pending fixes</a>
      <a class="button-link" href="/integrations/istore/seo-approvals-view">Open approvals</a>
      <button type="button" class="secondary-button" data-action="fetch" data-endpoint="/seo/fixes/generate-from-latest-crawl" data-method="POST" data-body='{"dry_run":false,"limit":50}' data-label="Run next step">Run next step</button>
    </div>
  `;
  bindOperations(panel);
}

async function runDashboardAction(button) {
  const targetId = button.dataset.resultTarget || "operation-result";
  const panel = document.getElementById(targetId) || document.getElementById("operation-result");
  const original = button.textContent;
  const label = button.dataset.label || original;
  button.disabled = true;
  button.textContent = `⏳ ${label}`;
  if (panel) panel.innerHTML = `<h2>מריץ פעולה: ${label}</h2><p><span class="spinner"></span> נא להמתין...</p>`;
  try {
    const headers = { Accept: "application/json" };
    const options = { method: button.dataset.method || "POST", headers };
    if (button.dataset.body) {
      headers["Content-Type"] = "application/json";
      options.body = button.dataset.body;
    }
    const response = await fetch(button.dataset.endpoint, options);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : { message: await response.text() };
    if (!response.ok) {
      const detail = payload.detail || payload.message || `HTTP ${response.status}`;
      renderResult(panel, label, { ...payload, errors: [typeof detail === "string" ? detail : JSON.stringify(detail)] }, false);
      return;
    }
    renderResult(panel, label, payload, true);
  } catch (error) {
    renderResult(panel, label, { errors: [error.message] }, false);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function bindOperations(root = document) {
  root.querySelectorAll("[data-action='fetch']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runDashboardAction(button));
  });
}

document.addEventListener("DOMContentLoaded", () => bindOperations());
