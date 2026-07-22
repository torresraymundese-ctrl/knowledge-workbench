"use strict";

const statusLabels = {
  draft: "草稿",
  reviewing: "审核中",
  verified: "已验证",
  conflicted: "有冲突",
  deprecated: "已弃用",
  archived: "已归档",
};

const eventLabels = {
  document_ingested: "资料已导入",
  document_reprocessed: "资料已重处理",
  evidence_status_changed: "证据状态已变更",
  wiki_revision_submitted: "Wiki 修订已提交",
  wiki_revision_published: "Wiki 修订已发布",
  potential_conflict_queued: "发现潜在冲突",
  conflict_status_changed: "冲突状态已变更",
  labeling_session_created: "黄金标注已创建",
  labeling_session_approved: "黄金标注已批准",
  labeling_dataset_exported: "黄金评测集已导出",
  labeling_duplicate_threshold_changed: "评测阈值已调整",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function shortId(value) {
  if (!value) return "—";
  return value.length > 18 ? `${value.slice(0, 12)}…` : value;
}

function formatTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function metricCard(label, value, sub, icon) {
  const card = element("article", "metric-card");
  const labelRow = element("div", "label");
  labelRow.append(element("span", "", label), element("span", "mini-icon", icon));
  card.append(labelRow, element("div", "value", formatNumber(value)), element("div", "sub", sub));
  return card;
}

function renderSummary(summary) {
  const metrics = document.querySelector("#metrics");
  metrics.replaceChildren(
    metricCard("当前资料", summary.document_count, "内容寻址版本", "文"),
    metricCard("原子证据", summary.current_evidence_count, "仅当前处理运行", "证"),
    metricCard("Wiki 页面", summary.wiki_page_count, "SQLite 状态真相", "知"),
    metricCard("批准评测集", summary.approved_labeling_session_count, "双人复核完成", "评"),
  );

  const statuses = document.querySelector("#evidence-status");
  statuses.replaceChildren();
  const total = Math.max(summary.current_evidence_count, 1);
  Object.entries(summary.evidence_by_status).sort((a, b) => b[1] - a[1]).forEach(([name, count]) => {
    const row = element("div", "status-row");
    const bar = element("div", "bar");
    const fill = element("span");
    fill.style.width = `${Math.max((count / total) * 100, 1)}%`;
    bar.append(fill);
    row.append(element("span", "", statusLabels[name] || name), bar, element("strong", "", formatNumber(count)));
    statuses.append(row);
  });

  const attention = document.querySelector("#attention");
  attention.replaceChildren();
  [
    [summary.active_conflict_count, "待处理冲突"],
    [summary.active_task_count, "活动任务"],
    [summary.needs_revalidation_count, "待重验证页面"],
    [summary.evidence_by_status.reviewing || 0, "审核中证据"],
  ].forEach(([value, label]) => {
    const item = element("div", "attention-item");
    item.append(element("strong", "", formatNumber(value)), element("span", "", label));
    attention.append(item);
  });
}

function renderDocuments(documents) {
  document.querySelector("#document-count").textContent = `${documents.total} 份`;
  const body = document.querySelector("#documents-body");
  body.replaceChildren();
  documents.items.forEach((item) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.append(element("div", "document-name", item.display_name), element("span", "document-id", shortId(item.document_id)));
    const classCell = document.createElement("td");
    classCell.append(element("span", `tag ${item.classification}`, item.classification));
    const wikiClass = item.needs_revalidation ? "state warn" : item.wiki_status === "verified" ? "state ok" : "state";
    row.append(
      nameCell,
      classCell,
      element("td", "state", item.parser),
      element("td", "", formatNumber(item.evidence_count)),
      element("td", item.reviewing_evidence_count ? "state warn" : "state", item.reviewing_evidence_count ? `${item.reviewing_evidence_count} 待审` : "—"),
      element("td", wikiClass, item.needs_revalidation ? "待重验证" : statusLabels[item.wiki_status] || item.wiki_status || "无"),
    );
    body.append(row);
  });
}

