"use strict";

const statusLabels = {
  draft: "草稿",
  reviewing: "审核中",
  verified: "已验证",
  rejected: "已驳回",
  conflicted: "有冲突",
  pending: "待处理",
  deprecated: "已弃用",
  archived: "已归档",
  merged: "已合并",
};

const eventLabels = {
  document_ingested: "资料已导入",
  document_reprocessed: "资料已重处理",
  evidence_status_changed: "证据状态已变更",
  wiki_revision_submitted: "Wiki 修订已提交",
  wiki_revision_rejected: "Wiki 修订已驳回",
  wiki_revision_published: "Wiki 修订已发布",
  potential_conflict_queued: "发现潜在冲突",
  conflict_status_changed: "冲突状态已变更",
  conflict_candidate_pack_created: "跨文档冲突候选包已生成",
  conflict_candidate_annotations_submitted: "跨文档冲突标签已提交",
  conflict_candidate_label_updated: "跨文档冲突标签已更新",
  conflict_candidate_review_updated: "跨文档冲突复核已更新",
  conflict_dataset_finalized: "跨文档冲突数据集已固化",
  entity_candidates_imported: "实体候选已导入",
  entity_candidate_accepted: "实体候选已接受",
  entity_candidate_rejected: "实体候选已驳回",
  entity_alias_added: "实体别名已登记",
  evidence_entity_linked: "证据已关联实体",
  entity_merge_proposed: "实体合并已提议",
  entity_merge_rejected: "实体合并已驳回",
  entity_merge_approved: "实体合并已批准",
  canonical_entities_merged: "规范实体已合并",
  entity_relation_type_created: "业务关系类型已登记",
  entity_relationship_created: "业务关系已登记",
  entity_relationship_retracted: "业务关系已撤销",
  graph_pilot_pack_created: "图谱试点证据包已生成",
  worker_started: "后台工作器已启动",
  worker_stopped: "后台工作器已停止",
  labeling_session_created: "黄金标注已创建",
  labeling_session_approved: "黄金标注已批准",
  labeling_dataset_exported: "黄金评测集已导出",
  labeling_duplicate_threshold_changed: "评测阈值已调整",
};

let csrfToken = "";
const reviewKinds = ["evidence", "conflicts", "wiki_revisions"];
const reviewState = {
  limit: 5,
  offsets: { evidence: 0, conflicts: 0, wiki_revisions: 0 },
  historyOffset: 0,
  filters: {
    query: "",
    classification: "",
    statuses: { evidence: "", conflicts: "", wiki_revisions: "" },
  },
};
const candidateState = {
  limit: 10,
  offset: 0,
  packId: "",
  planId: "",
  batchId: "",
  plans: [],
  invalidPlanCount: 0,
  pageRequestSerial: 0,
  planRequestSerial: 0,
  state: "all",
  query: "",
  contentSha256: "",
};
const graphPilotState = {
  limit: 10,
  offset: 0,
  packId: "",
  status: "all",
  query: "",
};
const entityCandidateState = {
  limit: 10,
  offset: 0,
  status: "pending",
  query: "",
};
const entityMergeState = {
  limit: 10,
  offset: 0,
  query: "",
  entityOptions: [],
};
const entityRelationshipState = {
  limit: 10,
  offset: 0,
  status: "active",
  query: "",
  entityOptions: [],
  relationTypes: [],
  evidenceItems: [],
};
const candidatePhaseLabels = {
  labeling: "标注中",
  reviewing: "复核中",
  reviewed: "复核完成",
  invalid: "完整性异常",
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

function queueColumn(title, page, describe) {
  const items = page.items;
  const column = element("article", "review-column");
  const header = document.createElement("header");
  const rangeStart = page.total ? page.offset + 1 : 0;
  const rangeEnd = page.offset + items.length;
  header.append(element("h3", "", title), element("span", "", `${rangeStart}-${rangeEnd}/${page.total} 项`));
  const list = element("div", "queue-list");
  if (!items.length) {
    list.append(element("div", "empty", "当前没有待处理项"));
  } else {
    items.forEach((item) => {
      const card = element("div", "queue-item");
      const [primary, secondary] = describe(item);
      card.append(element("strong", "", primary), element("small", "", secondary));
      list.append(card);
    });
  }
  const pagination = element("div", "queue-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    reviewState.offsets[page.kind] = Math.max(0, page.offset - page.limit);
    await refreshReviewQueues();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    reviewState.offsets[page.kind] = page.offset + page.limit;
    await refreshReviewQueues();
  });
  next.disabled = !page.has_next;
  pagination.append(previous, element("span", "", `第 ${pageNumber}/${pageCount} 页`), next);
  column.append(header, list, pagination);
  return column;
}

function button(label, className, action) {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function actorValue() {
  const actor = document.querySelector("#actor").value.trim();
  if (!actor) {
    showBanner("请先填写操作者，例如 reviewer-01");
    document.querySelector("#actor").focus();
    throw new Error("actor-required");
  }
  return actor;
}

function showBanner(message, success = false) {
  const banner = document.querySelector("#error-banner");
  banner.textContent = message;
  banner.classList.toggle("success", success);
  banner.hidden = false;
}

async function postTransition(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Workbench-CSRF": csrfToken,
    },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `操作失败（HTTP ${response.status}）`);
  return result;
}

async function transitionEvidence(evidenceId, target, label) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  if (!window.confirm(`确认${label}？该操作将以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/evidence/${encodeURIComponent(evidenceId)}/transition`, { target, actor });
    document.querySelector("#evidence-dialog").close();
    await loadDashboard();
    showBanner(`${label}完成，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "证据状态变更失败");
  }
}

async function openEvidence(evidenceId) {
  try {
    const response = await fetch(`/api/v1/evidence/${encodeURIComponent(evidenceId)}`, { headers: { Accept: "application/json" } });
    const detail = await response.json();
    if (!response.ok) throw new Error(detail.error || "无法读取证据详情");
    document.querySelector("#evidence-dialog-title").textContent = detail.document_name;
    document.querySelector("#evidence-dialog-meta").textContent = `${detail.classification} · #${detail.ordinal} · ${statusLabels[detail.status] || detail.status}`;
    document.querySelector("#evidence-dialog-excerpt").textContent = detail.excerpt;
    document.querySelector("#evidence-dialog-locator").textContent = JSON.stringify(
      detail.locators || [detail.locator]
    );
    const actions = document.querySelector("#evidence-dialog-actions");
    actions.replaceChildren();
    const definitions = {
      draft: [["提交审核", "reviewing", "primary"]],
      reviewing: [["审核通过", "verified", "primary"], ["退回草稿", "draft", ""], ["标记冲突", "conflicted", "danger"]],
      conflicted: [["重新进入审核", "reviewing", "primary"]],
    };
    (definitions[detail.status] || []).forEach(([label, target, className]) => {
      actions.append(button(label, className, () => transitionEvidence(detail.evidence_id, target, label)));
    });
    document.querySelector("#evidence-dialog").showModal();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "无法读取证据详情");
  }
}

async function transitionConflict(conflictId, target, label, noteInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const note = noteInput.value.trim();
  if (target === "resolved" && !note) {
    showBanner("解决冲突必须填写处理说明");
    noteInput.focus();
    return;
  }
  if (!window.confirm(`确认${label}？该操作将以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/conflicts/${encodeURIComponent(conflictId)}/transition`, { target, actor, note });
    await loadDashboard();
    showBanner(`${label}完成，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "冲突状态变更失败");
  }
}

async function submitRevisionReview(revisionId) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  if (!window.confirm(`确认提交 Wiki 修订复核？该操作将以 ${actor} 写入审计日志，且提交后不能继续修改草稿链接。`)) return;
  try {
    await postTransition(`/api/v1/wiki-revisions/${encodeURIComponent(revisionId)}/submit-review`, { actor });
    document.querySelector("#revision-dialog").close();
    await loadDashboard();
    showBanner(`Wiki 修订已提交复核，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "Wiki 修订提交复核失败");
  }
}

async function rejectRevisionReview(revisionId, noteInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const note = noteInput.value.trim();
  if (!note) {
    showBanner("驳回 Wiki 修订必须填写复核意见");
    noteInput.focus();
    return;
  }
  if (!window.confirm(`确认驳回 Wiki 修订？该决定和复核意见将以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/wiki-revisions/${encodeURIComponent(revisionId)}/reject`, { actor, note });
    document.querySelector("#revision-dialog").close();
    await loadDashboard();
    showBanner(`Wiki 修订已驳回，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "Wiki 修订驳回失败");
  }
}

async function publishRevision(revisionId, confirmationInput, expectedPhrase) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const confirmation = confirmationInput.value.trim();
  if (confirmation !== expectedPhrase) {
    showBanner(`正式发布前必须完整输入：${expectedPhrase}`);
    confirmationInput.focus();
    return;
  }
  if (!window.confirm(`确认正式发布该 Wiki 修订？发布后会立即替代旧正式修订，并以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/wiki-revisions/${encodeURIComponent(revisionId)}/publish`, { actor, confirmation });
    document.querySelector("#revision-dialog").close();
    await loadDashboard();
    showBanner(`Wiki 修订已正式发布，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "Wiki 修订正式发布失败");
  }
}

