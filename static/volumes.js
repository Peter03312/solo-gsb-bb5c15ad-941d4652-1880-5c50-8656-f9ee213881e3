"use strict";

// 字幕分卷：粘贴 SRT（最多 200 块），给出版数 K 与每卷最短/目标/最长时长，
// 在不拆开直接或传递重叠块的前提下，沿重叠组边界求 K 个非空连续卷，
// 最小化各卷时长与目标之差的平方和，平局取切分处下一卷入点序列字典序最小者。
// 预览各卷原起止时间、块数、时长与总分；下载 ZIP（各卷时间码减去本卷最早入点）。
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("vlSrt"),
    k: $("vlK"),
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

  // 8 块、5 个互不重叠（端点相触不算重叠）的重叠组；块 2 与块 1 重叠，
  // 块 8 与块 7 重叠。默认 K=3、目标 10000ms 时存在总分相同的两套切法，
  // 平局应取切分处下一卷入点序列字典序最小者（切点尽量靠前）。
  const SAMPLE_SRT = [
    "1",
    "00:00:00,000 --> 00:00:05,000",
    "第一组·甲",
    "",
    "2",
    "00:00:03,000 --> 00:00:04,500",
    "第一组·乙（与甲重叠，不可拆开）",
    "",
    "3",
    "00:00:05,000 --> 00:00:08,000",
    "第二组（端点相触即可切分）",
    "",
    "4",
    "00:00:10,000 --> 00:00:12,000",
    "第三组",
    "",
    "5",
    "00:00:15,000 --> 00:00:21,000",
    "第四组·甲",
    "",
    "6",
    "00:00:16,000 --> 00:00:18,000",
    "第四组·乙（传递重叠，同卷）",
    "",
    "7",
    "00:00:22,000 --> 00:00:25,000",
    "第五组·甲",
    "",
    "8",
    "00:00:24,000 --> 00:00:26,000",
    "第五组·乙（与甲重叠）",
    "",
  ].join("\n");

  $("vlSample").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    clearResult();
    setStatus("", false);
  });

  function clearResult() {
    lastRequest = null;
    els.summary.classList.add("hidden");
    els.resultWrap.classList.add("hidden");
    els.body.replaceChildren();
  }

  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  function readPosInt(input, label) {
    const v = Number(input.value);
    if (!Number.isInteger(v) || v <= 0) {
      throw new Error(`${label}须为正整数毫秒`);
    }
    return v;
  }

  function render(data) {
    const s = data.summary;
    $("vlCntK").textContent = s.k;
    $("vlCntBlocks").textContent = s.total_blocks;
    $("vlRange").textContent = `${s.min_ms}～${s.max_ms}`;
    $("vlTargetShown").textContent = s.target_ms;
    $("vlTotalScore").textContent = s.total_score;

    const frag = document.createDocumentFragment();
    for (const v of data.volumes) {
      const tr = document.createElement("tr");
      tr.className = "row-match";
      tr.innerHTML = `
        <td class="cell-idx">${v.number}</td>
        <td class="cell-tc">${v.start_tc}</td>
        <td class="cell-tc">${v.end_tc}</td>
        <td class="cell-num">${v.block_count}</td>
        <td class="cell-num">${v.duration_ms}</td>
        <td class="cell-num">${v.score}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    const k = parseInt(els.k.value, 10);
    let minMs, targetMs, maxMs;
    if (!Number.isInteger(k) || k < 1 || k > 20) {
      clearResult();
      setStatus("份数 K 须为 1～20 之间的整数", true);
      return;
    }
    try {
      minMs = readPosInt(els.min, "最短时长");
      targetMs = readPosInt(els.target, "目标时长");
      maxMs = readPosInt(els.max, "最长时长");
    } catch (e) {
      clearResult();
      setStatus(e.message, true);
      return;
    }
    if (!(minMs <= targetMs && targetMs <= maxMs)) {
      clearResult();
      setStatus("时长参数须满足 最短时长 ≤ 目标时长 ≤ 最长时长", true);
      return;
    }
    lastRequest = {
      srt: els.srt.value, k,
      min_ms: minMs, target_ms: targetMs, max_ms: maxMs,
    };

    els.runBtn.disabled = true;
    setStatus("分卷求解中……", false);
    try {
      const resp = await fetch("/api/volumes", {
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
        `完成：${data.summary.total_blocks} 块分为 ${data.summary.k} 卷，` +
        `总分 ${data.summary.total_score}`,
        false
      );
    } catch (e) {
      // 无解、非正时长块、解析失败等错误：清空旧结果并禁用下载
      clearResult();
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
      const resp = await fetch("/api/volumes.zip", {
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
      a.download = "subtitle-volumes.zip";
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
