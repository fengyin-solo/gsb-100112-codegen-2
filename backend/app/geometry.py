"""线段裁剪台的几何与桩号工具。

底图以"路线 + 里程桩号"为一维骨架（每条路线另带一份屏幕折线坐标用于画图），
所有裁剪、冲突判定都在同一路线的里程区间上完成，避免引入任何第三方 GIS 依赖。

区间一律采用左闭右开 [start_m, end_m)，端点相接（如 K1+500 接 K1+500）不算冲突，
以此落实"既有边界保留原线"——边界贴着原线时不产生待裁切块。
"""
from __future__ import annotations

import re
from typing import Any

_STAKE_FULL = re.compile(r"^K?\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_KM = re.compile(r"^([\d.]+)\s*km$", re.IGNORECASE)
_METER = re.compile(r"^([\d.]+)\s*m?$", re.IGNORECASE)


class StakeError(ValueError):
    """桩号无法解析或明显非法。"""


def parse_stake(value: Any) -> int:
    """把 K2+500 / 2+500 / 2500 / 2.5km 统一解析成米（整数）。

    正式桩号是后续所有裁定的唯一依据，解析不了必须显式报错，绝不允许静默成 0。
    """
    if value is None:
        raise StakeError("桩号为空")
    text = str(value).strip().replace("Ｋ", "K").replace("＋", "+")
    if not text:
        raise StakeError("桩号为空")
    match = _STAKE_FULL.match(text)
    if match:
        kilometers, meters = match.groups()
        result = int(kilometers) * 1000 + float(meters)
    else:
        match = _KM.match(text)
        if match:
            result = float(match.group(1)) * 1000
        else:
            match = _METER.match(text)
            if not match:
                raise StakeError(f"桩号「{text}」无法解析，支持 K2+500、2500、2.5km 三种写法")
            result = float(match.group(1))
    meters = int(round(result))
    if meters < 0:
        raise StakeError(f"桩号「{text}」为负里程，不允许进入底图")
    return meters


def format_stake(meters: int) -> str:
    """米回填成正式桩号 Kx+xxx，台账与地图标签共用同一口径。"""
    meters = int(round(meters))
    return f"K{meters // 1000}+{meters % 1000:03d}"


def split_by_published(
    start_m: int,
    end_m: int,
    occupied: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用已发布正式路段裁剪候选线段。

    返回有序的切块列表：落在既有路段之外的是 passable（可通行线段），
    与既有路段重叠的是 conflict（待裁切块），并记下与哪些既有路段重叠。
    端点相接不重叠，既有边界原样保留。
    """
    cuts: list[tuple[int, int, str, list[int]]] = []
    cursor = start_m
    overlaps = sorted(
        (seg for seg in occupied if seg["end_m"] > start_m and seg["start_m"] < end_m),
        key=lambda seg: (seg["start_m"], seg["end_m"]),
    )
    for seg in overlaps:
        seg_start, seg_end = seg["start_m"], seg["end_m"]
        if seg_start > cursor:
            cuts.append((cursor, seg_start, "passable", []))
        overlap_start = max(cursor, seg_start)
        overlap_end = max(overlap_start, seg_end)
        if cuts and cuts[-1][2] == "conflict" and cuts[-1][1] == overlap_start:
            prev_start, _, _, prev_ids = cuts[-1]
            cuts[-1] = (prev_start, overlap_end, "conflict", prev_ids + [seg["id"]])
        elif overlap_end > overlap_start:
            cuts.append((overlap_start, overlap_end, "conflict", [seg["id"]]))
        cursor = max(cursor, overlap_end)
    if cursor < end_m:
        cuts.append((cursor, end_m, "passable", []))
    return [
        {
            "start_m": a,
            "end_m": b,
            "kind": kind,
            "overlap_with": refs,
            "stake_start": format_stake(a),
            "stake_end": format_stake(b),
        }
        for a, b, kind, refs in cuts
    ]


def difference_segment(
    segment: dict[str, Any], start_m: int, end_m: int
) -> list[tuple[int, int]]:
    """既有路段被正式桩号覆盖时，把被覆盖的里程挖掉，剩余里程保持原线。"""
    remain: list[tuple[int, int]] = []
    if start_m > segment["start_m"]:
        remain.append((segment["start_m"], min(start_m, segment["end_m"])))
    if end_m < segment["end_m"]:
        remain.append((max(end_m, segment["start_m"]), segment["end_m"]))
    return [(a, b) for a, b in remain if b > a]


def point_at_m(route: dict[str, Any], meters: int) -> tuple[float, float]:
    """按里程比例在路线折线上取点，用于地图绘制与切块定位。"""
    coords = route["coords"]
    if len(coords) == 1:
        return float(coords[0][0]), float(coords[0][1])
    ratio = min(max(meters / max(route["length_m"], 1), 0.0), 1.0)
    target = ratio * (len(coords) - 1)
    index = int(target)
    if index >= len(coords) - 1:
        return float(coords[-1][0]), float(coords[-1][1])
    fraction = target - index
    x1, y1 = coords[index]
    x2, y2 = coords[index + 1]
    return x1 + (x2 - x1) * fraction, y1 + (y2 - y1) * fraction


def sub_polyline(
    route: dict[str, Any], start_m: int, end_m: int
) -> list[tuple[float, float]]:
    """裁出路线在 [start_m, end_m] 上的子折线，几何沿原路线插值（保留原线）。"""
    span = max(route["length_m"], 1)
    points = [point_at_m(route, start_m)]
    for index, (x, y) in enumerate(route["coords"]):
        vertex_m = round(span * index / (len(route["coords"]) - 1)) if index else 0
        if start_m < vertex_m < end_m:
            points.append((float(x), float(y)))
    points.append(point_at_m(route, end_m))
    return points


def polyline_points(points: list[tuple[float, float]]) -> str:
    """折线点转 SVG points 字符串。"""
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def covers(occupied: list[tuple[int, int]], start_m: int, end_m: int) -> bool:
    """判断里程区间是否被一组正式区间完整覆盖（工程边界引用完整性用）。"""
    cursor = start_m
    for seg_start, seg_end in sorted(occupied):
        if seg_start > cursor:
            return False
        cursor = max(cursor, seg_end)
        if cursor >= end_m:
            return True
    return cursor >= end_m


def first_gap(
    occupied: list[tuple[int, int]], start_m: int, end_m: int
) -> tuple[int, int] | None:
    """找出区间内第一段未被正式路网覆盖的里程，用于说明悬空原因。"""
    cursor = start_m
    for seg_start, seg_end in sorted(occupied):
        if seg_start > cursor:
            return cursor, min(seg_start, end_m)
        cursor = max(cursor, seg_end)
        if cursor >= end_m:
            return None
    return cursor, end_m if cursor < end_m else None