async function openRevision(revisionId) {
  try {
    const response = await fetch(`/api/v1/wiki-revisions/${encodeURIComponent(revisionId)}`, { headers: { Accept: "application/json" } });
    const detail = await response.json();
    if (!response.ok) throw new Error(detail.error || "无法读取 Wiki 修订详情");
    document.querySelector("#revision-dialog-title").textContent = detail.page_title;
    document.querySelector("#revision-dialog-meta").textContent = `${detail.classification} · 修订 ${detail.revision_number} · ${statusLabels[detail.status] || detail.status} · ${detail.generator}`;
    const evidenceSummary = Object.entries(detail.evidence_by_status)
      .map(([status, count]) => `${statusLabels[status] || status} ${count}`)
      .join("，");
    document.querySelector("#revision-dialog-evidence").textContent = `引用证据 ${detail.evidence_count} 条${evidenceSummary ? `（${evidenceSummary}）` : ""}`;
    document.querySelector("#revision-dialog-content").textContent = detail.content_preview;
    const notice = document.querySelector("#revision-dialog-notice");
    if (detail.content_truncated) {
      notice.textContent = `页面内容共 ${formatNumber(detail.content_length)} 个字符，Web 仅显示前 ${formatNumber(detail.content_preview.length)} 个字符；提交前请在 Obsidian 或 CLI 核对全文。`;
      notice.hidden = false;
    } else if (detail.status === "reviewing") {
      notice.textContent = detail.can_publish
        ? "该修订已满足正式发布条件；发布会立即更新当前正式知识，请再次核对全文和引用。"
        : "该修订正在复核，但尚未满足正式发布条件。";
      notice.hidden = false;
    } else {
      notice.hidden = true;
      notice.textContent = "";
    }
    const actions = document.querySelector("#revision-dialog-actions");
    actions.replaceChildren();
    if (detail.can_submit_review) {
      actions.append(button("提交复核", "primary", () => submitRevisionReview(detail.revision_id)));
    } else if (detail.status === "reviewing") {
      const note = element("textarea", "revision-review-note");
      note.maxLength = 2000;
      note.rows = 3;
      note.placeholder = "复核意见（驳回时必填）";
      actions.append(note, button("驳回修订", "danger", () => rejectRevisionReview(detail.revision_id, note)));

      const publishPanel = element("section", "revision-publish-panel");
      publishPanel.append(element("strong", "", "正式发布"));
      if (detail.can_publish) {
        const instruction = element("p", "", "发布后将立即成为当前正式知识。请输入以下短语确认：");
        const phrase = element("code", "publish-phrase", detail.publish_confirmation_phrase);
        const confirmation = element("input", "revision-publish-confirmation");
        confirmation.type = "text";
        confirmation.autocomplete = "off";
        confirmation.placeholder = detail.publish_confirmation_phrase;
        confirmation.setAttribute("aria-label", "正式发布确认短语");
        publishPanel.append(
          instruction,
          phrase,
          confirmation,
          button("正式发布", "publish", () => publishRevision(
            detail.revision_id,
            confirmation,
            detail.publish_confirmation_phrase,
          )),
        );
      } else {
        const blockers = Array.isArray(detail.publish_blockers) ? detail.publish_blockers.join("；") : "发布条件未满足";
        publishPanel.append(element("p", "publish-blockers", blockers));
      }
      actions.append(publishPanel);
    }
    document.querySelector("#revision-dialog").showModal();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "无法读取 Wiki 修订详情");
  }
}

function renderReviews(queue) {
  const columns = document.querySelector("#review-columns");
  const evidenceColumn = queueColumn("原子证据", queue.evidence, (item) => [item.document_name, `${statusLabels[item.status] || item.status} · #${item.ordinal}`]);
  evidenceColumn.querySelectorAll(".queue-item").forEach((card, index) => {
    const item = queue.evidence.items[index];
    if (!item) return;
    const actions = element("div", "queue-actions");
    if (item.classification === "restricted") {
      actions.append(element("small", "", "请回原文件并通过 CLI 审核"));
    } else {
      actions.append(button("查看并审核", "primary", () => openEvidence(item.evidence_id)));
    }
    card.append(actions);
  });

  const conflictColumn = queueColumn("潜在冲突", queue.conflicts, (item) => [item.document_name, `${item.conflict_type} · ${statusLabels[item.status] || item.status}`]);
  conflictColumn.querySelectorAll(".queue-item").forEach((card, index) => {
    const item = queue.conflicts.items[index];
    if (!item) return;
    const note = element("input", "queue-note");
    note.type = "text";
    note.maxLength = 2000;
    note.placeholder = "处理说明（解决时必填）";
    card.append(note);
    const actions = element("div", "queue-actions");
    if (item.classification === "restricted") {
      actions.append(element("small", "", "restricted 冲突仅允许 CLI 处理"));
    } else if (item.status === "pending") {
      actions.append(
        button("开始处理", "primary", () => transitionConflict(item.conflict_id, "reviewing", "开始处理", note)),
        button("排除", "danger", () => transitionConflict(item.conflict_id, "dismissed", "排除冲突", note)),
      );
    } else if (item.status === "reviewing") {
      actions.append(
        button("标记已解决", "primary", () => transitionConflict(item.conflict_id, "resolved", "解决冲突", note)),
        button("排除", "danger", () => transitionConflict(item.conflict_id, "dismissed", "排除冲突", note)),
      );
    }
    card.append(actions);
  });

  const wikiColumn = queueColumn("Wiki 修订", queue.wiki_revisions, (item) => [item.page_title, `修订 ${item.revision_number} · ${statusLabels[item.status] || item.status}`]);
  wikiColumn.querySelectorAll(".queue-item").forEach((card, index) => {
    const item = queue.wiki_revisions.items[index];
    if (!item) return;
    const actions = element("div", "queue-actions");
    if (item.classification === "restricted") {
      actions.append(element("small", "", "restricted 修订仅允许 CLI 复核"));
    } else {
      actions.append(button("查看修订", "primary", () => openRevision(item.revision_id)));
    }
    card.append(actions);
  });
  columns.replaceChildren(evidenceColumn, conflictColumn, wikiColumn);
}

function renderRejectedHistory(page) {
  document.querySelector("#rejected-history-count").textContent = `${page.total} 项`;
  const root = document.querySelector("#rejected-history");
  const list = element("div", "rejected-history-list");
  if (!page.items.length) {
    list.append(element("div", "empty", "暂无已驳回 Wiki 修订"));
  } else {
    page.items.forEach((item) => {
      const card = element("article", "rejected-history-card");
      const identity = element("div");
      identity.append(
        element("strong", "", item.page_title),
        element("small", "", `修订 ${item.revision_number} · ${item.classification} · ${item.source_is_current ? "当前来源" : "历史来源"}`),
      );
      const note = item.classification === "restricted"
        ? "复核意见受密级策略保护，请通过 CLI 回源核对。"
        : item.review_note || "未记录复核意见。";
      const metadata = element("div", "rejected-history-meta");
      metadata.append(
        element("span", "", item.rejected_by || "操作者未知"),
        element("time", "", formatTime(item.rejected_at)),
      );
      card.append(identity, element("p", "rejection-note", note), metadata);
      list.append(card);
    });
  }
  const pagination = element("div", "queue-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    reviewState.historyOffset = Math.max(0, page.offset - page.limit);
    await refreshReviewQueues();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    reviewState.historyOffset = page.offset + page.limit;
    await refreshReviewQueues();
  });
  next.disabled = !page.has_next;
  pagination.append(previous, element("span", "", `第 ${pageNumber}/${pageCount} 页`), next);
  root.replaceChildren(list, pagination);
}

async function fetchReviewPage(kind) {
  const parameters = new URLSearchParams({
    kind,
    limit: String(reviewState.limit),
    offset: String(reviewState.offsets[kind]),
  });
  if (reviewState.filters.query) parameters.set("q", reviewState.filters.query);
  if (reviewState.filters.statuses[kind]) parameters.set("status", reviewState.filters.statuses[kind]);
  if (reviewState.filters.classification) parameters.set("classification", reviewState.filters.classification);
  const response = await fetch(`/api/v1/review-queue?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `审核队列读取失败（HTTP ${response.status}）`);
  return page;
}

async function fetchRejectedHistory() {
  const parameters = new URLSearchParams({
    limit: String(reviewState.limit),
    offset: String(reviewState.historyOffset),
  });
  if (reviewState.filters.query) parameters.set("q", reviewState.filters.query);
  if (reviewState.filters.classification) parameters.set("classification", reviewState.filters.classification);
  const response = await fetch(`/api/v1/wiki-revisions/history?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `已驳回修订历史读取失败（HTTP ${response.status}）`);
  return page;
}

