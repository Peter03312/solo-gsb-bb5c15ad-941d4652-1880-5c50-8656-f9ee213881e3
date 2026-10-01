"""字幕分卷：把一段 SRT 切成 K 个非空连续卷。

规则：

- 全部块先按**入点升序、同入点按解析序位**排序，分卷只沿该序进行。
- 时间区间**左闭右开**：两块时间有交集（含传递重叠）即不可拆开，
  必须同卷；因此先把直接或传递重叠的块合并为**重叠组**，切分只允许
  发生在**相邻且互不重叠的组之间**。
- 每卷时长 = 卷内最晚出点 − 最早入点，须落在
  [最短时长, 最长时长] 内；所有块恰好分入 **K（1～20）个非空连续卷**。
- 目标：全局最小化各卷 ``(时长 − 目标时长)²`` 之和；总分相同的方案，
  取**切分处下一卷入点序列**（即第 2..K 卷入点，按卷序）字典序最小者。
- 无解（重叠组数少于 K、或任何切法都满足不了时长区间）时抛出
  ValueError；入点不早于出点（非正时长）的块由调用方先校验并报错。
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from .parsers import SrtItem, format_tc


@dataclass
class OverlapGroup:
    """直接或传递重叠的块构成的不可拆分组（按入点排序后）。"""

    start_ms: int   # 组内最早入点
    end_ms: int     # 组内最晚出点（严格大于上一组的 end_ms）
    items: list[SrtItem]


@dataclass
class Volume:
    """分卷结果中的一卷。"""

    number: int           # 卷号（从 1 开始）
    items: list[SrtItem]  # 卷内字幕块（已按入点、解析序位排序）
    start_ms: int         # 卷内最早入点
    end_ms: int           # 卷内最晚出点
    target_ms: int        # 目标时长（毫秒）

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def score(self) -> int:
        diff = self.duration_ms - self.target_ms
        return diff * diff

    @property
    def start_tc(self) -> str:
        return format_tc(self.start_ms)

    @property
    def end_tc(self) -> str:
        return format_tc(self.end_ms)


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def validate_positive_duration(items: list[SrtItem]) -> None:
    """入点不早于出点（非正时长）的块无法分卷，按解析序位指出首个。"""
    for pos, item in enumerate(items, start=1):
        if item.start_ms >= item.end_ms:
            raise ValueError(
                f"{_block_label(pos, item.index)}入点不早于出点"
                f"（{item.start_tc} --> {item.end_tc}），时长非正，无法分卷"
            )


def sort_items(items: list[SrtItem]) -> list[SrtItem]:
    """按入点升序、同入点按解析序位（即解析结果中的原始下标）排序。"""
    return [it for _, it in sorted(enumerate(items), key=lambda p: (p[1].start_ms, p[0]))]


def build_overlap_groups(items: list[SrtItem]) -> list[OverlapGroup]:
    """把已按入点排序的块合并为直接/传递重叠组。

    区间左闭右开：当前块入点 < 组内最晚出点即为重叠（仅端点相触不算）。
    """
    groups: list[OverlapGroup] = []
    for it in items:
        if groups and it.start_ms < groups[-1].end_ms:
            grp = groups[-1]
            grp.items.append(it)
            if it.end_ms > grp.end_ms:
                grp.end_ms = it.end_ms
        else:
            groups.append(OverlapGroup(it.start_ms, it.end_ms, [it]))
    return groups


def plan_volumes(
    items: list[SrtItem],
    k: int,
    min_ms: int,
    target_ms: int,
    max_ms: int,
) -> list[Volume]:
    """求 K 卷最优分卷方案；无解时抛 ValueError。

    动态规划：``dp[k0][j]`` 为前 ``j`` 个重叠组分成 ``k0`` 卷时的最优值，
    值为 ``(总分, 切分处下一卷入点元组)``——先比总分，再比入点序列字典序。
    卷长区间 [i, j) 的时长 = 组 j-1 的最晚出点 − 组 i 的最早入点
    （各组入点严格递增、组内入点不小于本组首入点）。
    """
    if not items:
        return []
    if min_ms <= 0 or target_ms <= 0 or max_ms <= 0:
        raise ValueError("最短、目标、最长时长均须为正整数毫秒")
    if not min_ms <= target_ms <= max_ms:
        raise ValueError("时长参数须满足 最短时长 ≤ 目标时长 ≤ 最长时长")

    ordered = sort_items(items)
    groups = build_overlap_groups(ordered)
    g = len(groups)
    if not 1 <= k <= 20:
        raise ValueError("份数 K 须为 1～20 之间的整数")
    if k > g:
        raise ValueError(
            f"无法分成 {k} 卷：存在直接或传递重叠的块必须同卷，"
            f"全部块只能形成 {g} 个可切分段，少于份数 {k}"
        )

    def span(i: int, j: int) -> int:
        """第 i..j-1 个组（连续）合成一卷时的时长。"""
        return groups[j - 1].end_ms - groups[i].start_ms

    # dp[k0][j] = (总分, 切分处下一卷入点元组)；不可行为 None
    dp: list[list[tuple[int, tuple[int, ...]] | None]] = [
        [None] * (g + 1) for _ in range(k + 1)
    ]
    dp[0][0] = (0, ())
    for k0 in range(1, k + 1):
        for j in range(k0, g + 1):
            best: tuple[int, tuple[int, ...]] | None = None
            for i in range(k0 - 1, j):
                prev = dp[k0 - 1][i]
                if prev is None:
                    continue
                dur = span(i, j)
                if not min_ms <= dur <= max_ms:
                    continue
                diff = dur - target_ms
                # 切点在组 i 之前；i == 0 是首卷起点，不进入“切分处”序列
                cuts = prev[1] + ((groups[i].start_ms,) if i > 0 else ())
                cand = (prev[0] + diff * diff, cuts)
                if best is None or cand < best:
                    best = cand
            dp[k0][j] = best

    final = dp[k][g]
    if final is None:
        raise ValueError(
            f"无法把字幕分成 {k} 卷且每卷时长都在 "
            f"{min_ms}～{max_ms} 毫秒之间：不存在满足约束的切分方案"
        )

    # 由切分处入点反解组边界（各组首入点严格递增）
    cut_starts = final[1]
    bounds = [0]
    for st in cut_starts:
        gi = bounds[-1]
        while groups[gi].start_ms < st:
            gi += 1
        bounds.append(gi)
    bounds.append(g)

    volumes: list[Volume] = []
    for vnum in range(1, k + 1):
        lo, hi = bounds[vnum - 1], bounds[vnum]
        vol_items = [it for grp in groups[lo:hi] for it in grp.items]
        volumes.append(
            Volume(
                number=vnum,
                items=vol_items,
                start_ms=min(it.start_ms for it in vol_items),
                end_ms=max(it.end_ms for it in vol_items),
                target_ms=target_ms,
            )
        )
    return volumes


def build_volume_srt(volume: Volume) -> str:
    """生成单卷 SRT：时间码减去该卷最早入点，保留正文，序号从 1 开始。"""
    base = volume.start_ms
    blocks = []
    for new_idx, it in enumerate(volume.items, start=1):
        blocks.append(
            f"{new_idx}\n"
            f"{format_tc(it.start_ms - base)} --> {format_tc(it.end_ms - base)}\n"
            + "\n".join(it.body_lines)
        )
    return "\n\n".join(blocks) + "\n"


def build_volumes_zip(volumes: list[Volume]) -> bytes:
    """把各卷 SRT 打包为 ZIP：文件名 volume01.srt、volume02.srt……"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for vol in volumes:
            zf.writestr(f"volume{vol.number:02d}.srt", build_volume_srt(vol))
    return buf.getvalue()
