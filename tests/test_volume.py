"""字幕分卷：分组/DP 选优/平局字典序算法单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.volume import build_volume_srt, build_volume_zip, plan_volumes

client = TestClient(app)


def _tc(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _srt(spans, declared=None):
    """spans: [(start_ms, end_ms, text), ...]；declared 可指定声明序号。"""
    blocks = []
    for i, (s, e, text) in enumerate(spans, start=1):
        idx = declared[i - 1] if declared else i
        blocks.append(f"{idx}\n{_tc(s)} --> {_tc(e)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _plan(spans, parts=2, min_ms=1, target_ms=2000, max_ms=10**7,
          declared=None):
    return plan_volumes(
        parse_srt(_srt(spans, declared)), parts, min_ms, target_ms, max_ms
    )


# ---------- 分组与排序 ----------

def test_touching_blocks_are_separable():
    # 左闭右开：[0,1000) 与 [1000,2000) 仅端点相触，不算重叠，可切分
    plan = _plan(
        [(0, 1000, "甲"), (1000, 2000, "乙")],
        parts=2, target_ms=1000,
    )
    assert plan.groups == 2
    assert [(r.start_ms, r.end_ms, r.blocks) for r in plan.rows] == [
        (0, 1000, 1),
        (1000, 2000, 1),
    ]
    assert plan.total_cost == 0


def test_transitive_overlap_forced_same_volume():
    # 块 1∩块 2、块 2∩块 3 传递重叠 → 三块同组不可拆；块 4 独立
    plan = _plan(
        [
            (0, 2000, "甲"),
            (1000, 3000, "乙"),
            (2500, 4000, "丙"),
            (5000, 6000, "丁"),
        ],
        parts=2, target_ms=2500,
    )
    assert plan.groups == 2
    assert [(r.start_ms, r.end_ms, r.blocks) for r in plan.rows] == [
        (0, 4000, 3),
        (5000, 6000, 1),
    ]
    assert plan.total_cost == (4000 - 2500) ** 2 + (1000 - 2500) ** 2


def test_blocks_sorted_by_start_then_parse_order():
    # 输入乱序：按入点重排后分卷；同入点保持解析序位
    plan = _plan(
        [(4000, 5000, "后"), (0, 1000, "先"), (2000, 3000, "中")],
        parts=3, target_ms=1000,
    )
    assert [r.start_ms for r in plan.rows] == [0, 2000, 4000]
    assert [r.blocks for r in plan.rows] == [1, 1, 1]
    # ZIP 中第 1 卷正文应为「先」
    names, texts = _zip_contents(plan)
    assert names == ["volume_01.srt", "volume_02.srt", "volume_03.srt"]
    assert "先" in texts[0] and "中" in texts[1] and "后" in texts[2]


# ---------- 全局最优与平局 ----------

def test_global_optimum_not_greedy():
    # 四块各 1000ms 首尾相接，目标 2000：均分两卷总分 0
    plan = _plan(
        [(i * 1000, (i + 1) * 1000, f"块{i + 1}") for i in range(4)],
        parts=2, target_ms=2000,
    )
    assert [(r.start_ms, r.end_ms) for r in plan.rows] == [(0, 2000), (2000, 4000)]
    assert plan.total_cost == 0


def test_tie_break_lexicographically_smallest_next_volume_starts():
    # 三块各 1000ms、间隔 1000ms，目标 1500：
    # 切 1|23 与 12|3 总分相同，选下一卷入点序列 (2000,) 而非 (4000,)
    plan = _plan(
        [(0, 1000, "甲"), (2000, 3000, "乙"), (4000, 5000, "丙")],
        parts=2, target_ms=1500,
    )
    assert plan.total_cost == (1000 - 1500) ** 2 + (3000 - 1500) ** 2
    assert [(r.start_ms, r.end_ms, r.blocks) for r in plan.rows] == [
        (0, 1000, 1),
        (2000, 5000, 2),
    ]


def test_tie_break_multiple_cuts():
    # 五块各 1000ms、间隔 1000ms，K=3，目标 2000：
    # (1,2,2)/(2,1,2)/(2,2,1) 同分，选下一卷入点序列 (2000,6000) 最小者
    plan = _plan(
        [(i * 2000, i * 2000 + 1000, f"块{i + 1}") for i in range(5)],
        parts=3, target_ms=2000,
    )
    assert plan.total_cost == 3 * 1000 ** 2
    assert [(r.start_ms, r.end_ms, r.blocks) for r in plan.rows] == [
        (0, 1000, 1),
        (2000, 5000, 2),
        (6000, 9000, 2),
    ]


def test_min_max_duration_bounds_respected():
    # 上限 2500：均分（各 2000）可行；若每卷须恰为 1500 则无可行切分
    spans = [(i * 1000, (i + 1) * 1000, f"块{i + 1}") for i in range(4)]
    plan = _plan(spans, parts=2, target_ms=2000, max_ms=2500)
    assert plan.total_cost == 0
    with pytest.raises(ValueError, match="无解"):
        _plan(spans, parts=2, min_ms=1500, target_ms=1500, max_ms=1500)


def test_single_volume_must_fit_range():
    plan = _plan([(0, 1000, "甲"), (2000, 3000, "乙")], parts=1,
                 target_ms=3000, max_ms=5000)
    assert len(plan.rows) == 1
    assert plan.rows[0].duration_ms == 3000
    with pytest.raises(ValueError, match="无解"):
        _plan([(0, 1000, "甲"), (2000, 3000, "乙")], parts=1,
              target_ms=1000, max_ms=1000)


# ---------- 报错 ----------

def test_parts_exceed_groups_raises():
    with pytest.raises(ValueError, match="无重叠组"):
        _plan([(0, 1000, "甲"), (1000, 2000, "乙")], parts=3)


def test_zero_length_block_raises_and_points_position():
    with pytest.raises(ValueError, match="第 2 块"):
        _plan([(0, 1000, "甲"), (2000, 2000, "乙"), (3000, 4000, "丙")])


def test_negative_duration_block_raises_with_declared_index():
    with pytest.raises(ValueError, match="第 1 块（声明序号 9）"):
        _plan([(3000, 1000, "倒着走")], declared=[9])


def test_invalid_duration_params_raise():
    with pytest.raises(ValueError, match="最短 ≤ 目标 ≤ 最长"):
        _plan([(0, 1000, "甲")], parts=1, min_ms=2000, target_ms=1000,
              max_ms=3000)
    with pytest.raises(ValueError, match="正整数"):
        _plan([(0, 1000, "甲")], parts=1, min_ms=0, target_ms=1000,
              max_ms=3000)


# ---------- 导出 ----------

def test_build_volume_srt_shifts_and_renumbers():
    items = parse_srt(_srt([(5000, 6000, "第一行\n第二行"), (6500, 7000, "后")]))
    text = build_volume_srt(tuple(items), 5000)
    assert text == (
        "1\n00:00:00,000 --> 00:00:01,000\n第一行\n第二行\n\n"
        "2\n00:00:01,500 --> 00:00:02,000\n后\n"
    )


def _zip_contents(plan):
    raw = build_volume_zip(plan)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        texts = [zf.read(n).decode("utf-8") for n in names]
    return names, texts


def test_zip_one_srt_per_volume_shifted_from_one():
    plan = _plan(
        [(1000, 4000, "前段"), (20000, 23000, "后段")],
        parts=2, target_ms=3000,
    )
    names, texts = _zip_contents(plan)
    assert names == ["volume_01.srt", "volume_02.srt"]
    # 各卷首块从 00:00:00,000 开始、从 1 编号
    assert texts[0].startswith("1\n00:00:00,000 --> 00:00:03,000\n前段")
    assert texts[1].startswith("1\n00:00:00,000 --> 00:00:03,000\n后段")


# ---------- 接口 ----------

SRT = _srt(
    [
        (1000, 4000, "前段第一句"),
        (4500, 7500, "前段第二句"),
        (8000, 11000, "前段第三句"),
        (20000, 23000, "后段第一句"),
        (23500, 26500, "后段第二句"),
        (27000, 30000, "后段第三句"),
    ]
)

REQ = {"srt": SRT, "parts": 2, "min_ms": 1000, "target_ms": 10000,
       "max_ms": 60000}


def test_api_volume_preview():
    resp = client.post("/api/volume", json=REQ)
    assert resp.status_code == 200
    data = resp.json()
    # 六块互不重叠、各自成组；切分点落在第 3、4 组之间
    assert data["summary"] == {
        "volumes": 2,
        "blocks": 6,
        "groups": 6,
        "parts": 2,
        "min_ms": 1000,
        "target_ms": 10000,
        "max_ms": 60000,
        "total_cost": 0,
    }
    assert data["rows"] == [
        {
            "volume": 1,
            "start_tc": "00:00:01,000",
            "end_tc": "00:00:11,000",
            "start_ms": 1000,
            "end_ms": 11000,
            "blocks": 3,
            "duration_ms": 10000,
            "deviation_ms": 0,
            "cost": 0,
        },
        {
            "volume": 2,
            "start_tc": "00:00:20,000",
            "end_tc": "00:00:30,000",
            "start_ms": 20000,
            "end_ms": 30000,
            "blocks": 3,
            "duration_ms": 10000,
            "deviation_ms": 0,
            "cost": 0,
        },
    ]


def test_api_volume_zip_download():
    resp = client.post("/api/volume.zip", json=REQ)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="volumes.zip"' in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["volume_01.srt", "volume_02.srt"]
        first = zf.read("volume_01.srt").decode("utf-8")
        second = zf.read("volume_02.srt").decode("utf-8")
    # 各卷时间减去该卷最早入点、保留正文、从 1 编号
    assert first.startswith("1\n00:00:00,000 --> 00:00:03,000\n前段第一句")
    assert "3\n00:00:07,000 --> 00:00:10,000\n前段第三句" in first
    assert second.startswith("1\n00:00:00,000 --> 00:00:03,000\n后段第一句")
    assert "3\n00:00:07,000 --> 00:00:10,000\n后段第三句" in second


def test_api_volume_no_solution_400():
    resp = client.post(
        "/api/volume",
        json={"srt": SRT, "parts": 2, "min_ms": 1, "target_ms": 1000,
              "max_ms": 1000},
    )
    assert resp.status_code == 400
    assert "无解" in resp.json()["detail"]


def test_api_volume_zip_rejects_no_solution():
    resp = client.post(
        "/api/volume.zip",
        json={"srt": SRT, "parts": 2, "min_ms": 1, "target_ms": 1000,
              "max_ms": 1000},
    )
    assert resp.status_code == 400


def test_api_volume_zero_length_block_400_points_block():
    bad_srt = _srt([(0, 1000, "正常块"), (2000, 2000, "零时长")])
    resp = client.post(
        "/api/volume",
        json={"srt": bad_srt, "parts": 1, "min_ms": 1, "target_ms": 1000,
              "max_ms": 5000},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "第 2 块" in detail and "入点不早于出点" in detail


def test_api_volume_empty_and_bad_srt_400():
    resp = client.post(
        "/api/volume",
        json={"srt": "", "parts": 2, "min_ms": 1, "target_ms": 10,
              "max_ms": 100},
    )
    assert resp.status_code == 400
    resp = client.post(
        "/api/volume",
        json={"srt": "没有时间码", "parts": 2, "min_ms": 1, "target_ms": 10,
              "max_ms": 100},
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]


def test_api_volume_too_many_blocks_400():
    many = _srt([(i * 1000, i * 1000 + 500, f"块{i + 1}") for i in range(201)])
    resp = client.post(
        "/api/volume",
        json={"srt": many, "parts": 2, "min_ms": 1, "target_ms": 1000,
              "max_ms": 10**7},
    )
    assert resp.status_code == 400
    assert "200" in resp.json()["detail"]


def test_api_volume_param_validation():
    base = {"srt": SRT, "parts": 2, "min_ms": 1000, "target_ms": 10000,
            "max_ms": 60000}
    # 份数越界
    for parts in (0, 21):
        resp = client.post("/api/volume", json={**base, "parts": parts})
        assert resp.status_code in (400, 422)
    # 非正时长
    resp = client.post("/api/volume", json={**base, "min_ms": 0})
    assert resp.status_code in (400, 422)
    # 最短 > 目标
    resp = client.post(
        "/api/volume", json={**base, "min_ms": 20000, "target_ms": 10000}
    )
    assert resp.status_code == 400
    assert "最短 ≤ 目标 ≤ 最长" in resp.json()["detail"]
    # 目标 > 最长
    resp = client.post(
        "/api/volume", json={**base, "target_ms": 70000, "max_ms": 60000}
    )
    assert resp.status_code == 400
    # 非整数
    resp = client.post("/api/volume", json={**base, "parts": 2.5})
    assert resp.status_code == 422


def test_api_volume_parts_boundaries_accepted():
    # K=1：整段一卷；K=20 超过组数时报无解而非参数错误
    resp = client.post(
        "/api/volume",
        json={"srt": SRT, "parts": 1, "min_ms": 1000, "target_ms": 29000,
              "max_ms": 60000},
    )
    assert resp.status_code == 200
    assert resp.json()["summary"]["volumes"] == 1
    resp = client.post(
        "/api/volume",
        json={"srt": SRT, "parts": 20, "min_ms": 1, "target_ms": 1000,
              "max_ms": 60000},
    )
    assert resp.status_code == 400
    assert "无重叠组" in resp.json()["detail"]