async function loadReviewQueues() {
  const [pages, history] = await Promise.all([
    Promise.all(reviewKinds.map(fetchReviewPage)),
    fetchRejectedHistory(),
  ]);
  renderReviews(Object.fromEntries(pages.map((page) => [page.kind, page])));
  renderRejectedHistory(history);
}

async function refreshReviewQueues() {
  try {
    await loadReviewQueues();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "审核队列读取失败");
  }
}

async function acceptEntityCandidate(candidate, entitySelect, noteInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const entityId = entitySelect.value;
  if (!entityId) {
    showBanner("请选择已存在的规范实体");
    entitySelect.focus();
    return;
  }
  if (!window.confirm(`确认把“${candidate.suggested_name}”作为逐字提及关联到所选规范实体？该操作将以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/entity-candidates/${encodeURIComponent(candidate.candidate_id)}/accept`, {
      actor,
      entity_id: entityId,
      note: noteInput.value.trim() || null,
    });
    await loadDashboard();
    showBanner(`实体候选已接受，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "实体候选接受失败");
  }
}

async function rejectEntityCandidate(candidate, noteInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const note = noteInput.value.trim();
  if (!note) {
    showBanner("驳回实体候选必须填写复核意见");
    noteInput.focus();
    return;
  }
  if (!window.confirm(`确认驳回实体候选“${candidate.suggested_name}”？该终态决定将以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/entity-candidates/${encodeURIComponent(candidate.candidate_id)}/reject`, { actor, note });
    await loadDashboard();
    showBanner(`实体候选已驳回，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "实体候选驳回失败");
  }
}

function renderEntityCandidates(page) {
  document.querySelector("#entity-candidate-count").textContent = `${page.total} 项`;
  const list = document.querySelector("#entity-candidate-list");
  list.replaceChildren();
  if (!page.items.length) {
    list.append(element("div", "panel empty", "当前筛选没有非受限实体候选"));
  }
  page.items.forEach((candidate) => {
    const card = element("article", "entity-candidate-card panel");
    const heading = element("header", "entity-candidate-heading");
    const title = element("div");
    title.append(
      element("h3", "", candidate.suggested_name),
      element("small", "", `${shortId(candidate.candidate_id)} · ${candidate.suggested_type}`),
    );
    const badges = element("div", "entity-candidate-badges");
    badges.append(
      element("span", `tag ${candidate.classification}`, candidate.classification),
      element("span", "tag", statusLabels[candidate.status] || candidate.status),
      element("span", `tag ${candidate.verbatim_match ? "internal" : "restricted"}`, candidate.verbatim_match ? "逐字命中" : "非逐字"),
      element("span", `tag ${candidate.source_is_current ? "internal" : "restricted"}`, candidate.source_is_current ? "来源当前" : "来源过期"),
    );
    heading.append(title, badges);

    const body = element("div", "entity-candidate-body");
    const metadata = element("div", "entity-candidate-metadata");
    metadata.append(
      element("span", "", `证据 ID：${candidate.evidence_id}`),
      element("span", "", `模型候选 ID：${candidate.source_candidate_id}`),
      element("span", "", `来源：${candidate.provider}${candidate.model ? ` / ${candidate.model}` : ""}`),
      element("span", "", `提示词版本：${candidate.prompt_version}`),
      element("span", "", `导入时间：${formatTime(candidate.created_at)}`),
    );
    const actions = element("div", "entity-candidate-actions");
    if (candidate.status === "pending") {
      const compatibleEntities = [...page.entity_options].sort((left, right) => {
        const leftMatch = left.entity_type === candidate.suggested_type ? 0 : 1;
        const rightMatch = right.entity_type === candidate.suggested_type ? 0 : 1;
        return leftMatch - rightMatch || left.canonical_name.localeCompare(right.canonical_name, "zh-CN");
      });
      const entitySelect = element("select");
      const placeholder = element("option", "", compatibleEntities.length ? "选择规范实体（同类型优先）" : "暂无规范实体，请先使用 CLI 创建");
      placeholder.value = "";
      entitySelect.append(placeholder);
      compatibleEntities.forEach((entityOption) => {
        const option = element("option", "", `${entityOption.canonical_name} · ${entityOption.entity_type} · ${entityOption.evidence_count} 条证据`);
        option.value = entityOption.entity_id;
        entitySelect.append(option);
      });
      const acceptNote = element("textarea");
      acceptNote.maxLength = 2000;
      acceptNote.rows = 2;
      acceptNote.placeholder = "接受说明（可选，仅审计保存哈希）";
      const acceptRow = element("div", "entity-candidate-action-row");
      const acceptButton = button("接受并关联", "primary", () => acceptEntityCandidate(candidate, entitySelect, acceptNote));
      acceptButton.disabled = !candidate.verbatim_match || !candidate.source_is_current || !compatibleEntities.length;
      acceptRow.append(entitySelect, acceptButton);

      const rejectNote = element("textarea");
      rejectNote.maxLength = 2000;
      rejectNote.rows = 2;
      rejectNote.placeholder = "驳回意见（必填，仅审计保存哈希）";
      const rejectRow = element("div", "entity-candidate-action-row");
      rejectRow.append(rejectNote, button("驳回候选", "danger", () => rejectEntityCandidate(candidate, rejectNote)));
      actions.append(acceptNote, acceptRow, rejectRow);
    } else {
      const result = candidate.status === "accepted"
        ? `已关联：${candidate.canonical_name || candidate.resolved_entity_id}`
        : "已驳回，不可再次流转";
      actions.append(element("div", "entity-candidate-terminal", `${result} · ${candidate.reviewed_by || "未知审核人"} · ${formatTime(candidate.reviewed_at)}`));
    }
    body.append(metadata, actions);
    card.append(heading, body);
    list.append(card);
  });

  const pagination = document.querySelector("#entity-candidate-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    entityCandidateState.offset = Math.max(0, page.offset - page.limit);
    await refreshEntityCandidates();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    entityCandidateState.offset = page.offset + page.limit;
    await refreshEntityCandidates();
  });
  next.disabled = !page.has_next;
  pagination.replaceChildren(previous, element("span", "", `第 ${pageNumber}/${pageCount} 页 · ${page.total} 项`), next);
}

async function loadEntityCandidates() {
  const parameters = new URLSearchParams({
    limit: String(entityCandidateState.limit),
    offset: String(entityCandidateState.offset),
    status: entityCandidateState.status,
  });
  if (entityCandidateState.query) parameters.set("q", entityCandidateState.query);
  const response = await fetch(`/api/v1/entity-candidates?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `实体候选读取失败（HTTP ${response.status}）`);
  renderEntityCandidates(page);
}

async function refreshEntityCandidates() {
  try {
    await loadEntityCandidates();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "实体候选读取失败");
  }
}

function entityMergeOptionLabel(item) {
  return `${item.canonical_name} · ${item.entity_type} · ${item.visibility} · ${item.evidence_count} 条证据`;
}

function populateEntityMergeOptions(items) {
  entityMergeState.entityOptions = items;
  const sourceSelect = document.querySelector("#entity-merge-source");
  const targetSelect = document.querySelector("#entity-merge-target");
  const previousSource = sourceSelect.value;
  const previousTarget = targetSelect.value;
  sourceSelect.replaceChildren(element("option", "", items.length ? "选择将被归档的源实体" : "暂无 Web 可见实体"));
  sourceSelect.firstChild.value = "";
  items.forEach((item) => {
    const option = element("option", "", entityMergeOptionLabel(item));
    option.value = item.entity_id;
    sourceSelect.append(option);
  });
  if (items.some((item) => item.entity_id === previousSource)) sourceSelect.value = previousSource;

  const source = items.find((item) => item.entity_id === sourceSelect.value);
  const targets = source
    ? items.filter((item) => item.entity_type === source.entity_type && item.entity_id !== source.entity_id)
    : [];
  targetSelect.replaceChildren(element("option", "", source ? (targets.length ? "选择保留的目标实体" : "没有同类型目标实体") : "请先选择源实体"));
  targetSelect.firstChild.value = "";
  targets.forEach((item) => {
    const option = element("option", "", entityMergeOptionLabel(item));
    option.value = item.entity_id;
    targetSelect.append(option);
  });
  if (targets.some((item) => item.entity_id === previousTarget)) targetSelect.value = previousTarget;
}

async function reviewEntityMerge(request, decision, noteInput, confirmationInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const note = noteInput.value.trim();
  if (!note) {
    showBanner("实体合并复核意见不能为空");
    noteInput.focus();
    return;
  }
  const approving = decision === "approve";
  if (approving && confirmationInput.value.trim() !== request.approval_confirmation) {
    showBanner(`批准前必须完整输入确认短语：${request.approval_confirmation}`);
    confirmationInput.focus();
    return;
  }
  const action = approving ? "批准并执行合并" : "驳回合并请求";
  if (!window.confirm(`确认${action}？该决定将以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/entity-merges/${encodeURIComponent(request.id)}/review`, {
      actor,
      decision,
      note,
      confirmation: approving ? confirmationInput.value.trim() : "",
    });
    await loadDashboard();
    showBanner(`实体合并请求已${approving ? "批准" : "驳回"}，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "实体合并复核失败");
  }
}

