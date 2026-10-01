"""字幕分卷：重叠组切分 + K 卷最优 DP（含暴力枚举对拍）+ 接口测试。

运行：python -m pytest tests/ -q
"""
import io
import itertools
import random
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.volumes import (
    build_overlap_groups,
    build_volume_srt,
    build_volumes_zip,
    plan_volumes,
    sort_items,
    validate_positive_duration,
)

client = TestClient(app)


def _tc(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _srt(blocks, declared=None):
    """blocks: [(start_ms, end_ms, 正文(可为多行)), ...]；声明序号可选。"""
    out = []
    for i, (s, e, text) in enumerate(blocks, start=1):
        idx = declared[i - 1] if declared else i
        out.append(f"{idx}\n{_tc(s)} --> {_tc(e)}\n{text}")
    return "\n\n".join(out) + "\n"


def _items(blocks, declared=None):
    return parse_srt(_srt(blocks, declared))


# ---------- 排序与重叠组 ----------

def test_sort_by_start_then_parse_order():
    items = _items([
        (5000, 6000, "后写的"),
        (1000, 2000, "先写的甲"),
        (1000, 3000, "先写的乙"),
        (3000, 4000, "中间的"),
    ])
    ordered = sort_items(items)
    assert [it.raw_text for it in ordered] == [
        "先写的甲", "先写的乙", "中间的", "后写的",
    ]


def test_touching_intervals_are_separate_groups():
    # 左闭右开：出点 == 下一入点（端点相触）不算重叠
    groups = build_overlap_groups(sort_items(_items([
        (0, 5000, "甲"),
        (5000, 8000, "乙"),
    ])))
    assert len(groups) == 2
    assert [g.start_ms for g in groups] == [0, 5000]


def test_direct_and_transitive_overlap_merged():
    # 甲 [0,5) 与 乙 [3,7) 重叠，乙 与 丙 [6,9) 重叠；甲丙不直接重叠，
    # 但经乙传递重叠，必须同组
    groups = build_overlap_groups(sort_items(_items([
        (0, 5000, "甲"),
        (3000, 7000, "乙"),
        (6000, 9000, "丙"),
        (9000, 10000, "丁"),
    ])))
    assert len(groups) == 2
    assert len(groups[0].items) == 3
    assert groups[0].start_ms == 0 and groups[0].end_ms == 9000
    assert len(groups[1].items) == 1


# ---------- 非正时长 ----------

def test_non_positive_duration_rejected():
    items = _items([
        (1000, 3000, "正常块"),
        (5000, 5000, "零时长块"),
    ])
    with pytest.raises(ValueError, match="第 2 块.*时长非正"):
        validate_positive_duration(items)


def test_negative_like_duration_rejected():
    # SRT 解析不允许出点早于入点的块进入分卷（这里直接构造数据触发）
    items = _items([(1000, 500, "倒序块")])
    with pytest.raises(ValueError, match="第 1 块"):
        validate_positive_duration(items)


def test_error_uses_declared_index():
    items = _items(
        [(1000, 3000, "甲"), (5000, 5000, "零时长")],
        declared=[7, 9],
    )
    with pytest.raises(ValueError, match="第 2 块（声明序号 9）"):
        validate_positive_duration(items)


# ---------- 最优分卷 ----------

def test_k1_single_volume_spans_all_groups():
    vols = plan_volumes(_items([
        (0, 5000, "甲"),
        (6000, 7000, "乙"),
    ]), 1, 1000, 5000, 60000)
    assert len(vols) == 1
    assert vols[0].start_ms == 0 and vols[0].end_ms == 7000
    assert vols[0].duration_ms == 7000
    assert len(vols[0].items) == 2


def test_overlapping_blocks_cannot_split():
    # 两个重叠块、却要求 2 卷 → 无解
    with pytest.raises(ValueError, match="只能形成 1 个可切分段"):
        plan_volumes(_items([
            (0, 5000, "甲"),
            (4000, 6000, "乙"),
        ]), 2, 1000, 5000, 60000)


def test_no_solution_when_duration_bounds_unsatisfiable():
    # 两组，卷长分别为 5000 与 1000；要求最短 3000 → 无解
    with pytest.raises(ValueError, match="不存在满足约束的切分方案"):
        plan_volumes(_items([
            (0, 5000, "甲"),
            (5000, 6000, "乙"),
        ]), 2, 3000, 4000, 60000)


def test_invalid_duration_order_rejected():
    items = _items([(0, 1000, "甲")])
    with pytest.raises(ValueError, match="最短时长 ≤ 目标时长 ≤ 最长时长"):
        plan_volumes(items, 1, 5000, 4000, 6000)


def test_tie_prefers_lex_smaller_cut_starts():
    # 五组时长均为 2 或 4 秒、组间留 1 秒间隙（端点不重叠）：
    # 组 [0,4),[5,7),[8,12),[13,15),[16,20)；K=3、目标 8s
    #   切法 [2,4]：卷长 7s/7s/4s → 1+1+16 = 18M，切点 8000,16000
    #   切法 [1,3]：卷长 4s/7s/7s → 16+1+1 = 18M，切点 5000,13000
    # 总分相同，切点序列字典序 (5000,13000) < (8000,16000) → 取后者
    blocks = [
        (0, 4000, "g1"),
        (5000, 7000, "g2"),
        (8000, 12000, "g3"),
        (13000, 15000, "g4"),
        (16000, 20000, "g5"),
    ]
    vols = plan_volumes(_items(blocks), 3, 1000, 8000, 20000)
    starts = [v.start_ms for v in vols]
    assert starts == [0, 5000, 13000]
    assert [v.duration_ms for v in vols] == [4000, 7000, 7000]
    assert sum(v.score for v in vols) == 18_000_000


def _brute_plan(starts, ends, k, min_ms, target_ms, max_ms):
    """枚举 g 个组之间选 k-1 个切点的全部方案，返回 (最优总分, 切点入点元组)。

    starts/ends 为各组首入点、末出点；平局按切点入点元组字典序取小。
    """
    g = len(starts)
    best = None
    for cuts in itertools.combinations(range(1, g), k - 1):
        bounds = (0,) + cuts + (g,)
        durs = [ends[bounds[t + 1] - 1] - starts[bounds[t]] for t in range(k)]
        if not all(min_ms <= d <= max_ms for d in durs):
            continue
        score = sum((d - target_ms) ** 2 for d in durs)
        key = (score, tuple(starts[c] for c in cuts))
        if best is None or key < best:
            best = key
    return best


def test_plan_matches_bruteforce_random():
    rng = random.Random(20261001)
    for _ in range(400):
        g = rng.randint(1, 7)
        # 构造 g 个端点相触/有间隙的组，每组 1 块（组内用单独块即可）
        t = 0
        blocks = []
        starts, ends = [], []
        for _ in range(g):
            t += rng.choice([0, 500, 1000])      # 组间间隙
            s = t
            e = s + rng.randint(1, 9) * 1000
            blocks.append((s, e, f"b{len(blocks)}"))
            starts.append(s)
            ends.append(e)
            t = e
        k = rng.randint(1, min(g, 5))
        target = rng.randint(2, 12) * 1000
        min_ms = rng.randint(1, max(1, target // 1000)) * 1000
        max_ms = target + rng.randint(0, 10) * 1000
        expected = _brute_plan(starts, ends, k, min_ms, target, max_ms)
        if expected is None:
            with pytest.raises(ValueError):
                plan_volumes(_items(blocks), k, min_ms, target, max_ms)
            continue
        vols = plan_volumes(_items(blocks), k, min_ms, target, max_ms)
        got_cuts = tuple(v.start_ms for v in vols[1:])
        got_score = sum(v.score for v in vols)
        assert (got_score, got_cuts) == expected
        # 每卷非空、块总数守恒
        assert all(len(v.items) >= 1 for v in vols)
        assert sum(len(v.items) for v in vols) == g


def test_plan_with_overlap_matches_bruteforce_random():
    """随机制造组内多块（含传递重叠），对拍 DP 与暴力。"""
    rng = random.Random(424242)
    for _ in range(200):
        g = rng.randint(1, 6)
        blocks = []
        t = 0
        for _ in range(g):
            t += rng.choice([0, 800])
            s0 = t
            nb = rng.randint(1, 3)
            cursor = s0
            e_last = s0
            for _ in range(nb):
                bs = cursor + rng.randint(0, 1200)
                be = bs + rng.randint(500, 4000)
                blocks.append((bs, be, "x"))
                cursor = bs
                e_last = max(e_last, be)
            t = e_last
        items = _items(blocks)
        # 先按与实现一致的方式合并重叠组，再在组边界上枚举切分
        groups = build_overlap_groups(sort_items(items))
        starts = [grp.start_ms for grp in groups]
        ends = [grp.end_ms for grp in groups]
        g = len(groups)
        k = rng.randint(1, min(g, 4))
        target = rng.randint(2, 10) * 1000
        min_ms = rng.randint(1, max(1, target // 1000)) * 1000
        max_ms = target + rng.randint(0, 8) * 1000
        expected = _brute_plan(starts, ends, k, min_ms, target, max_ms)
        if expected is None:
            with pytest.raises(ValueError):
                plan_volumes(items, k, min_ms, target, max_ms)
            continue
        vols = plan_volumes(items, k, min_ms, target, max_ms)
        got = (sum(v.score for v in vols), tuple(v.start_ms for v in vols[1:]))
        assert got == expected
        assert sum(len(v.items) for v in vols) == len(items)


# ---------- 导出 ----------

def test_build_volume_srt_rebases_and_renumbers():
    vols = plan_volumes(_items([
        (0, 5000, "甲"),
        (4000, 6000, "乙（重叠）"),
        (10000, 12000, "丙"),
    ]), 2, 1000, 5000, 20000)
    # 卷1 = 重叠组[0,6)，卷2 = [10,12)
    assert len(vols) == 2
    srt1 = build_volume_srt(vols[0])
    assert srt1.strip().split("\n\n") == [
        "1\n00:00:00,000 --> 00:00:05,000\n甲",
        "2\n00:00:04,000 --> 00:00:06,000\n乙（重叠）",
    ]
    srt2 = build_volume_srt(vols[1])
    assert srt2.strip().split("\n\n") == [
        "1\n00:00:00,000 --> 00:00:02,000\n丙",
    ]


def test_build_volume_srt_keeps_multiline_body():
    vols = plan_volumes(_items([
        (1000, 5000, "第一行\n第二行"),
        (9000, 12000, "另一卷"),
    ]), 2, 1000, 3000, 20000)
    srt0 = build_volume_srt(vols[0])
    # 时间减去本卷最早入点 1000ms；正文两行保留
    assert srt0.startswith("1\n00:00:00,000 --> 00:00:04,000\n第一行\n第二行\n")


def test_zip_contains_numbered_srt_files():
    vols = plan_volumes(_items([
        (0, 2000, "甲"),
        (5000, 7000, "乙"),
        (9000, 11000, "丙"),
    ]), 3, 1000, 2000, 10000)
    data = build_volumes_zip(vols)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.namelist() == ["volume01.srt", "volume02.srt", "volume03.srt"]
    for name in zf.namelist():
        assert zf.read(name).decode("utf-8").startswith("1\n")


# ---------- 接口 ----------

SAMPLE = _srt([
    (0, 5000, "第一组·甲"),
    (3000, 4500, "第一组·乙"),
    (5000, 8000, "第二组"),
    (10000, 12000, "第三组"),
    (15000, 21000, "第四组·甲"),
    (16000, 18000, "第四组·乙"),
    (22000, 25000, "第五组·甲"),
    (24000, 26000, "第五组·乙"),
])


def test_api_volumes_preview():
    resp = client.post("/api/volumes", json={
        "srt": SAMPLE, "k": 3,
        "min_ms": 1000, "target_ms": 10000, "max_ms": 20000,
    })
    assert resp.status_code == 200
    data = resp.json()
    s = data["summary"]
    assert s["k"] == 3
    assert s["total_blocks"] == 8
    assert s["total_score"] == sum(v["score"] for v in data["volumes"])
    vols = data["volumes"]
    assert [v["number"] for v in vols] == [1, 2, 3]
    # 最优切法切点 5000、15000（组边界 [1,3]）：卷1=组1（块1-2），
    # 卷2=组2-3（块3-4），卷3=组4-5（块5-8）
    assert [v["block_count"] for v in vols] == [2, 2, 4]
    assert vols[0]["start_tc"] == "00:00:00,000"
    assert vols[0]["end_tc"] == "00:00:05,000"
    assert vols[0]["duration_ms"] == 5000
    assert vols[1]["start_ms"] == 5000
    assert vols[1]["end_ms"] == 12000
    assert vols[1]["duration_ms"] == 7000
    assert vols[2]["start_ms"] == 15000
    assert vols[2]["end_ms"] == 26000
    assert vols[2]["duration_ms"] == 11000
    assert s["total_score"] == 35_000_000
    assert all(1000 <= v["duration_ms"] <= 20000 for v in vols)


def test_api_volumes_lex_tie():
    # 五组时长 4/2/4/2/4 秒（组间 1 秒间隙），K=3、目标 8s 存在两套同分
    # 切法，切点序列字典序较小者（5000,13000）应胜出
    srt = _srt([
        (0, 4000, "g1"),
        (5000, 7000, "g2"),
        (8000, 12000, "g3"),
        (13000, 15000, "g4"),
        (16000, 20000, "g5"),
    ])
    resp = client.post("/api/volumes", json={
        "srt": srt, "k": 3,
        "min_ms": 1000, "target_ms": 8000, "max_ms": 20000,
    })
    assert resp.status_code == 200
    starts = [v["start_ms"] for v in resp.json()["volumes"]]
    assert starts == [0, 5000, 13000]


def test_api_volumes_zip_download():
    resp = client.post("/api/volumes.zip", json={
        "srt": SAMPLE, "k": 3,
        "min_ms": 1000, "target_ms": 10000, "max_ms": 20000,
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "subtitle-volumes.zip" in resp.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.namelist() == ["volume01.srt", "volume02.srt", "volume03.srt"]
    vol1 = zf.read("volume01.srt").decode("utf-8")
    # 卷 1 = 组1（块1-2），最早入点为 0：时间码不变
    assert "00:00:00,000 --> 00:00:05,000" in vol1
    assert "00:00:03,000 --> 00:00:04,500" in vol1
    vol3 = zf.read("volume03.srt").decode("utf-8")
    # 卷 3 = 组4-5（块5-8），最早入点 15000：22000→7000、24000→9000
    assert "00:00:07,000 --> 00:00:10,000" in vol3
    assert "00:00:09,000 --> 00:00:11,000" in vol3
    # 重叠块（块6 与块5）必须同卷
    assert "00:00:01,000 --> 00:00:03,000" in vol3


def test_api_volumes_no_solution_400():
    resp = client.post("/api/volumes", json={
        "srt": SAMPLE, "k": 8,
        "min_ms": 1000, "target_ms": 5000, "max_ms": 60000,
    })
    assert resp.status_code == 400
    assert "可切分段" in resp.json()["detail"]

    resp = client.post("/api/volumes", json={
        "srt": SAMPLE, "k": 5,
        "min_ms": 1000, "target_ms": 5000, "max_ms": 1500,
    })
    assert resp.status_code == 400
    assert "最短时长 ≤ 目标时长 ≤ 最长时长" in resp.json()["detail"]


def test_api_volumes_non_positive_block_400():
    srt = _srt([(0, 5000, "甲"), (8000, 8000, "零时长")])
    resp = client.post("/api/volumes", json={
        "srt": srt, "k": 1,
        "min_ms": 1000, "target_ms": 5000, "max_ms": 60000,
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "第 2 块" in detail and "时长非正" in detail


def test_api_volumes_validation_limits():
    payload = {
        "srt": _srt([(0, 1000, "甲")]),
        "k": 1, "min_ms": 1000, "target_ms": 5000, "max_ms": 60000,
    }
    for bad in (
        {"k": 0}, {"k": 21},
        {"min_ms": 0}, {"target_ms": -1},
        {"min_ms": 9000, "target_ms": 5000, "max_ms": 60000},
    ):
        p = {**payload, **bad}
        resp = client.post("/api/volumes", json=p)
        assert resp.status_code in (400, 422), bad


def test_api_volumes_block_limit():
    blocks = [(i * 3000, i * 3000 + 1000, f"块{i}") for i in range(201)]
    resp = client.post("/api/volumes", json={
        "srt": _srt(blocks), "k": 1,
        "min_ms": 1000, "target_ms": 600000, "max_ms": 900000,
    })
    assert resp.status_code == 400
    assert "200" in resp.json()["detail"]


def test_api_volumes_empty_and_bad_srt():
    resp = client.post("/api/volumes", json={
        "srt": "", "k": 1,
        "min_ms": 1000, "target_ms": 5000, "max_ms": 60000,
    })
    assert resp.status_code == 400
    resp = client.post("/api/volumes", json={
        "srt": "没有时间码", "k": 1,
        "min_ms": 1000, "target_ms": 5000, "max_ms": 60000,
    })
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]
