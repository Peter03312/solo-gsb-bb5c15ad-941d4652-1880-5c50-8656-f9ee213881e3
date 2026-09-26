"""字幕分卷：把 SRT 字幕块切成 K 个时长接近目标的连续卷。

规则：

- 块先按 **入点**（相同入点按 **解析序位**）排序；时间区间 **左闭右开**，
  直接或传递重叠的块划为同一 **无重叠组**，组内块不可拆到不同卷，
  只能在相邻无重叠组之间切分。
- 所有块恰好分入 **K 个非空连续卷**；卷时长 = 卷内最晚出点 − 最早入点，
  须落在 [最短, 最长] 毫秒内（最短 ≤ 目标 ≤ 最长，均为正整数毫秒）。
- 全局最小化 **各卷时长与目标之差的平方和**；同分时选
  **切分处下一卷入点序列字典序最小** 的方案——DP 关键字为
  （总分, 各下一卷入点元组）的字典序，代价可累加、序列可拼接，
  满足最优子结构。
- 时长非正（入点不早于出点）的块指出块号并拒绝分卷；
  约束下不存在可行切分时同样报错（无解）。
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from .parsers import SrtItem, format_tc


@dataclass
class VolumeRow:
    """单卷的分卷结果（按卷号排列）。"""

    volume: int         # 卷号（从 1 开始）
    start_ms: int       # 卷内最早入点
    end_ms: int         # 卷内最晚出点
    start_tc: str
    end_tc: str
    blocks: int         # 卷内字幕块数
    duration_ms: int    # 卷时长 = 最晚出点 − 最早入点
    deviation_ms: int   # 卷时长 − 目标时长
    cost: int           # 偏差平方
    first: int          # 卷内首块在排序后序列中的下标（含）
    last: int           # 卷内末块之后一位（不含）


@dataclass
class VolumePlan:
    """整段分卷方案。"""

    rows: list[VolumeRow]
    total_cost: int                 # 总分：各卷偏差平方和
    groups: int                     # 无重叠组数
    items: tuple[SrtItem, ...]      # 按入点、解析序位排序后的全部块


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def plan_volumes(
    items: list[SrtItem],
    parts: int,
    min_ms: int,
    target_ms: int,
    max_ms: int,
) -> VolumePlan:
    """把字幕块切成 ``parts`` 个连续卷，全局最小化时长偏差平方和。

    时长非正的块、份数不足无重叠组、或时长范围内不存在可行切分时，
    抛带具体原因的 ValueError（调用方应拒绝预览/导出）。
    """
    if parts < 1:
        raise ValueError("份数 K 至少为 1")
    if min_ms <= 0 or target_ms <= 0 or max_ms <= 0:
        raise ValueError("最短、目标、最长时长须为正整数毫秒")
    if not min_ms <= target_ms <= max_ms:
        raise ValueError("时长范围须满足 最短 ≤ 目标 ≤ 最长")
    if not items:
        raise ValueError("SRT 内容为空")

    for pos, item in enumerate(items, start=1):
        if item.end_ms <= item.start_ms:
            raise ValueError(
                f"{_block_label(pos, item.index)}入点不早于出点"
                f"（{item.start_tc} --> {item.end_tc}），时长非正，无法分卷"
            )

    # 按入点、解析序位排序（enumerate 下标即解析序位）
    sorted_items = [
        it for _, it in sorted(
            enumerate(items), key=lambda p: (p[1].start_ms, p[0])
        )
    ]

    # 左闭右开：入点 < 当前组最晚出点 即与本组（传递）重叠，同组不可拆；
    # 入点 == 当前组最晚出点 仅端点相触，不算重叠，可在此切分
    bounds: list[tuple[int, int]] = []   # 每组 (首块下标, 末块后一位)
    seg_first = 0
    seg_end = sorted_items[0].end_ms
    for i in range(1, len(sorted_items)):
        it = sorted_items[i]
        if it.start_ms >= seg_end:
            bounds.append((seg_first, i))
            seg_first = i
            seg_end = it.end_ms
        elif it.end_ms > seg_end:
            seg_end = it.end_ms
    bounds.append((seg_first, len(sorted_items)))

    g = len(bounds)
    if parts > g:
        raise ValueError(
            f"全片仅 {g} 个无重叠组，少于份数 {parts}，"
            "重叠块不可拆到不同卷，无法分卷"
        )

    # 组按入点有序：组最早入点 = 组内首块入点；各组最晚出点严格递增，
    # 故卷 [i, j) 的时长 = 组 j-1 最晚出点 − 组 i 最早入点，O(1) 可得
    g_start = [sorted_items[a].start_ms for a, _ in bounds]
    g_end = [
        max(it.end_ms for it in sorted_items[a:b]) for a, b in bounds
    ]

    # DP：best[k][j] = 前 j 组恰好分 k 卷的 (总分, 各下一卷入点元组)
    best: list[list[tuple[int, tuple[int, ...]] | None]] = [
        [None] * (g + 1) for _ in range(parts + 1)
    ]
    parent: list[list[int | None]] = [
        [None] * (g + 1) for _ in range(parts + 1)
    ]
    best[0][0] = (0, ())
    for k in range(1, parts + 1):
        for j in range(k, g + 1):
            # 最后一卷为组 [i, j)，i 从 k-1（前 i 组恰够 k-1 卷）到 j-1
            for i in range(k - 1, j):
                prev = best[k - 1][i]
                if prev is None:
                    continue
                dur = g_end[j - 1] - g_start[i]
                if dur < min_ms or dur > max_ms:
                    continue
                # 第 1 卷之前无切分，不计入「切分处下一卷入点」序列
                key = prev[1] + ((g_start[i],) if k >= 2 else ())
                cand = (prev[0] + (dur - target_ms) ** 2, key)
                if best[k][j] is None or cand < best[k][j]:
                    best[k][j] = cand
                    parent[k][j] = i

    if best[parts][g] is None:
        raise ValueError(
            f"无解：{len(sorted_items)} 块（{g} 个无重叠组）无法在卷时长 "
            f"{min_ms}~{max_ms} 毫秒的约束下恰好分成 {parts} 个非空连续卷"
        )

    # 回溯切分点：cuts 为各卷覆盖的组区间 [a, b)
    cuts: list[tuple[int, int]] = []
    k, j = parts, g
    while k:
        i = parent[k][j]
        cuts.append((i, j))
        j = i
        k -= 1
    cuts.reverse()

    rows: list[VolumeRow] = []
    for vol, (a, b) in enumerate(cuts, start=1):
        first = bounds[a][0]
        last = bounds[b - 1][1]
        start_ms = g_start[a]
        end_ms = g_end[b - 1]
        dur = end_ms - start_ms
        rows.append(
            VolumeRow(
                volume=vol,
                start_ms=start_ms,
                end_ms=end_ms,
                start_tc=format_tc(start_ms),
                end_tc=format_tc(end_ms),
                blocks=last - first,
                duration_ms=dur,
                deviation_ms=dur - target_ms,
                cost=(dur - target_ms) ** 2,
                first=first,
                last=last,
            )
        )
    return VolumePlan(
        rows=rows,
        total_cost=best[parts][g][0],
        groups=g,
        items=tuple(sorted_items),
    )


def build_volume_srt(items: tuple[SrtItem, ...], base_ms: int) -> str:
    """生成单卷 SRT：各块时间减去该卷最早入点，保留正文，从 1 编号。"""
    blocks = []
    for n, it in enumerate(items, start=1):
        lines = [
            str(n),
            f"{format_tc(it.start_ms - base_ms)}"
            f" --> {format_tc(it.end_ms - base_ms)}",
        ]
        lines.extend(it.body_lines or (it.raw_text,))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def build_volume_zip(plan: VolumePlan) -> bytes:
    """打包全部卷为 ZIP：每卷一个 SRT 文件（volume_01.srt ……）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in plan.rows:
            text = build_volume_srt(
                plan.items[row.first:row.last], row.start_ms
            )
            zf.writestr(
                f"volume_{row.volume:02d}.srt", text.encode("utf-8")
            )
    return buf.getvalue()