function renderEntityMerges(page) {
  document.querySelector("#entity-merge-count").textContent = `${page.total} 项待复核`;
  populateEntityMergeOptions(page.entity_options);
  const list = document.querySelector("#entity-merge-list");
  list.replaceChildren();
  if (!page.items.length) {
    list.append(element("div", "panel empty", "当前没有符合条件的实体合并请求"));
  }
  page.items.forEach((request) => {
    const card = element("article", "entity-candidate-card panel");
    const heading = element("header", "entity-candidate-heading");
    const title = element("div");
    title.append(
      element("h3", "", "待复核实体合并"),
      element("small", "", request.id),
    );
    const badges = element("div", "entity-candidate-badges");
    badges.append(
      element("span", "tag", request.entity_type),
      element("span", "tag internal", statusLabels[request.status] || request.status),
    );
    heading.append(title, badges);

    const route = element("div", "entity-merge-route");
    const source = element("div", "entity-merge-side");
    source.append(
      element("small", "", "源实体 · 合并后归档"),
      element("strong", "", request.source_name),
      element("small", "", `${request.source_visibility} · ${request.source_evidence_count} 条历史证据 · ${shortId(request.source_entity_id)}`),
    );
    const target = element("div", "entity-merge-side");
    target.append(
      element("small", "", "目标实体 · 合并后保留"),
      element("strong", "", request.target_name),
      element("small", "", `${request.target_visibility} · ${request.target_evidence_count} 条历史证据 · ${shortId(request.target_entity_id)}`),
    );
    route.append(source, element("span", "entity-merge-arrow", "→"), target);

    const metadata = element("div", "entity-merge-review-meta", `提议人：${request.proposed_by} · 提交时间：${formatTime(request.created_at)} · 复核人必须不同于提议人`);
    const review = element("div", "entity-merge-review");
    const note = element("textarea");
    note.maxLength = 2000;
    note.rows = 2;
    note.placeholder = "复核意见（必填，仅保存 SHA-256）";
    const confirmation = element("input");
    confirmation.maxLength = 180;
    confirmation.placeholder = request.approval_confirmation;
    confirmation.setAttribute("aria-label", "批准合并确认短语");
    review.append(
      note,
      confirmation,
      button("批准合并", "primary", () => reviewEntityMerge(request, "approve", note, confirmation)),
      button("驳回请求", "danger", () => reviewEntityMerge(request, "reject", note, confirmation)),
    );
    card.append(heading, route, metadata, review);
    list.append(card);
  });

  const pagination = document.querySelector("#entity-merge-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    entityMergeState.offset = Math.max(0, page.offset - page.limit);
    await refreshEntityMerges();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    entityMergeState.offset = page.offset + page.limit;
    await refreshEntityMerges();
  });
  next.disabled = !page.has_next;
  pagination.replaceChildren(previous, element("span", "", `第 ${pageNumber}/${pageCount} 页 · ${page.total} 项`), next);
}

async function loadEntityMerges() {
  const parameters = new URLSearchParams({
    limit: String(entityMergeState.limit),
    offset: String(entityMergeState.offset),
  });
  if (entityMergeState.query) parameters.set("q", entityMergeState.query);
  const response = await fetch(`/api/v1/entity-merges?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `实体合并请求读取失败（HTTP ${response.status}）`);
  renderEntityMerges(page);
}

async function refreshEntityMerges() {
  try {
    await loadEntityMerges();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "实体合并请求读取失败");
  }
}

function relationshipEntityOptionLabel(item) {
  return `${item.canonical_name} · ${item.entity_type} · ${item.visibility} · ${item.evidence_count} 条证据`;
}

function updateEntityRelationshipConfirmationHint() {
  const relationKey = document.querySelector("#entity-relationship-type").value;
  const sourceId = document.querySelector("#entity-relationship-source").value;
  const targetId = document.querySelector("#entity-relationship-target").value;
  const input = document.querySelector("#entity-relationship-confirmation");
  input.value = "";
  input.placeholder = relationKey && sourceId && targetId
    ? `登记 ${relationKey} ${sourceId} ${targetId}`
    : "选择关系类型和两个实体后生成";
}

function populateEntityRelationshipOptions(page) {
  entityRelationshipState.entityOptions = page.entity_options;
  entityRelationshipState.relationTypes = page.relation_types;
  const typeSelect = document.querySelector("#entity-relationship-type");
  const sourceSelect = document.querySelector("#entity-relationship-source");
  const targetSelect = document.querySelector("#entity-relationship-target");
  const previousType = typeSelect.value;
  const previousSource = sourceSelect.value;
  const previousTarget = targetSelect.value;

  typeSelect.replaceChildren(element("option", "", page.relation_types.length ? "选择关系类型" : "暂无类型，请先使用 CLI 登记"));
  typeSelect.firstChild.value = "";
  page.relation_types.forEach((item) => {
    const direction = item.directed ? "有向" : "无向";
    const option = element("option", "", `${item.label} · ${item.relation_key} · ${direction}`);
    option.value = item.relation_key;
    typeSelect.append(option);
  });
  if (page.relation_types.some((item) => item.relation_key === previousType)) typeSelect.value = previousType;

  sourceSelect.replaceChildren(element("option", "", page.entity_options.length ? "选择源实体" : "暂无 Web 可见实体"));
  sourceSelect.firstChild.value = "";
  page.entity_options.forEach((item) => {
    const option = element("option", "", relationshipEntityOptionLabel(item));
    option.value = item.entity_id;
    sourceSelect.append(option);
  });
  if (page.entity_options.some((item) => item.entity_id === previousSource)) sourceSelect.value = previousSource;

  const targets = page.entity_options.filter((item) => item.entity_id !== sourceSelect.value);
  targetSelect.replaceChildren(element("option", "", sourceSelect.value ? (targets.length ? "选择目标实体" : "没有可用目标实体") : "请先选择源实体"));
  targetSelect.firstChild.value = "";
  targets.forEach((item) => {
    const option = element("option", "", relationshipEntityOptionLabel(item));
    option.value = item.entity_id;
    targetSelect.append(option);
  });
  if (targets.some((item) => item.entity_id === previousTarget)) targetSelect.value = previousTarget;
  updateEntityRelationshipConfirmationHint();
}

function renderEntityRelationshipEvidence(page) {
  entityRelationshipState.evidenceItems = page.items;
  const root = document.querySelector("#entity-relationship-evidence");
  root.replaceChildren();
  document.querySelector("#entity-relationship-evidence-summary").textContent = page.total
    ? `${page.total} 条共同证据；当前显示 ${page.items.length} 条`
    : "没有同时关联两个实体的当前 verified 非受限证据";
  if (!page.items.length) {
    root.append(element("span", "muted", "暂无可选证据"));
    return;
  }
  page.items.forEach((item) => {
    const label = element("label", "relationship-evidence-option");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = item.evidence_id;
    checkbox.name = "relationship-evidence";
    const text = element("span");
    text.append(
      element("strong", "", `${item.document_name} · ${item.classification}`),
      element("small", "", item.evidence_id),
    );
    label.append(checkbox, text);
    root.append(label);
  });
}

async function loadEntityRelationshipEvidence() {
  const sourceId = document.querySelector("#entity-relationship-source").value;
  const targetId = document.querySelector("#entity-relationship-target").value;
  const root = document.querySelector("#entity-relationship-evidence");
  if (!sourceId || !targetId) {
    entityRelationshipState.evidenceItems = [];
    root.replaceChildren();
    document.querySelector("#entity-relationship-evidence-summary").textContent = "选择两个实体后读取候选";
    updateEntityRelationshipConfirmationHint();
    return;
  }
  const parameters = new URLSearchParams({
    source_entity_id: sourceId,
    target_entity_id: targetId,
    limit: "50",
  });
  const response = await fetch(`/api/v1/entity-relationships/evidence?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `关系证据读取失败（HTTP ${response.status}）`);
  renderEntityRelationshipEvidence(page);
  updateEntityRelationshipConfirmationHint();
}

