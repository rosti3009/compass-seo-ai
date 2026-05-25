const HEBREW_PERSISTENCE_MESSAGE = "הפעולה הסתיימה ונשמרה. חזרה אחורה בדפדפן לא מבטלת אותה.";
const RISK_ORDER = { critical: 4, high: 3, medium: 2, low: 1 };

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function nestedValue(payload, key) {
  if (payload[key] !== undefined && payload[key] !== null) return payload[key];
  for (const container of [payload.metrics, payload.summary, payload.result, payload.data]) {
    if (container && container[key] !== undefined && container[key] !== null) return container[key];
  }
  return undefined;
}

function countFrom(payload, keys) {
  for (const key of keys) {
    const value = nestedValue(payload, key);
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) return Number(value);
  }
  return 0;
}

function valueFrom(payload, keys, fallback = "0") {
  for (const key of keys) {
    const value = nestedValue(payload, key);
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return fallback;
}

function generatedFixes(payload) {
  return asArray(payload.fixes || payload.results || payload.items || []).slice(0, 10);
}

function yesNo(value) {
  return value ? "כן" : "לא";
}

function statusBadge(text, className = "") {
  return `<span class="badge status-badge ${className}">${escapeHtml(text)}</span>`;
}

function diffHtml(oldValue, newValue) {
  const oldText = String(oldValue || "—");
  const newText = String(newValue || "—");
  return `<div class="diff-viewer">
    <section class="diff-pane diff-old"><h3>OLD <span>${oldText.length} תווים</span></h3><pre><mark class="removed-text">${escapeHtml(oldText)}</mark></pre></section>
    <section class="diff-pane diff-new"><h3>NEW <span>${newText.length} תווים</span></h3><pre><mark class="added-text">${escapeHtml(newText)}</mark></pre></section>
  </div>`;
}

function renderFixList(fixes) {
  if (!fixes.length) return "<p class='empty'>לא נוצרו תיקונים להצגה.</p>";
  const items = fixes
    .map((fix) => {
      const url = fix.target_url || fix.page_url || fix.url || fix.target_id || "—";
      const issue = fix.issue_type || fix.fix_type || fix.field_path || "—";
      const pageType = fix.page_type || fix.target_type || "—";
      const risk = fix.risk_level || "—";
      const priority = fix.priority_score ?? "—";
      const oldValue = (fix.preview && fix.preview.old_value) || fix.current_value || "";
      const newValue = (fix.preview && fix.preview.new_value) || fix.proposed_value || "";
      return `<article class="fix-card compact-fix-card">
        <header class="fix-card-header"><div><p class="eyebrow">${escapeHtml(issue)}</p><h3 dir="ltr" class="bidi-isolate">${escapeHtml(url)}</h3></div><div class="badge-row">${statusBadge(risk, risk === "high" || risk === "critical" ? "status-risk" : "")}${statusBadge(pageType)}</div></header>
        <dl class="fix-meta-grid"><div><dt>priority_score</dt><dd>${escapeHtml(priority)}</dd></div><div><dt>mapping verified</dt><dd>${yesNo(fix.publish_mapping_verified)}</dd></div><div><dt>publishable</dt><dd>${yesNo(fix.publishable)}</dd></div></dl>
        <details><summary>Diff preview</summary>${diffHtml(oldValue, newValue)}</details>
      </article>`;
    })
    .join("");
  return `<div class="review-list generated-fixes">${items}</div>`;
}

function resultMetric(label, value) {
  return `<span><strong>${escapeHtml(label)}</strong><b>${escapeHtml(value)}</b></span>`;
}

function renderResult(panel, label, payload, ok) {
  const warnings = asArray(payload.warnings || payload.errors);
  const crawlRun = valueFrom(payload, ["crawl_run_id"], "—");
  const pages = valueFrom(payload, ["pages_crawled", "pages_scanned"], 0);
  const average = valueFrom(payload, ["average_score", "avg_score", "seo_average_score"], 0);
  const created = countFrom(payload, ["created_count", "fixes_generated", "verified_count", "drafts_created"]);
  const duplicates = countFrom(payload, ["duplicates_skipped", "skipped_duplicates"]);
  const pending = valueFrom(payload, ["pending_fixes_count", "pending_count", "pending_fixes"], 0);
  const highRisk = countFrom(payload, ["high_risk_fixes", "high_risk_count"]);
  const publishable = countFrom(payload, ["publishable_fixes", "publishable_count"]);
  const unmapped = countFrom(payload, ["unmapped_fixes", "unmapped_count"]);
  panel.innerHTML = `
    <h2>${ok ? "פעולה הסתיימה בהצלחה" : "הפעולה נכשלה"}: ${escapeHtml(label)}</h2>
    <p class="safe-message">${HEBREW_PERSISTENCE_MESSAGE}</p>
    <div class="result-grid">
      ${resultMetric("תיקונים שנוצרו", created)}
      ${resultMetric("כפילויות שדולגו", duplicates)}
      ${resultMetric("תיקונים בסיכון גבוה", highRisk)}
      ${resultMetric("תיקונים ניתנים לפרסום", publishable)}
      ${resultMetric("תיקונים ללא מיפוי", unmapped)}
      ${resultMetric("crawl_run_id", crawlRun)}
      ${resultMetric("עמודים שנסרקו", pages)}
      ${resultMetric("ציון SEO ממוצע", average)}
      ${resultMetric("ממתין לאישור", pending)}
    </div>
    ${warnings.length ? `<div class="notice error"><strong>אזהרות:</strong><ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul></div>` : ""}
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
  if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
  const targetId = button.dataset.resultTarget || "operation-result";
  const panel = document.getElementById(targetId) || document.getElementById("operation-result");
  const original = button.textContent;
  const label = button.dataset.label || original;
  button.disabled = true;
  button.textContent = `⏳ ${label}`;
  if (panel) panel.innerHTML = `<h2>מריץ פעולה: ${escapeHtml(label)}</h2><p><span class="spinner"></span> נא להמתין...</p>`;
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

async function runEditAction(button) {
  const card = button.closest("[data-fix-card]");
  const textarea = card ? card.querySelector("[data-edit-value]") : null;
  button.dataset.body = JSON.stringify({ proposed_value: textarea ? textarea.value : "" });
  button.dataset.method = "POST";
  await runDashboardAction(button);
}

async function runAssignProductAction(button) {
  const card = button.closest("[data-fix-card]");
  const input = card ? card.querySelector("[data-manual-product-id]") : null;
  button.dataset.body = JSON.stringify({ istore_product_id: input ? input.value.trim() : "" });
  button.dataset.method = "POST";
  await runDashboardAction(button);
}

function applyDiffHighlights(root = document) {
  root.querySelectorAll(".diff-viewer[data-diff-old][data-diff-new]").forEach((viewer) => {
    const oldPre = viewer.querySelector(".diff-old pre");
    const newPre = viewer.querySelector(".diff-new pre");
    if (!oldPre || !newPre || viewer.dataset.diffBound === "true") return;
    viewer.dataset.diffBound = "true";
    oldPre.innerHTML = `<mark class="removed-text">${escapeHtml(viewer.dataset.diffOld || "—")}</mark>`;
    newPre.innerHTML = `<mark class="added-text">${escapeHtml(viewer.dataset.diffNew || "—")}</mark>`;
  });
}

function bindReviewFilters(root = document) {
  const list = root.querySelector("[data-review-list]");
  const controls = root.querySelector("[data-review-filters]");
  if (!list || !controls || controls.dataset.bound === "true") return;
  controls.dataset.bound = "true";
  let page = 1;
  const cards = Array.from(list.querySelectorAll("[data-fix-card]"));
  const summary = root.querySelector("[data-pagination-summary]");
  const pageSizeControl = root.querySelector("[data-page-size]");
  const sortControl = root.querySelector("[data-sort]");

  function matches(card) {
    return Array.from(controls.querySelectorAll("[data-filter]")).every((control) => {
      const key = control.dataset.filter;
      const value = control.value.trim().toLowerCase();
      if (!value) return true;
      if (key === "search") return (card.dataset.search || "").toLowerCase().includes(value);
      const datasetKey = { page_type: "pageType", issue_type: "issueType", risk_level: "riskLevel", publishable: "publishable", mapping_verified: "mappingVerified" }[key];
      return String(card.dataset[datasetKey] || "").toLowerCase() === value;
    });
  }

  function sortCards(filtered) {
    const mode = sortControl ? sortControl.value : "priority_desc";
    return filtered.sort((a, b) => {
      if (mode === "url_asc") return (a.dataset.url || "").localeCompare(b.dataset.url || "");
      if (mode === "issue_asc") return (a.dataset.issueType || "").localeCompare(b.dataset.issueType || "");
      if (mode === "risk_desc") return (RISK_ORDER[b.dataset.riskLevel] || 0) - (RISK_ORDER[a.dataset.riskLevel] || 0);
      return Number(b.dataset.priority || 0) - Number(a.dataset.priority || 0);
    });
  }

  function render() {
    const pageSize = Number(pageSizeControl ? pageSizeControl.value : 10);
    const filtered = sortCards(cards.filter(matches));
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.min(page, totalPages);
    const start = (page - 1) * pageSize;
    const visible = new Set(filtered.slice(start, start + pageSize));
    filtered.forEach((card) => list.appendChild(card));
    cards.forEach((card) => { card.hidden = !visible.has(card); });
    if (summary) summary.textContent = `מציג ${visible.size} מתוך ${filtered.length} תיקונים | עמוד ${page} מתוך ${totalPages}`;
  }

  controls.addEventListener("input", () => { page = 1; render(); });
  if (sortControl) sortControl.addEventListener("change", () => { page = 1; render(); });
  root.querySelector("[data-page-prev]")?.addEventListener("click", () => { page = Math.max(1, page - 1); render(); });
  root.querySelector("[data-page-next]")?.addEventListener("click", () => { page += 1; render(); });
  render();
}

async function runSimpleBulkApprove(button) {
  const fixIds = (button.dataset.bulkIds || "")
    .split(",")
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isInteger(value) && value > 0);
  if (!fixIds.length) return;
  if (!window.confirm("בדקת שהטקסט החדש נשמע נכון ומתאים למוצר?")) return;
  button.dataset.body = JSON.stringify({ fix_ids: fixIds, confirmed: true });
  button.dataset.method = "POST";
  button.dataset.endpoint = "/seo/simple-workspace/bulk-approve";
  button.dataset.label = button.dataset.label || "אישור תיקונים בטוחים";
  await runDashboardAction(button);
}

function renderArticlePreview(card, draft) {
  const container = card.querySelector("[data-preview-container]");
  if (!container) return;
  const imageUrl = draft.generated_image_url || draft.featured_image_url || "";
  const imageAlt = draft.image_alt_text || "";
  const imgHtml = imageUrl ? `<p><img src="${imageUrl}" alt="${imageAlt}"></p>` : "";
  let htmlWithInline = draft.article_body || "";
  if (imageUrl) {
    htmlWithInline = htmlWithInline.replace("[IMAGE_1_HERE]", imgHtml);
  }
  htmlWithInline = htmlWithInline.replace("[IMAGE_2_HERE]", "");
  const htmlNoMarkers = htmlWithInline.replace(/\[IMAGE_[0-9]+_HERE\]/g, "").trim();
  const markers = `${draft.article_body || ""}\n[IMAGE_1_HERE]\n[IMAGE_2_HERE]`;
  const previewHtml = `<article><div>${htmlNoMarkers}</div></article>`;
  container.innerHTML = previewHtml;
  const markerNode = card.querySelector("[data-preview-markers]");
  if (markerNode) markerNode.textContent = markers;
  const previewImageBlock = card.querySelector("[data-preview-image-block]");
  if (previewImageBlock) {
    if (imageUrl) {
      previewImageBlock.hidden = false;
      previewImageBlock.innerHTML = `<img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(imageAlt)}" style="max-width:320px;height:auto"/>`;
    } else {
      previewImageBlock.hidden = true;
      previewImageBlock.innerHTML = "";
    }
  }
  card.dataset.fullHtml = htmlNoMarkers;
  card.dataset.cleanHtml = (draft.article_body || "").replace(/\[IMAGE_[0-9]+_HERE\]/g, "").trim();
  card.dataset.previewHtml = markers;
  card.dataset.markersHtml = markers;
}

