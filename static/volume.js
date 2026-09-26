"use strict";

// 字幕分卷：粘贴 SRT（最多 200 块）并设置份数 K（1～20）与最短/目标/最长
// 时长（正整数毫秒，最短 ≤ 目标 ≤ 最长）；块按入点排序后在相邻无重叠组之间
// 切分，全局最小化各卷时长与目标的平方偏差；预览各卷起止/块数/时长与总分，
// 可下载每卷一个 SRT 的 ZIP（各卷时间减去该卷最早入点、保留正文、从 1 编号）。
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("vlSrt"),
    parts: $("vlParts"),
    min: $("vlMin"),
    target: $("vlTarget"),
    max: $("vlMax"),
    runBtn: $("vlRunBtn"),
    clearBtn: $("vlClearBtn"),
    status: $("vlStatus"),
    summary: $("vlSummary"),
    resultWrap: $("vlResultWrap"),
    body: $("vlBody"),
    downloadBtn: $("vlDownloadBtn"),
  };

  // 前后两段各 10000ms、中间留空：默认参数下恰好均分两卷、总分 0
  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:04,000",
    "前段第一句",
    "",
    "2",
    "00:00:04,500 --> 00:00:07,500",
    "前段第二句",
    "",
    "3",
    "00:00:08,000 --> 00:00:11,000",
    "前段第三句",
    "",
    "4",
    "00:00:20,000 --> 00:00:23,000",
    "后段第一句",
    "",
    "5",
    "00:00:23,500 --> 00:00:26,500",
    "后段第二句",
    "",
    "6",
    "00:00:27,000 --> 00:00:30,000",
    "后段第三句",
    "",
  ].join("\n");

  $("vlSample").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
  });

  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  // 清空旧结果并禁用下载（报错与清空时共用）
  function clearResults() {
    els.summary.classList.add("hidden");
    els.resultWrap.classList.add("hidden");
    els.downloadBtn.disabled = true;
  }

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    clearResults();
    setStatus("", false);
  });

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function readParams() {
    const parts = Number(els.parts.value);
    const minMs = Number(els.min.value);
    const targetMs = Number(els.target.value);
    const maxMs = Number(els.max.value);
    if (![parts, minMs, targetMs, maxMs].every(Number.isInteger)) {
      throw new Error("份数与时长均须为整数");
    }
    if (parts < 1 || parts > 20) {
      throw new Error("份数 K 须在 1～20 之间");
    }
    if (minMs <= 0 || targetMs <= 0 || maxMs <= 0) {
      throw new Error("最短、目标、最长时长须为正整数毫秒");
    }
    if (!(minMs <= targetMs && targetMs <= maxMs)) {
      throw new Error("时长范围须满足 最短 ≤ 目标 ≤ 最长");
    }
    return { parts, min_ms: minMs, target_ms: targetMs, max_ms: maxMs };
  }

  function render(data) {
    const s = data.summary;
    $("vlCntVolumes").textContent = s.volumes;
    $("vlCntBlocks").textContent = s.blocks;
    $("vlCntGroups").textContent = s.groups;
    $("vlTargetShown").textContent = s.target_ms;
    $("vlTotalCost").textContent = s.total_cost;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      const dev = r.deviation_ms;
      tr.innerHTML = `
        <td class="cell-idx">${r.volume}</td>
        <td class="cell-tc">${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}</td>
        <td class="cell-num">${r.blocks}</td>
        <td class="cell-num">${r.duration_ms}</td>
        <td class="cell-num">${dev > 0 ? "+" : ""}${dev}</td>
        <td class="cell-num">${r.cost}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
    els.downloadBtn.disabled = false;
  }

  let lastRequest = null;

  async function run() {
    let params;
    try {
      params = readParams();
    } catch (e) {
      lastRequest = null;
      clearResults();
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, ...params };

    els.runBtn.disabled = true;
    setStatus("分卷中……", false);
    try {
      const resp = await fetch("/api/volume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `请求失败（${resp.status}）` }));
        throw new Error(err.detail || `请求失败（${resp.status}）`);
      }
      const data = await resp.json();
      render(data);
      setStatus(
        `完成：${data.summary.volumes} 卷，总分 ${data.summary.total_cost}`,
        false
      );
    } catch (e) {
      // 无解、非正时长块等错误：清空旧结果并禁用下载
      lastRequest = null;
      clearResults();
      setStatus(e.message, true);
    } finally {
      els.runBtn.disabled = false;
    }
  }

  async function downloadZip() {
    if (!lastRequest) {
      setStatus("请先执行分卷", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/volume.zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "下载失败" }));
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "volumes.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      els.downloadBtn.disabled = false;
    }
  }

  els.runBtn.addEventListener("click", run);
  els.downloadBtn.addEventListener("click", downloadZip);
})();