async function retractEntityRelationship(relationship, noteInput, confirmationInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const note = noteInput.value.trim();
  if (!note) {
    showBanner("撤销业务关系必须填写说明");
    noteInput.focus();
    return;
  }
  if (confirmationInput.value.trim() !== relationship.retraction_confirmation) {
    showBanner(`撤销前必须完整输入确认短语：${relationship.retraction_confirmation}`);
    confirmationInput.focus();
    return;
  }
  if (!window.confirm(`确认撤销“${relationship.source_name} ${relationship.label} ${relationship.target_name}”？历史证据不会删除。`)) return;
  try {
    await postTransition(`/api/v1/entity-relationships/${encodeURIComponent(relationship.relationship_id)}/retract`, {
      actor,
      note,
      confirmation: confirmationInput.value.trim(),
    });
    await loadDashboard();
    showBanner(`业务关系已撤销，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "业务关系撤销失败");
  }
}

function renderEntityRelationships(page) {
  document.querySelector("#entity-relationship-count").textContent = `${page.total} 项`;
  populateEntityRelationshipOptions(page);
  const list = document.querySelector("#entity-relationship-list");
  list.replaceChildren();
  if (!page.items.length) {
    list.append(element("div", "panel empty", "当前筛选没有可见业务关系"));
  }
  page.items.forEach((relationship) => {
    const card = element("article", "entity-candidate-card panel");
    const heading = element("header", "entity-candidate-heading");
    const title = element("div");
    title.append(
      element("h3", "", relationship.label),
      element("small", "", `${relationship.relationship_id} · ${relationship.relation_key}`),
    );
    const badges = element("div", "entity-candidate-badges");
    badges.append(
      element("span", "tag", relationship.directed ? "有向" : "无向"),
      element("span", `tag ${relationship.status === "active" ? "internal" : ""}`, relationship.status === "active" ? "生效中" : "已撤销"),
    );
    relationship.classifications.forEach((classification) => badges.append(element("span", `tag ${classification}`, classification)));
    heading.append(title, badges);

    const route = element("div", "relationship-route");
    const source = element("div", "entity-merge-side");
    source.append(
      element("small", "", "源实体"),
      element("strong", "", relationship.source_name),
      element("small", "", `${relationship.source_entity_type} · ${shortId(relationship.source_entity_id)}`),
    );
    const target = element("div", "entity-merge-side");
    target.append(
      element("small", "", "目标实体"),
      element("strong", "", relationship.target_name),
      element("small", "", `${relationship.target_entity_type} · ${shortId(relationship.target_entity_id)}`),
    );
    route.append(source, element("span", "entity-merge-arrow", relationship.directed ? "→" : "↔"), target);
    const support = element(
      "div",
      "relationship-support",
      `历史证据 ${relationship.supporting_evidence_count} 条 · 当前 verified ${relationship.current_verified_supporting_evidence_count} 条 · 登记人 ${relationship.created_by} · ${formatTime(relationship.created_at)}`,
    );
    card.append(heading, route, support);
    if (relationship.status === "active") {
      const retract = element("div", "relationship-retract");
      const note = element("textarea");
      note.maxLength = 2000;
      note.rows = 2;
      note.placeholder = "撤销说明（必填，仅保存 SHA-256）";
      const confirmation = element("input");
      confirmation.maxLength = 220;
      confirmation.placeholder = relationship.retraction_confirmation;
      retract.append(
        note,
        confirmation,
        button("撤销关系", "danger", () => retractEntityRelationship(relationship, note, confirmation)),
      );
      card.append(retract);
    } else {
      card.append(element("div", "relationship-support", `撤销人 ${relationship.retracted_by || "未知"} · ${formatTime(relationship.retracted_at)}`));
    }
    list.append(card);
  });

  const pagination = document.querySelector("#entity-relationship-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    entityRelationshipState.offset = Math.max(0, page.offset - page.limit);
    await refreshEntityRelationships();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    entityRelationshipState.offset = page.offset + page.limit;
    await refreshEntityRelationships();
  });
  next.disabled = !page.has_next;
  pagination.replaceChildren(previous, element("span", "", `第 ${pageNumber}/${pageCount} 页 · ${page.total} 项`), next);
}

async function loadEntityRelationships() {
  const parameters = new URLSearchParams({
    limit: String(entityRelationshipState.limit),
    offset: String(entityRelationshipState.offset),
    status: entityRelationshipState.status,
  });
  if (entityRelationshipState.query) parameters.set("q", entityRelationshipState.query);
  const response = await fetch(`/api/v1/entity-relationships?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `业务关系读取失败（HTTP ${response.status}）`);
  renderEntityRelationships(page);
}

async function refreshEntityRelationships() {
  try {
    await loadEntityRelationships();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "业务关系读取失败");
  }
}

function candidateSide(side, label) {
  const root = element("section", "candidate-side");
  const heading = element("div", "candidate-side-heading");
  heading.append(
    element("strong", "", `${label} · ${side.document_name}`),
    element("code", "", shortId(side.evidence_id)),
  );
  const excerpt = element("pre", "candidate-excerpt", side.excerpt);
  const locator = element("small", "candidate-locator", JSON.stringify(side.locators));
  root.append(heading, excerpt, locator);
  return root;
}

async function saveCandidateLabel(candidate, conflictSelect, typeSelect, noteInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  if (!conflictSelect.value) {
    showBanner("请选择冲突或非冲突");
    conflictSelect.focus();
    return;
  }
  const expectedConflict = conflictSelect.value === "true";
  const expectedType = expectedConflict ? typeSelect.value || null : null;
  if (expectedConflict && !expectedType) {
    showBanner("标记为冲突时必须选择冲突类型");
    typeSelect.focus();
    return;
  }
  try {
    await postTransition(`/api/v1/conflict-candidate-packs/${encodeURIComponent(candidateState.packId)}/label`, {
      actor,
      candidate_id: candidate.candidate_id,
      expected_content_sha256: candidateState.contentSha256,
      expected_conflict: expectedConflict,
      expected_type: expectedType,
      note: noteInput.value.trim() || null,
    });
    await loadCandidatePlans();
    await loadCandidatePage();
    showBanner(`候选标签已保存，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "候选标签保存失败");
  }
}

async function saveCandidateReview(candidate, decisionSelect, noteInput) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  const decision = decisionSelect.value;
  if (!decision) {
    showBanner("请选择复核通过或驳回");
    decisionSelect.focus();
    return;
  }
  const note = noteInput.value.trim();
  if (decision === "rejected" && !note) {
    showBanner("驳回复核必须填写意见");
    noteInput.focus();
    return;
  }
  try {
    await postTransition(`/api/v1/conflict-candidate-packs/${encodeURIComponent(candidateState.packId)}/review`, {
      actor,
      candidate_id: candidate.candidate_id,
      expected_content_sha256: candidateState.contentSha256,
      decision,
      note: note || null,
    });
    await loadCandidatePlans();
    await loadCandidatePage();
    showBanner(`候选复核已保存，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "候选复核保存失败");
  }
}