function queueColumn(title, items, describe) {
  const column = element("article", "review-column");
  const header = document.createElement("header");
  header.append(element("h3", "", title), element("span", "", `${items.length} 项`));
  const list = element("div", "queue-list");
  if (!items.length) {
    list.append(element("div", "empty", "当前没有待处理项"));
  } else {
    items.slice(0, 5).forEach((item) => {
      const card = element("div", "queue-item");
      const [primary, secondary] = describe(item);
      card.append(element("strong", "", primary), element("small", "", secondary));
      list.append(card);
    });
  }
  column.append(header, list);
  return column;
}

function renderReviews(queue) {
  const columns = document.querySelector("#review-columns");
  columns.replaceChildren(
    queueColumn("原子证据", queue.evidence, (item) => [item.document_name, `${statusLabels[item.status] || item.status} · #${item.ordinal}`]),
    queueColumn("潜在冲突", queue.conflicts, (item) => [item.document_name, `${item.conflict_type} · ${item.status}`]),
    queueColumn("Wiki 修订", queue.wiki_revisions, (item) => [item.page_title, `修订 ${item.revision_number} · ${statusLabels[item.status] || item.status}`]),
  );
}

function renderEvaluations(reports) {
  const root = document.querySelector("#evaluations");
  root.replaceChildren();
  if (!reports.length) {
    root.append(element("div", "panel empty", "尚无可展示的评测报告"));
    return;
  }
  reports.forEach((report) => {
    const card = element("article", "evaluation-card");
    const header = document.createElement("header");
    header.append(element("h3", "", report.dataset_name || report.file_name), element("span", "tag", `v${report.schema_version || "?"}`));
    const passRate = report.metrics.pass_rate;
    const precision = report.metrics.precision ?? report.metrics.citation_precision;
    const scoreValue = passRate ?? precision;
    const score = scoreValue === undefined ? "已完成" : `${Math.round(scoreValue * 100)}%`;
    const scoreNode = element("div", scoreValue === 1 ? "score good" : "score", score);
    const cases = report.metrics.case_count === undefined ? "" : `${report.metrics.case_count} 个用例 · `;
    card.append(header, scoreNode, element("div", "evaluation-meta", `${cases}${formatTime(report.evaluated_at)}`));
    root.append(card);
  });
}

function renderActivity(items) {
  const list = document.querySelector("#activity-list");
  list.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("li");
    row.append(
      element("time", "", formatTime(item.created_at)),
      element("span", "", `${eventLabels[item.event_type] || item.event_type} · ${shortId(item.entity_id)}`),
      element("span", "actor", item.actor),
    );
    list.append(row);
  });
}

async function loadDashboard() {
  const refresh = document.querySelector("#refresh");
  const sync = document.querySelector("#sync-state");
  const error = document.querySelector("#error-banner");
  refresh.disabled = true;
  sync.classList.remove("ready");
  sync.lastChild.textContent = "正在读取";
  error.hidden = true;
  try {
    const response = await fetch("/api/v1/bootstrap", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`);
    const data = await response.json();
    renderSummary(data.summary);
    renderDocuments(data.documents);
    renderReviews(data.review_queue);
    renderEvaluations(data.evaluations);
    renderActivity(data.activity);
    sync.classList.add("ready");
    sync.lastChild.textContent = `已同步 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  } catch (cause) {
    error.textContent = cause instanceof Error ? cause.message : "工作台读取失败";
    error.hidden = false;
    sync.lastChild.textContent = "读取失败";
  } finally {
    refresh.disabled = false;
  }
}

document.querySelector("#refresh").addEventListener("click", loadDashboard);
document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((entry) => entry.classList.remove("active"));
    item.classList.add("active");
  });
});
loadDashboard();
