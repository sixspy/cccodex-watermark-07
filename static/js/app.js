(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let formMarker = "旁路快照";
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  // 详情页状态：{ kind: "report"|"batch", id }
  let detail = null;
  // 复验进度：null | { running: bool, rounds: 2, done: 0|1|2 }
  let rescan = null;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    const gapMeta = await api("/api/gap-reports");
    reports = gapMeta.items || [];
    formMarker = gapMeta.form_marker || "旁路快照";
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${t.version_no}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账；点编号看详情</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td><a href="#" class="link" data-batch-detail="${b.id}">${b.id}</a></td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="chip ${b.status === "已作废" ? "chip-void" : ""}">${esc(b.status)}</span></td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>${
              b.status === "待盖印"
                ? `<span class="row-inline">
                    <button type="button" class="ghost" data-pick="${b.id}">盖印</button>
                    <button type="button" class="ghost danger-btn" data-void="${b.id}">作废</button>
                  </span>`
                : ""
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function gapBar(report) {
    const expected = report.expected_count || 0;
    const stamped = report.stamped_count || 0;
    const pct = expected ? Math.round((stamped / expected) * 100) : 0;
    return `
      <div class="gapbar" role="img" aria-label="已盖印占应盖印 ${pct}%">
        <div class="gapbar-fill" style="width:${pct}%"></div>
        <span class="gapbar-txt">已盖印 ${stamped} / 应盖印 ${expected}（${pct}%）· 缺口 ${report.gap_count} · 作废 ${report.voided_count || 0}</span>
      </div>`;
  }

  function progressNode() {
    if (!rescan) return "";
    const steps = [1, 2].map((n) => {
      const state = !rescan.running && rescan.done >= n
        ? "step-done"
        : rescan.running && rescan.done >= n - 1
        ? "step-active"
        : "step-todo";
      return `<span class="step ${state}">第${n}轮复验</span>`;
    });
    return `<div class="progress">${steps.join("<span class=\"step-sep\">→</span>")}</div>`;
  }

  function renderGaps() {
    const latest = reports[0] || null;
    return `
      <h2>缺口对账</h2>
      <div class="cardish recon-card">
        <div class="recon-head">
          <h3>对账卡片</h3>
          <span class="marker">形态：${esc(formMarker)}</span>
        </div>
        ${
          latest
            ? `<p class="muted">最新 #${latest.id} · ${esc(latest.summary)}</p>
               ${gapBar(latest)}
               <p class="muted small">复验轮次：${latest.check_round || 2} 轮 · 截至 ${esc(latest.as_of_date)}</p>`
            : `<p class="muted">尚无对账包，点重扫生成第一份不可变对账包。</p>`
        }
        <div class="row">
          <button type="button" id="btn-gap" ${rescan && rescan.running ? "disabled" : ""}>
            ${rescan && rescan.running ? "重扫中…" : "重扫"}
          </button>
          <label class="date-label">截至日
            <input id="gap-date" value="2026-11-16" />
          </label>
        </div>
        ${progressNode()}
      </div>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口条</th><th>缺口</th><th>作废实物</th><th>形态标记</th><th>摘要</th><th>来源批号</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td><a href="#" class="link" data-report-detail="${r.id}">${r.id}</a></td>
            <td>${esc(r.as_of_date)}</td>
            <td class="gapbar-cell">${gapBar(r)}</td>
            <td><strong>${r.gap_count}</strong></td>
            <td>${r.voided_count || 0}</td>
            <td><span class="marker">${esc(r.form_marker || formMarker)}</span></td>
            <td>${esc(r.summary)}</td>
            <td><a href="#" class="link" data-report-detail="${r.id}">查看来源批号</a></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  async function renderReportDetail(reportId) {
    let report;
    try {
      report = (await api("/api/gap-reports/" + reportId)).report;
    } catch (e) {
      view.innerHTML = `<div class="cardish"><p class="err">${esc(e.message)}</p>
        <button type="button" class="ghost" data-back-gaps>返回缺口列表</button></div>`;
      bindBack();
      return;
    }
    const gapSources = report.sources.filter((s) => s.is_gap);
    const voidSources = report.sources.filter((s) => s.is_voided);
    view.innerHTML = `
      <p><a href="#" class="link" data-back-gaps>← 返回缺口列表</a></p>
      <h2>对账包详情 #${report.id}</h2>
      <div class="cardish">
        <div class="recon-head">
          <h3>形态标记</h3>
          <span class="marker">${esc(report.form_marker)}</span>
        </div>
        <p class="muted">${esc(report.summary)}</p>
        <p class="muted small">截至 ${esc(report.as_of_date)} · 复验 ${report.check_round || 2} 轮 · 生成于 ${esc(report.created_at)}</p>
        ${gapBar(report)}
      </div>
      <h3>缺口来源批号（${gapSources.length}）</h3>
      <table>
        <thead><tr><th>来源批号</th><th>表</th><th>业务日</th><th>实物状态</th><th>标记</th></tr></thead>
        <tbody>
          ${
            gapSources
              .map(
                (s) => `<tr>
              <td class="src-code">${esc(s.run_code)}</td>
              <td>${esc(s.table_name)}</td>
              <td>${esc(s.biz_date)}</td>
              <td><span class="chip">${esc(s.batch_status)}</span></td>
              <td><span class="marker">${esc(report.form_marker)}</span></td>
            </tr>`
              )
              .join("") || `<tr><td colspan="5" class="muted">无缺口</td></tr>`
          }
        </tbody>
      </table>
      <h3>作废批实物（${voidSources.length}）</h3>
      <table>
        <thead><tr><th>来源批号</th><th>表</th><th>业务日</th><th>实物状态</th><th>是否计入应盖印</th></tr></thead>
        <tbody>
          ${
            voidSources
              .map(
                (s) => `<tr>
              <td class="src-code">${esc(s.run_code)}</td>
              <td>${esc(s.table_name)}</td>
              <td>${esc(s.biz_date)}</td>
              <td><span class="chip chip-void">${esc(s.batch_status)}</span></td>
              <td>否（旁路快照留存实物）</td>
            </tr>`
              )
              .join("") || `<tr><td colspan="5" class="muted">无作废批</td></tr>`
          }
        </tbody>
      </table>
      <h3>全部旁路来源快照（${report.sources.length} 行）</h3>
      <table>
        <thead><tr><th>来源批号</th><th>状态</th><th>计应盖印</th><th>缺口</th><th>作废</th></tr></thead>
        <tbody>
          ${report.sources
            .map(
              (s) => `<tr>
            <td class="src-code">${esc(s.run_code)}</td>
            <td>${esc(s.batch_status)}</td>
            <td>${s.counted_as_expected ? "是" : "否"}</td>
            <td>${s.is_gap ? "是" : "否"}</td>
            <td>${s.is_voided ? "是" : "否"}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
    bindBack();
  }

  async function renderBatchDetail(batchId) {
    let batch;
    try {
      batch = (await api("/api/batches/" + batchId)).batch;
    } catch (e) {
      view.innerHTML = `<div class="cardish"><p class="err">${esc(e.message)}</p>
        <button type="button" class="ghost" data-back-batches>返回批次列表</button></div>`;
      bindBack();
      return;
    }
    view.innerHTML = `
      <p><a href="#" class="link" data-back-batches>← 返回批次列表</a></p>
      <h2>批次详情 #${batch.id}</h2>
      <div class="cardish">
        <div class="recon-head">
          <h3>形态标记</h3>
          <span class="marker">实物状态：${esc(batch.status)}</span>
        </div>
        <table>
          <tbody>
            <tr><th>运行码（来源批号）</th><td class="src-code">${esc(batch.run_code)}</td></tr>
            <tr><th>表</th><td>${esc(batch.table_name)}</td></tr>
            <tr><th>业务日</th><td>${esc(batch.biz_date)}</td></tr>
            <tr><th>状态</th><td><span class="chip ${batch.status === "已作废" ? "chip-void" : ""}">${esc(batch.status)}</span></td></tr>
            <tr><th>水印值</th><td>${esc(batch.watermark_value || "-")}</td></tr>
            <tr><th>盖印时间</th><td>${esc(batch.stamped_at || "-")}</td></tr>
            <tr><th>缺口复算口径</th><td>${
              batch.status === "已作废"
                ? "作废实物：入旁路快照、不计应盖印"
                : "在保实物：入旁路快照、计入应盖印"
            }</td></tr>
          </tbody>
        </table>
      </div>`;
    bindBack();
  }

  function bindBack() {
    const backGaps = view.querySelector("[data-back-gaps]");
    if (backGaps)
      backGaps.onclick = (e) => {
        e.preventDefault();
        detail = null;
        tab = "gaps";
        syncDock();
        render();
      };
    const backBatches = view.querySelector("[data-back-batches]");
    if (backBatches)
      backBatches.onclick = (e) => {
        e.preventDefault();
        detail = null;
        tab = "batches";
        syncDock();
        render();
      };
  }

  function render() {
    if (detail && detail.kind === "report") {
      renderReportDetail(detail.id);
      return;
    }
    if (detail && detail.kind === "batch") {
      renderBatchDetail(detail.id);
      return;
    }
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();
    bindCommon();
  }

  function bindCommon() {
    view.querySelectorAll("[data-report-detail]").forEach((el) => {
      el.onclick = (e) => {
        e.preventDefault();
        detail = { kind: "report", id: Number(el.dataset.reportDetail) };
        render();
      };
    });
    view.querySelectorAll("[data-batch-detail]").forEach((el) => {
      el.onclick = (e) => {
        e.preventDefault();
        detail = { kind: "batch", id: Number(el.dataset.batchDetail) };
        render();
      };
    });
    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    view.querySelectorAll("[data-void]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.void);
        const b = batches.find((x) => x.id === id);
        if (!b || !window.confirm(`确认作废批次 ${b.run_code}？作废后实物保留、不再计入应盖印。`)) return;
        setMsg("");
        try {
          await api("/api/batches/" + id + "/void", { method: "POST", body: "{}" });
          await refresh();
          setMsg("批次已作废");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        const day = (document.getElementById("gap-date") || {}).value || "2026-11-16";
        // 复验进度：两轮逐步点亮，最终数字以服务端两轮复验通过的对账包为准
        rescan = { running: true, rounds: 2, done: 0 };
        render();
        const tick = () => {
          if (rescan && rescan.running) {
            rescan.done = Math.min(rescan.done + 1, 1);
            render();
          }
        };
        const timer = setInterval(tick, 180);
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: day }),
          });
          clearInterval(timer);
          rescan = { running: false, rounds: 2, done: 2 };
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          clearInterval(timer);
          rescan = null;
          setMsg(e.message);
          render();
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      detail = null;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