async function submitCandidatePack(page) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  if (!window.confirm(`确认提交全部 ${page.counts.total} 条标签？提交后标签将锁定，并以 ${actor} 写入审计日志。`)) return;
  try {
    await postTransition(`/api/v1/conflict-candidate-packs/${encodeURIComponent(page.pack_id)}/submit`, {
      actor,
      expected_content_sha256: page.content_sha256,
    });
    await loadCandidatePacks();
    showBanner(`候选标签已提交复核，标注人：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "候选标签提交失败");
  }
}

function renderCandidatePage(page) {
  candidateState.contentSha256 = page.content_sha256;
  renderCandidatePlanBoundary();
  const summary = document.querySelector("#candidate-pack-summary");
  const metadata = element("div", "candidate-summary-metadata");
  metadata.append(
    element("strong", "", candidatePhaseLabels[page.phase] || page.phase),
    element("span", "", `${page.counts.labeled}/${page.counts.total} 已标注 · ${page.counts.approved} 通过 · ${page.counts.rejected} 驳回`),
    element("small", "", `生成者 ${page.generated_by} · 相似度阈值 ${page.minimum_similarity}${page.annotator ? ` · 标注人 ${page.annotator}` : ""}`),
  );
  if (page.labeling_batch) {
    const batch = page.labeling_batch.status;
    metadata.append(
      element("span", "candidate-batch-progress", `${page.labeling_batch.batch_id}：${batch.labeled_count}/${batch.candidate_count} 已标注 · ${batch.approved_count} 通过 · ${batch.rejected_count} 驳回`),
    );
  }
  const controls = element("div", "candidate-summary-actions");
  if (page.phase === "labeling") {
    const submit = button("提交整包复核", "primary", () => submitCandidatePack(page));
    submit.disabled = page.counts.labeled !== page.counts.total || page.statistics.truncated;
    controls.append(submit);
    if (page.statistics.truncated) controls.append(element("small", "", "截断候选包不能作为完整基线提交"));
  } else if (page.phase === "reviewed") {
    controls.append(element("small", "", "全部复核通过，可使用 CLI 固化评测数据集"));
  } else {
    controls.append(element("small", "", "标签已锁定，复核人必须与标注人不同"));
  }
  summary.replaceChildren(metadata, controls);

  const list = document.querySelector("#candidate-list");
  list.replaceChildren();
  if (!page.items.length) {
    list.append(element("div", "panel empty", "当前筛选没有候选项"));
  }
  page.items.forEach((candidate) => {
    const card = element("article", "candidate-card panel");
    const header = element("header", "candidate-card-heading");
    const prediction = candidate.predicted_conflict
      ? `规则预测：${candidate.predicted_type}`
      : "规则预测：非冲突";
    header.append(
      element("div", "", candidate.candidate_id),
      element("span", "tag", `${prediction} · ${Math.round(candidate.similarity * 100)}%`),
    );
    const comparison = element("div", "candidate-comparison");
    comparison.append(candidateSide(candidate.left, "左侧"), candidateSide(candidate.right, "右侧"));
    const form = element("div", "candidate-decision");
    if (page.phase === "labeling") {
      const conflict = element("select");
      [["", "请选择人工标签"], ["true", "冲突"], ["false", "非冲突"]].forEach(([value, label]) => {
        const option = element("option", "", label);
        option.value = value;
        conflict.append(option);
      });
      conflict.value = candidate.label.expected_conflict === null ? "" : String(candidate.label.expected_conflict);
      const type = element("select");
      [["", "选择冲突类型"], ["polarity_change", "polarity_change"], ["value_change", "value_change"]].forEach(([value, label]) => {
        const option = element("option", "", label);
        option.value = value;
        type.append(option);
      });
      type.value = candidate.label.expected_type || "";
      type.disabled = conflict.value !== "true";
      conflict.addEventListener("change", () => {
        type.disabled = conflict.value !== "true";
        if (type.disabled) type.value = "";
      });
      const note = element("textarea");
      note.maxLength = 2000;
      note.rows = 2;
      note.placeholder = "标注依据（可选）";
      note.value = candidate.label.note || "";
      form.append(conflict, type, note, button("保存标签", "primary", () => saveCandidateLabel(candidate, conflict, type, note)));
    } else {
      const labelText = candidate.label.expected_conflict
        ? `人工标签：${candidate.label.expected_type}`
        : "人工标签：非冲突";
      form.append(element("strong", "candidate-human-label", labelText));
      const decision = element("select");
      [["", "请选择复核决定"], ["approved", "复核通过"], ["rejected", "复核驳回"]].forEach(([value, label]) => {
        const option = element("option", "", label);
        option.value = value;
        decision.append(option);
      });
      decision.value = candidate.review.decision || "";
      const note = element("textarea");
      note.maxLength = 2000;
      note.rows = 2;
      note.placeholder = "复核意见（驳回时必填）";
      note.value = candidate.review.note || "";
      form.append(decision, note, button("保存复核", "primary", () => saveCandidateReview(candidate, decision, note)));
    }
    card.append(header, comparison, form);
    list.append(card);
  });

  const pagination = document.querySelector("#candidate-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    candidateState.offset = Math.max(0, page.offset - page.limit);
    await loadCandidatePage();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    candidateState.offset = page.offset + page.limit;
    await loadCandidatePage();
  });
  next.disabled = !page.has_next;
  pagination.replaceChildren(previous, element("span", "", `第 ${pageNumber}/${pageCount} 页 · ${page.total} 项`), next);
}

async function loadCandidatePage() {
  if (!candidateState.packId) return;
  const requestSerial = ++candidateState.pageRequestSerial;
  const summaryRoot = document.querySelector("#candidate-pack-summary");
  summaryRoot.setAttribute("aria-busy", "true");
  const parameters = new URLSearchParams({
    limit: String(candidateState.limit),
    offset: String(candidateState.offset),
    state: candidateState.state,
  });
  if (candidateState.query) parameters.set("q", candidateState.query);
  if (candidateState.planId && candidateState.batchId) {
    parameters.set("plan_id", candidateState.planId);
    parameters.set("batch_id", candidateState.batchId);
  }
  try {
    const response = await fetch(`/api/v1/conflict-candidate-packs/${encodeURIComponent(candidateState.packId)}?${parameters}`, { headers: { Accept: "application/json" } });
    const page = await response.json();
    if (requestSerial !== candidateState.pageRequestSerial) return;
    if (!response.ok) throw new Error(page.error || `候选包读取失败（HTTP ${response.status}）`);
    renderCandidatePage(page);
  } finally {
    if (requestSerial === candidateState.pageRequestSerial) {
      summaryRoot.removeAttribute("aria-busy");
    }
  }
}

function renderCandidatePlanBoundary() {
  const boundary = document.querySelector("#candidate-plan-boundary");
  const plan = candidateState.plans.find(
    (item) => item.plan_id === candidateState.planId
  );
  if (!plan || !candidateState.batchId) {
    boundary.textContent = "未启用批次范围；当前显示候选包全部候选。";
  } else {
    boundary.textContent = `当前只显示 ${candidateState.batchId}；计划覆盖整包 ${plan.summary.candidate_count} 对候选且不保存第二套标签。`;
  }
  if (candidateState.invalidPlanCount) {
    boundary.textContent += `；另有 ${candidateState.invalidPlanCount} 个计划校验失败，未开放。`;
  }
}

function renderCandidateBatchOptions() {
  const planSelector = document.querySelector("#candidate-plan");
  const batchSelector = document.querySelector("#candidate-batch");
  const plan = candidateState.plans.find((item) => item.plan_id === candidateState.planId);
  batchSelector.replaceChildren();
  if (!plan) {
    candidateState.planId = "";
    candidateState.batchId = "";
    planSelector.value = "";
    const option = element("option", "", "未选择批次");
    option.value = "";
    batchSelector.append(option);
    batchSelector.disabled = true;
    renderCandidatePlanBoundary();
    return;
  }
  const previousBatchId = candidateState.batchId;
  const nextBatch = plan.batches.find((item) => !item.annotation_complete)
    || plan.batches.find((item) => !item.review_complete)
    || plan.batches[0];
  candidateState.batchId = plan.batches.some((item) => item.batch_id === previousBatchId)
    ? previousBatchId
    : nextBatch?.batch_id || "";
  plan.batches.forEach((batch) => {
    const option = element(
      "option",
      "",
      `${batch.batch_id} · 标注 ${batch.labeled_count}/${batch.candidate_count} · 通过 ${batch.approved_count}`,
    );
    option.value = batch.batch_id;
    batchSelector.append(option);
  });
  batchSelector.value = candidateState.batchId;
  batchSelector.disabled = !candidateState.batchId;
  renderCandidatePlanBoundary();
}

async function loadCandidatePlans() {
  const requestSerial = ++candidateState.planRequestSerial;
  const planSelector = document.querySelector("#candidate-plan");
  const previousPlanId = candidateState.planId;
  planSelector.replaceChildren();
  const allOption = element("option", "", "全部候选（不按批次）");
  allOption.value = "";
  planSelector.append(allOption);
  if (!candidateState.packId) {
    candidateState.plans = [];
    candidateState.invalidPlanCount = 0;
    candidateState.planId = "";
    candidateState.batchId = "";
    renderCandidateBatchOptions();
    return;
  }
  const parameters = new URLSearchParams({
    source_pack_id: candidateState.packId,
  });
  const response = await fetch(`/api/v1/conflict-labeling-plans?${parameters}`, { headers: { Accept: "application/json" } });
  const listing = await response.json();
  if (requestSerial !== candidateState.planRequestSerial) return;
  if (!response.ok) throw new Error(listing.error || `标注计划读取失败（HTTP ${response.status}）`);
  candidateState.plans = listing.items;
  candidateState.invalidPlanCount = listing.invalid_plan_count;
  listing.items.forEach((item) => {
    const option = element(
      "option",
      "",
      `${shortId(item.plan_id)} · ${item.summary.labeled_count}/${item.summary.candidate_count} 已标注 · ${item.summary.batch_count} 批`,
    );
    option.value = item.plan_id;
    planSelector.append(option);
  });
  candidateState.planId = listing.items.some((item) => item.plan_id === previousPlanId)
    ? previousPlanId
    : listing.items[0]?.plan_id || "";
  planSelector.value = candidateState.planId;
  renderCandidateBatchOptions();
}

async function loadCandidatePacks() {
  const response = await fetch("/api/v1/conflict-candidate-packs", { headers: { Accept: "application/json" } });
  const listing = await response.json();
  if (!response.ok) throw new Error(listing.error || `候选包列表读取失败（HTTP ${response.status}）`);
  document.querySelector("#candidate-pack-count").textContent = listing.invalid_count
    ? `${listing.total} 个候选包 · ${listing.invalid_count} 个校验失败`
    : `${listing.total} 个候选包`;
  const selector = document.querySelector("#candidate-pack");
  const previous = candidateState.packId;
  selector.replaceChildren();
  listing.items.forEach((item) => {
    const option = element("option", "", `${shortId(item.pack_id)} · ${candidatePhaseLabels[item.phase] || item.phase} · ${item.counts.labeled}/${item.counts.total}`);
    option.value = item.pack_id;
    selector.append(option);
  });
  candidateState.packId = listing.items.some((item) => item.pack_id === previous)
    ? previous
    : listing.items[0]?.pack_id || "";
  selector.value = candidateState.packId;
  if (!candidateState.packId) {
    document.querySelector("#candidate-pack-summary").replaceChildren(element("div", "empty", "尚无有效跨文档冲突候选包"));
    document.querySelector("#candidate-list").replaceChildren();
    document.querySelector("#candidate-pagination").replaceChildren();
    return;
  }
  await loadCandidatePlans();
  await loadCandidatePage();
}

function graphPilotProgress(label, value, detail, tone = "") {
  const card = element("article", `graph-pilot-progress ${tone}`.trim());
  card.append(
    element("span", "", label),
    element("strong", "", value),
    element("small", "", detail),
  );
  return card;
}

function renderGraphPilotPage(page) {
  const summary = page.summary;
  document.querySelector("#graph-pilot-count").textContent = `${page.total} / ${summary.candidate_count} 条`;
  const summaryRoot = document.querySelector("#graph-pilot-summary");
  summaryRoot.replaceChildren(
    graphPilotProgress(
      "来源快照",
      `${summary.snapshot_valid_count}/${summary.candidate_count}`,
      summary.source_snapshot_passed ? "正文、定位、版本与运行一致" : `${summary.snapshot_invalid_count} 条失效`,
      summary.source_snapshot_passed ? "ok" : "danger",
    ),
    graphPilotProgress(
      "证据审核",
      `${Math.round(summary.verified_evidence_coverage * 100)}%`,
      `${summary.verified_evidence_count} 条 verified · ${summary.status_counts.draft || 0} 条 draft`,
      summary.evidence_review_complete ? "ok" : "warn",
    ),
    graphPilotProgress(
      "实体绑定",
      `${summary.verified_with_two_entities_count}`,
      `${summary.verified_with_entity_count} 条有实体 · ${summary.relationship_ready_evidence_count} 条关系就绪`,
    ),
    graphPilotProgress(
      "关系与黄金集",
      summary.graph_gold_prerequisites_met ? "可开始" : "未就绪",
      `${summary.verified_with_active_relationship_count} 条已有 active 关系`,
      summary.graph_gold_prerequisites_met ? "ok" : "warn",
    ),
  );

  const list = document.querySelector("#graph-pilot-list");
  list.replaceChildren();
  if (!page.items.length) {
    list.append(element("div", "panel empty", "当前筛选下没有试点证据"));
  } else {
    page.items.forEach((item) => {
      const card = element("article", "panel graph-pilot-card");
      const heading = element("div", "graph-pilot-card-heading");
      const identity = element("div");
      identity.append(
        element("h3", "", item.document_name),
        element("small", "", `${shortId(item.evidence_id)} · #${item.ordinal} · ${item.case_ids.join("、")}`),
      );
      heading.append(
        identity,
        element("span", `tag ${item.classification}`, item.classification),
        element("span", `state ${item.status === "verified" ? "ok" : "warn"}`, statusLabels[item.status] || item.status),
      );
      const metadata = element("div", "graph-pilot-card-meta");
      metadata.append(
        element("span", "", `${item.location_count} 个定位`),
        element("span", "", `${item.active_entity_count} 个 active 实体`),
        element("span", "", `${item.active_relationship_count} 条 active 关系`),
        element("span", item.ready_for_relationship_registration ? "state ok" : "state", item.ready_for_relationship_registration ? "可登记关系" : "尚未满足关系登记条件"),
      );
      const actions = element("div", "queue-actions");
      actions.append(button("查看并审核", "primary", () => openEvidence(item.evidence_id)));
      card.append(heading, metadata, actions);
      list.append(card);
    });
  }

  const pagination = document.querySelector("#graph-pilot-pagination");
  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const previous = button("上一页", "", async () => {
    graphPilotState.offset = Math.max(0, page.offset - page.limit);
    await loadGraphPilotPage();
  });
  previous.disabled = !page.has_previous;
  const next = button("下一页", "", async () => {
    graphPilotState.offset = page.offset + page.limit;
    await loadGraphPilotPage();
  });
  next.disabled = !page.has_next;
  pagination.replaceChildren(
    previous,
    element("span", "", `第 ${pageNumber}/${pageCount} 页 · ${page.total} 项`),
    next,
  );
}