async function runManualImageAction(button, action) {
  const card = button.closest("[data-article-id]");
  if (!card) return;
  const feedback = card.querySelector("[data-image-feedback]");
  const draftId = button.dataset.draftId;
  const loadingText = action === "plan" ? "מכין תכנון תמונה..." : "יוצר תמונה...";
  const endpoint = action === "plan" ? `/content/articles/${draftId}/generate-image-plan` : `/content/articles/${draftId}/generate-image`;
  button.disabled = true;
  if (feedback) feedback.textContent = loadingText;
  try {
    const response = await fetch(endpoint, { method: "POST", headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) {
      if (action === "image" && payload && payload.error === "Image provider returned no URL") {
        const diagnosticsText = JSON.stringify(payload.diagnostics || {}, null, 2);
        throw new Error(`Image provider returned no URL
${diagnosticsText}`);
      }
      throw new Error(payload.detail || payload.error || "הפעולה נכשלה");
    }
    const draft = payload.draft || {};
    const generatedImageUrl = draft.generated_image_url
      || payload.generated_image_url
      || draft.featured_image_url
      || payload.featured_image_url
      || payload.open_image_url
      || payload.download_image_url
      || "";
    const imageStatus = draft.featured_image_status || payload.image_status || payload.status || "";
    if (generatedImageUrl && !draft.generated_image_url) draft.generated_image_url = generatedImageUrl;
    if (generatedImageUrl && !draft.featured_image_url) draft.featured_image_url = generatedImageUrl;
    if (imageStatus && !draft.featured_image_status) draft.featured_image_status = imageStatus;
    renderArticlePreview(card, draft);
    const statusNode = card.querySelector("[data-image-status]");
    if (statusNode) statusNode.textContent = imageStatus || (action === "image" ? "generated" : "planned");
    const linksNode = card.querySelector("[data-image-links]");
    if (action === "plan" && feedback) feedback.textContent = "תכנון התמונה עודכן בהצלחה";
    if (action === "image" && feedback) {
      if (!generatedImageUrl) {
        console.warn("Image URL missing from success payload", payload);
        const diagnosticsText = JSON.stringify(payload.diagnostics || {}, null, 2);
        feedback.textContent = `שגיאה: Image provider returned no URL
${diagnosticsText}`;
      } else {
        const openUrl = payload.open_image_url || generatedImageUrl;
        const downloadUrl = payload.download_image_url || generatedImageUrl;
        feedback.innerHTML = `התמונה נוצרה בהצלחה · <a href="${escapeHtml(openUrl)}" target="_blank" rel="noopener">פתח תמונה</a> · <button type="button" class="secondary-button" data-action="copy-image-url" data-image-url="${escapeHtml(generatedImageUrl)}">העתק קישור תמונה</button> · <a href="${escapeHtml(downloadUrl)}" download>הורד תמונה</a>`;
      }
    }
    if (linksNode) {
      if (generatedImageUrl) {
        const openUrl = payload.open_image_url || generatedImageUrl;
        const downloadUrl = payload.download_image_url || generatedImageUrl;
        linksNode.hidden = false;
        linksNode.innerHTML = `<a href="${escapeHtml(openUrl)}" target="_blank" rel="noopener">פתח תמונה</a> · <button type="button" class="secondary-button" data-action="copy-image-url" data-image-url="${escapeHtml(generatedImageUrl)}">העתק קישור תמונה</button> · <a href="${escapeHtml(downloadUrl)}" download>הורד תמונה</a>`;
      } else {
        linksNode.hidden = true;
        linksNode.innerHTML = "";
      }
    }
    bindOperations(card);
  } catch (error) {
    if (feedback) feedback.textContent = `שגיאה: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function bindOperations(root = document) {
  root.querySelectorAll("[data-action='fetch']:not([data-bound='true']), [data-action='confirm-fetch']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runDashboardAction(button));
  });
  root.querySelectorAll("[data-action='edit-fix']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runEditAction(button));
  });
  root.querySelectorAll("[data-action='assign-product']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runAssignProductAction(button));
  });
  root.querySelectorAll("[data-action='bulk-simple-approve']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runSimpleBulkApprove(button));
  });
  root.querySelectorAll("[data-action='copy-text']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const text = button.dataset.copyText || "";
      await navigator.clipboard.writeText(text);
      button.textContent = "הועתק ✓";
      setTimeout(() => { button.textContent = button.dataset.originalLabel || "העתק"; }, 1200);
    });
    button.dataset.originalLabel = button.textContent;
  });
  root.querySelectorAll("[data-action='copy-image-url']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const url = button.dataset.imageUrl || "";
      if (!url) return;
      await navigator.clipboard.writeText(url);
      const original = button.textContent;
      button.textContent = "הועתק ✓";
      setTimeout(() => { button.textContent = original || "העתק קישור תמונה"; }, 1200);
    });
  });
  root.querySelectorAll("[data-action='copy-from-target']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const source = document.getElementById(button.dataset.copyTarget || "");
      if (!source) return;
      await navigator.clipboard.writeText(source.value || source.textContent || "");
      const original = button.textContent;
      button.textContent = "הועתק ✓";
      setTimeout(() => { button.textContent = original; }, 1200);
    });
  });
  root.querySelectorAll("[data-action='generate-image-plan']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runManualImageAction(button, "plan"));
  });
  root.querySelectorAll("[data-action='generate-image']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", () => runManualImageAction(button, "image"));
  });
  root.querySelectorAll("[data-action='copy-html']:not([data-bound='true'])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const card = button.closest("[data-article-id]");
      if (!card) return;
      const key = button.dataset.copyType === "full" ? "fullHtml" : button.dataset.copyType === "clean" ? "cleanHtml" : "markersHtml";
      await navigator.clipboard.writeText(card.dataset[key] || "");
    });
  });
  root.querySelectorAll("[data-article-id]").forEach((card) => {
    if (card.dataset.previewInit === "true") return;
    card.dataset.previewInit = "true";
    const title = card.querySelector("h3")?.textContent || "";
    const body = card.querySelector("details div")?.innerHTML || "";
    renderArticlePreview(card, { title, article_body: body });
  });
  applyDiffHighlights(root);
  bindReviewFilters(root);
}

document.addEventListener("DOMContentLoaded", () => bindOperations());