async function loadGraphPilotPage() {
  if (!graphPilotState.packId) return;
  const parameters = new URLSearchParams({
    limit: String(graphPilotState.limit),
    offset: String(graphPilotState.offset),
    status: graphPilotState.status,
  });
  if (graphPilotState.query) parameters.set("q", graphPilotState.query);
  const response = await fetch(`/api/v1/graph-pilot-packs/${encodeURIComponent(graphPilotState.packId)}?${parameters}`, { headers: { Accept: "application/json" } });
  const page = await response.json();
  if (!response.ok) throw new Error(page.error || `图谱试点包读取失败（HTTP ${response.status}）`);
  renderGraphPilotPage(page);
}

async function loadGraphPilotPacks() {
  const response = await fetch("/api/v1/graph-pilot-packs", { headers: { Accept: "application/json" } });
  const listing = await response.json();
  if (!response.ok) throw new Error(listing.error || `图谱试点包列表读取失败（HTTP ${response.status}）`);
  const selector = document.querySelector("#graph-pilot-pack");
  const previous = graphPilotState.packId;
  selector.replaceChildren();
  listing.items.forEach((item) => {
    const summary = item.summary;
    const option = element("option", "", `${item.source_labeling_session_name} · verified ${summary.verified_evidence_count}/${summary.candidate_count}`);
    option.value = item.pack_id;
    selector.append(option);
  });
  graphPilotState.packId = listing.items.some((item) => item.pack_id === previous)
    ? previous
    : listing.items[0]?.pack_id || "";
  selector.value = graphPilotState.packId;
  if (!graphPilotState.packId) {
    const message = listing.invalid_pack_count
      ? `没有有效试点包；${listing.invalid_pack_count} 个文件校验失败`
      : "尚无有效图谱试点证据包";
    document.querySelector("#graph-pilot-count").textContent = "0 个";
    document.querySelector("#graph-pilot-summary").replaceChildren(element("div", "empty", message));
    document.querySelector("#graph-pilot-list").replaceChildren();
    document.querySelector("#graph-pilot-pagination").replaceChildren();
    return;
  }
  await loadGraphPilotPage();
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
    csrfToken = data.web.csrf_token;
    renderSummary(data.summary);
    renderDocuments(data.documents);
    const candidateLoad = loadCandidatePacks().catch((cause) => {
      const message = cause instanceof Error ? cause.message : "候选包读取失败";
      document.querySelector("#candidate-pack-summary").replaceChildren(
        element("div", "empty", message),
      );
      document.querySelector("#candidate-list").replaceChildren();
      document.querySelector("#candidate-pagination").replaceChildren();
      showBanner(message);
    });
    const graphPilotLoad = loadGraphPilotPacks().catch((cause) => {
      const message = cause instanceof Error ? cause.message : "图谱试点包读取失败";
      document.querySelector("#graph-pilot-summary").replaceChildren(element("div", "empty", message));
      document.querySelector("#graph-pilot-list").replaceChildren();
      document.querySelector("#graph-pilot-pagination").replaceChildren();
      showBanner(message);
    });
    const entityCandidateLoad = loadEntityCandidates().catch((cause) => {
      const message = cause instanceof Error ? cause.message : "实体候选读取失败";
      document.querySelector("#entity-candidate-list").replaceChildren(element("div", "panel empty", message));
      document.querySelector("#entity-candidate-pagination").replaceChildren();
      showBanner(message);
    });
    const entityMergeLoad = loadEntityMerges().catch((cause) => {
      const message = cause instanceof Error ? cause.message : "实体合并请求读取失败";
      document.querySelector("#entity-merge-list").replaceChildren(element("div", "panel empty", message));
      document.querySelector("#entity-merge-pagination").replaceChildren();
      showBanner(message);
    });
    const entityRelationshipLoad = loadEntityRelationships().catch((cause) => {
      const message = cause instanceof Error ? cause.message : "业务关系读取失败";
      document.querySelector("#entity-relationship-list").replaceChildren(element("div", "panel empty", message));
      document.querySelector("#entity-relationship-pagination").replaceChildren();
      showBanner(message);
    });
    await Promise.all([loadReviewQueues(), graphPilotLoad, candidateLoad, entityCandidateLoad, entityMergeLoad, entityRelationshipLoad]);
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
document.querySelector("#review-filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  reviewState.filters.query = document.querySelector("#review-query").value.trim();
  reviewState.filters.classification = document.querySelector("#review-classification").value;
  reviewState.filters.statuses = {
    evidence: document.querySelector("#evidence-review-status").value,
    conflicts: document.querySelector("#conflict-review-status").value,
    wiki_revisions: document.querySelector("#wiki-review-status").value,
  };
  reviewKinds.forEach((kind) => { reviewState.offsets[kind] = 0; });
  reviewState.historyOffset = 0;
  await refreshReviewQueues();
});
document.querySelector("#clear-review-filters").addEventListener("click", async () => {
  document.querySelector("#review-filters").reset();
  reviewState.filters = {
    query: "",
    classification: "",
    statuses: { evidence: "", conflicts: "", wiki_revisions: "" },
  };
  reviewKinds.forEach((kind) => { reviewState.offsets[kind] = 0; });
  reviewState.historyOffset = 0;
  await refreshReviewQueues();
});
document.querySelector("#graph-pilot-filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  graphPilotState.packId = document.querySelector("#graph-pilot-pack").value;
  graphPilotState.status = document.querySelector("#graph-pilot-status").value;
  graphPilotState.query = document.querySelector("#graph-pilot-query").value.trim();
  graphPilotState.offset = 0;
  try { await loadGraphPilotPage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "图谱试点包读取失败");
  }
});
document.querySelector("#graph-pilot-pack").addEventListener("change", async (event) => {
  graphPilotState.packId = event.target.value;
  graphPilotState.offset = 0;
  try { await loadGraphPilotPage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "图谱试点包读取失败");
  }
});
document.querySelector("#clear-graph-pilot-filters").addEventListener("click", async () => {
  document.querySelector("#graph-pilot-status").value = "all";
  document.querySelector("#graph-pilot-query").value = "";
  graphPilotState.status = "all";
  graphPilotState.query = "";
  graphPilotState.offset = 0;
  try { await loadGraphPilotPage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "图谱试点包读取失败");
  }
});
document.querySelector("#entity-candidate-filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  entityCandidateState.status = document.querySelector("#entity-candidate-status").value;
  entityCandidateState.query = document.querySelector("#entity-candidate-query").value.trim();
  entityCandidateState.offset = 0;
  await refreshEntityCandidates();
});
document.querySelector("#clear-entity-candidate-filters").addEventListener("click", async () => {
  document.querySelector("#entity-candidate-status").value = "pending";
  document.querySelector("#entity-candidate-query").value = "";
  entityCandidateState.status = "pending";
  entityCandidateState.query = "";
  entityCandidateState.offset = 0;
  await refreshEntityCandidates();
});
document.querySelector("#entity-merge-source").addEventListener("change", () => {
  populateEntityMergeOptions(entityMergeState.entityOptions);
});
document.querySelector("#entity-merge-proposal").addEventListener("submit", async (event) => {
  event.preventDefault();
  let actor;
  try { actor = actorValue(); } catch { return; }
  const sourceEntityId = document.querySelector("#entity-merge-source").value;
  const targetEntityId = document.querySelector("#entity-merge-target").value;
  const note = document.querySelector("#entity-merge-proposal-note").value.trim();
  if (!sourceEntityId || !targetEntityId) {
    showBanner("请选择源实体和同类型目标实体");
    return;
  }
  if (!note) {
    showBanner("实体合并提议说明不能为空");
    document.querySelector("#entity-merge-proposal-note").focus();
    return;
  }
  const source = entityMergeState.entityOptions.find((item) => item.entity_id === sourceEntityId);
  const target = entityMergeState.entityOptions.find((item) => item.entity_id === targetEntityId);
  if (!window.confirm(`确认提交合并方向“${source?.canonical_name || sourceEntityId} → ${target?.canonical_name || targetEntityId}”供异人复核？`)) return;
  try {
    await postTransition("/api/v1/entity-merges/propose", {
      actor,
      source_entity_id: sourceEntityId,
      target_entity_id: targetEntityId,
      note,
    });
    document.querySelector("#entity-merge-proposal-note").value = "";
    entityMergeState.offset = 0;
    await loadDashboard();
    showBanner(`实体合并提议已提交，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "实体合并提议失败");
  }
});
document.querySelector("#entity-merge-filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  entityMergeState.query = document.querySelector("#entity-merge-query").value.trim();
  entityMergeState.offset = 0;
  await refreshEntityMerges();
});
document.querySelector("#clear-entity-merge-filter").addEventListener("click", async () => {
  document.querySelector("#entity-merge-query").value = "";
  entityMergeState.query = "";
  entityMergeState.offset = 0;
  await refreshEntityMerges();
});
document.querySelector("#entity-relationship-type").addEventListener("change", updateEntityRelationshipConfirmationHint);
document.querySelector("#entity-relationship-source").addEventListener("change", async () => {
  populateEntityRelationshipOptions({
    entity_options: entityRelationshipState.entityOptions,
    relation_types: entityRelationshipState.relationTypes,
  });
  try { await loadEntityRelationshipEvidence(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "关系证据读取失败");
  }
});
document.querySelector("#entity-relationship-target").addEventListener("change", async () => {
  try { await loadEntityRelationshipEvidence(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "关系证据读取失败");
  }
});
document.querySelector("#entity-relationship-create").addEventListener("submit", async (event) => {
  event.preventDefault();
  let actor;
  try { actor = actorValue(); } catch { return; }
  const relationKey = document.querySelector("#entity-relationship-type").value;
  const sourceEntityId = document.querySelector("#entity-relationship-source").value;
  const targetEntityId = document.querySelector("#entity-relationship-target").value;
  const note = document.querySelector("#entity-relationship-note").value.trim();
  const confirmationInput = document.querySelector("#entity-relationship-confirmation");
  const evidenceIds = [...document.querySelectorAll('input[name="relationship-evidence"]:checked')].map((item) => item.value);
  if (!relationKey || !sourceEntityId || !targetEntityId) {
    showBanner("请选择关系类型、源实体和目标实体");
    return;
  }
  if (!evidenceIds.length) {
    showBanner("请至少选择一条共同 verified 证据");
    return;
  }
  if (!note) {
    showBanner("业务关系登记说明不能为空");
    document.querySelector("#entity-relationship-note").focus();
    return;
  }
  const expected = `登记 ${relationKey} ${sourceEntityId} ${targetEntityId}`;
  if (confirmationInput.value.trim() !== expected) {
    showBanner(`登记前必须完整输入确认短语：${expected}`);
    confirmationInput.focus();
    return;
  }
  if (!window.confirm(`确认登记该业务关系并引用 ${evidenceIds.length} 条已验证证据？`)) return;
  try {
    await postTransition("/api/v1/entity-relationships/create", {
      actor,
      relation_key: relationKey,
      source_entity_id: sourceEntityId,
      target_entity_id: targetEntityId,
      evidence_ids: evidenceIds,
      note,
      confirmation: confirmationInput.value.trim(),
    });
    document.querySelector("#entity-relationship-note").value = "";
    confirmationInput.value = "";
    entityRelationshipState.status = "active";
    entityRelationshipState.offset = 0;
    await loadDashboard();
    showBanner(`业务关系已登记，审计操作者：${actor}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "业务关系登记失败");
  }
});
document.querySelector("#entity-relationship-filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  entityRelationshipState.status = document.querySelector("#entity-relationship-status").value;
  entityRelationshipState.query = document.querySelector("#entity-relationship-query").value.trim();
  entityRelationshipState.offset = 0;
  await refreshEntityRelationships();
});
document.querySelector("#clear-entity-relationship-filter").addEventListener("click", async () => {
  document.querySelector("#entity-relationship-status").value = "active";
  document.querySelector("#entity-relationship-query").value = "";
  entityRelationshipState.status = "active";
  entityRelationshipState.query = "";
  entityRelationshipState.offset = 0;
  await refreshEntityRelationships();
});
document.querySelector("#candidate-filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  candidateState.packId = document.querySelector("#candidate-pack").value;
  candidateState.planId = document.querySelector("#candidate-plan").value;
  candidateState.batchId = document.querySelector("#candidate-batch").value;
  candidateState.state = document.querySelector("#candidate-state").value;
  candidateState.query = document.querySelector("#candidate-query").value.trim();
  candidateState.offset = 0;
  try { await loadCandidatePage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "候选包读取失败");
  }
});
document.querySelector("#candidate-pack").addEventListener("change", async (event) => {
  candidateState.packId = event.target.value;
  candidateState.planId = "";
  candidateState.batchId = "";
  candidateState.offset = 0;
  try {
    await loadCandidatePlans();
    await loadCandidatePage();
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "候选包读取失败");
  }
});
document.querySelector("#candidate-plan").addEventListener("change", async (event) => {
  candidateState.planId = event.target.value;
  candidateState.batchId = "";
  candidateState.offset = 0;
  renderCandidateBatchOptions();
  try { await loadCandidatePage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "标注批次读取失败");
  }
});
document.querySelector("#candidate-batch").addEventListener("change", async (event) => {
  candidateState.batchId = event.target.value;
  candidateState.offset = 0;
  renderCandidatePlanBoundary();
  try { await loadCandidatePage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "标注批次读取失败");
  }
});
document.querySelector("#clear-candidate-filters").addEventListener("click", async () => {
  document.querySelector("#candidate-state").value = "all";
  document.querySelector("#candidate-query").value = "";
  candidateState.state = "all";
  candidateState.query = "";
  candidateState.offset = 0;
  try { await loadCandidatePage(); } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "候选包读取失败");
  }
});
document.querySelector("#close-evidence-dialog").addEventListener("click", () => document.querySelector("#evidence-dialog").close());
document.querySelector("#close-revision-dialog").addEventListener("click", () => document.querySelector("#revision-dialog").close());
document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((entry) => entry.classList.remove("active"));
    item.classList.add("active");
  });
});
loadDashboard();
