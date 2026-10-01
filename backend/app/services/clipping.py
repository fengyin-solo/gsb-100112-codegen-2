"""线段裁剪台：管养路段底图的唯一入口。

导入与发布严格按四阶段推进，不能跳级：

    解析(import) → 分段核对(check) → 冲突裁定(decision) → 正式发布(publish)

发布时路段台账、巡查待办、工程边界围绕同一个发布版本原子重算，
任何悬空引用（工程边界落不到正式路网上）都不允许进入正式层。
发布中断只留下裁剪草稿，恢复后仅补未发布线段；重复上传幂等，不产生重叠路段。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app import geometry
from app.geometry import format_stake, parse_stake
from app.store import store


class ClippingError(Exception):
    """流程被规则拦下；status 给出建议的 HTTP 状态码。"""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


# 底图初始骨架：正式桩号是里程，coords 仅用于地图绘制。
INITIAL_ROUTES: list[dict[str, Any]] = [
    {
        "code": "G104",
        "name": "国道主干线",
        "length_m": 3000,
        "coords": [[60, 210], [160, 180], [270, 195], [380, 160], [490, 175], [600, 150], [720, 165]],
    },
    {
        "code": "S205",
        "name": "省道东接线",
        "length_m": 2200,
        "coords": [[90, 330], [200, 300], [320, 320], [440, 290], [570, 310], [700, 290]],
    },
]

INITIAL_SEGMENTS = [
    dict(code="G104", start_m=0, end_m=1200, name="城北快速通道", grade="主干路",
         lanes="6", surface="沥青混凝土", org="市政一处"),
    dict(code="G104", start_m=1200, end_m=2500, name="北二环连接线", grade="主干路",
         lanes="4", surface="沥青混凝土", org="市政二处"),
    dict(code="S205", start_m=0, end_m=1200, name="东郊路", grade="次干路",
         lanes="4", surface="水泥混凝土", org="市政三处"),
]

BASELINE_VERSION = "v0-基线"

# 演示用工程清单：前两个引用 G104、第三个引用 S205；发布前 2/3 悬空，发布后全部落图。
DEMO_PROJECTS = [
    dict(工程编号="PROJ-0001", 工程名称="北干线路面中修", 工程类型="路面中修",
         施工路段="G104 K0+500~K2+000", route_code="G104", start_m=500, end_m=2000,
         承建单位="通达养护公司", 开工日期="2026-09-10", 竣工日期="2026-11-30",
         status="施工中", pending=True),
    dict(工程编号="PROJ-0002", 工程名称="北延交安补建", 工程类型="交安工程",
         施工路段="G104 K2+600~K2+900", route_code="G104", start_m=2600, end_m=2900,
         承建单位="交建集团", 开工日期="2026-10-20", 竣工日期="2026-12-20",
         status="待开工", pending=True),
    dict(工程编号="PROJ-0003", 工程名称="东延排水改造", 工程类型="排水工程",
         施工路段="S205 K1+500~K2+000", route_code="S205", start_m=1500, end_m=2000,
         承建单位="水务工程处", 开工日期="2026-10-15", 竣工日期="2026-12-05",
         status="待开工", pending=True),
]

STAGES = ["解析", "分段核对", "冲突裁定", "正式发布"]


def _stamp() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


def _version_label() -> str:
    return f"v{len(_state['versions']) + 1}-{datetime.now().strftime('%Y%m%d%H%M')}"


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
_state: dict[str, Any] = {
    "routes": {},
    "network": [],
    "current_version": BASELINE_VERSION,
    "versions": [],
    "draft": None,
    "ingested": {},
    "derived": {"ledger": [], "patrol_todos": [], "project_bounds": []},
    "_seq": {"segment": 0, "batch": 0, "candidate": 0, "piece": 0},
}
_booted = False


def reset_state() -> None:
    """清空全部裁剪台状态并重新播种，供测试隔离使用。"""
    global _booted
    _state.update(
        routes={},
        network=[],
        current_version=BASELINE_VERSION,
        versions=[],
        draft=None,
        ingested={},
        derived={"ledger": [], "patrol_todos": [], "project_bounds": []},
        _seq={"segment": 0, "batch": 0, "candidate": 0, "piece": 0},
    )
    _booted = False
    bootstrap()


def _next_id(kind: str) -> int:
    _state["_seq"][kind] += 1
    return _state["_seq"][kind]


def bootstrap() -> None:
    """播种底图骨架、基线正式路段，并让台账/待办/工程边界围绕基线重算一次。"""
    global _booted
    if _booted:
        return
    _booted = True
    _state["routes"] = {route["code"]: dict(route) for route in INITIAL_ROUTES}
    for item in INITIAL_SEGMENTS:
        segment = dict(item)
        segment["id"] = _next_id("segment")
        segment["segment_code"] = f"{item['code']}-S{segment['id']:03d}"
        segment["status"] = "正常"
        segment["version"] = BASELINE_VERSION
        _state["network"].append(segment)
    # 工程清单换成带正式桩号引用的演示数据，供边界完整性重算。
    project_rows = store.rows("project")
    project_rows.clear()
    for index, item in enumerate(DEMO_PROJECTS, start=1):
        row = {"id": index, "abnormal": False}
        row.update(item)
        project_rows.append(row)
    _recompute_derived(BASELINE_VERSION)


# ---------------------------------------------------------------------------
# 派生重算：台账、巡查待办、工程边界同一版本
# ---------------------------------------------------------------------------
def _active_network() -> list[dict[str, Any]]:
    return sorted(_state["network"], key=lambda seg: (seg["code"], seg["start_m"]))


def _recompute_derived(version: str) -> dict[str, Any]:
    """围绕指定发布版本，重算路段台账、巡查待办、工程边界。

    三层一起算、一起落，保证不会出现"底图更新了、台账还是旧版本"的情况。
    """
    network = _active_network()

    ledger: list[dict[str, Any]] = []
    todos: list[dict[str, Any]] = []
    for index, seg in enumerate(network, start=1):
        stake_text = f"{seg['code']} {format_stake(seg['start_m'])}~{format_stake(seg['end_m'])}"
        ledger.append({
            "id": seg["id"],
            "status": seg["status"],
            "pending": False,
            "abnormal": False,
            "路段编号": seg["segment_code"],
            "路段名称": seg["name"],
            "路线": seg["code"],
            "起止桩号": stake_text,
            "道路等级": seg["grade"],
            "车道数": seg["lanes"],
            "路面类型": seg["surface"],
            "管养单位": seg["org"],
            "路段状态": seg["status"],
            "发布版本": seg["version"],
        })
        todos.append({
            "id": index,
            "status": "待巡查",
            "pending": True,
            "abnormal": False,
            "巡查编号": f"PATR-{seg['id']:04d}",
            "巡查路段": f"{seg['name']}（{stake_text}）",
            "巡查日期": f"2026-10-{(index % 7) + 1:02d}",
            "巡查人员": seg["org"],
            "巡查车辆": f"巡查{index:02d}号车",
            "发现问题": "—",
            "处置措施": "—",
            "巡查状态": "待巡查",
            "引用状态": "正式层内",
            "发布版本": version,
        })

    bounds: list[dict[str, Any]] = []
    for project in store.rows("project"):
        route_code = project.get("route_code")
        start_m, end_m = project.get("start_m"), project.get("end_m")
        route = _state["routes"].get(route_code or "")
        occupied = [
            (seg["start_m"], seg["end_m"])
            for seg in network
            if route is not None and seg["code"] == route_code
        ]
        bound = bool(route and start_m is not None and geometry.covers(occupied, start_m, end_m))
        missing_text = ""
        if not bound:
            if route is None:
                missing_text = f"路线「{route_code}」不在底图上"
            else:
                gap = geometry.first_gap(occupied, start_m, end_m)
                missing_text = (
                    f"{format_stake(gap[0])}~{format_stake(gap[1])} 不在正式路网"
                    if gap else "边界不完整"
                )
        project["abnormal"] = not bound
        project["引用状态"] = "正式层内" if bound else "悬空引用"
        project["发布版本"] = version if bound else "未入正式层"
        bounds.append({
            "工程编号": project["工程编号"],
            "工程名称": project["工程名称"],
            "施工路段": project["施工路段"],
            "引用状态": "正式层内" if bound else "悬空引用",
            "缺失区间": missing_text,
            "发布版本": version if bound else "未入正式层",
        })

    # 正式层只接收引用完整的数据：台账/待办整体替换，悬空工程不进边界正式层。
    store.rows("road_section").clear()
    store.rows("road_section").extend(dict(row) for row in ledger)
    store.rows("patrol").clear()
    store.rows("patrol").extend(dict(row) for row in todos)

    _state["derived"] = {
        "ledger": ledger,
        "patrol_todos": todos,
        "project_bounds": [row for row in bounds if row["引用状态"] == "正式层内"],
        "project_refs": bounds,
    }
    return _state["derived"]


# ---------------------------------------------------------------------------
# 阶段一：解析（导入即裁剪）
# ---------------------------------------------------------------------------
def _candidate_hash(route_code: str, start_m: int, end_m: int) -> str:
    raw = f"{route_code}|{start_m}|{end_m}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _parse_import_lines(content: str) -> list[dict[str, Any]]:
    """支持整段 JSON（对象/数组）、JSON Lines 与带表头的 CSV，统一转成字段字典。"""
    text = (content or "").strip()
    if not text:
        raise ClippingError("导入文件为空，无法解析", status=400)
    if text[0] in "[{":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClippingError(f"JSON 解析失败：{exc.msg}", status=400) from exc
        if isinstance(payload, dict):
            payload = payload.get("lines") or payload.get("features") or []
        if not isinstance(payload, list):
            raise ClippingError("JSON 顶层需是线段数组或 {\"lines\": [...]}", status=400)
        return [dict(item) for item in payload]

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ClippingError("CSV 至少需要表头和一行数据", status=400)
    headers = [part.strip() for part in lines[0].split(",")]
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        values = [part.strip() for part in line.split(",")]
        rows.append(dict(zip(headers, values)))
    return rows


def import_file(filename: str, content: str) -> dict[str, Any]:
    """解析导入文件，并立刻按已发布底图裁出可通行线段/待裁切块。"""
    bootstrap()
    if _state["draft"] is not None:
        raise ClippingError("存在未完成的裁剪草稿，请先完成发布、恢复续发或废弃后再导入")

    raw_rows = _parse_import_lines(content)
    batch = {
        "id": _next_id("batch"),
        "filename": filename or "导入线段",
        "created_at": _stamp(),
        "status": "draft",
        "stage": "解析",
        "interrupted": False,
        "version": None,
        "candidates": [],
        "pieces": [],
    }
    parse_errors: list[str] = []
    for offset, raw in enumerate(raw_rows, start=1):
        route_ref = str(raw.get("路线") or raw.get("route") or "").strip()
        route = _state["routes"].get(route_ref)
        if route is None:
            route = next((item for item in _state["routes"].values() if item["name"] == route_ref), None)
        candidate: dict[str, Any] = {
            "id": _next_id("candidate"),
            "raw_line": offset,
            "raw_text": raw.get("历史区划") or raw.get("路段名称") or f"第{offset}行",
            "route_code": route["code"] if route else route_ref,
            "route_name": route["name"] if route else route_ref,
            "name": str(raw.get("路段名称") or raw.get("name") or "").strip(),
            "grade": str(raw.get("道路等级") or "未登记").strip(),
            "lanes": str(raw.get("车道数") or "—").strip(),
            "surface": str(raw.get("路面类型") or "—").strip(),
            "org": str(raw.get("管养单位") or "—").strip(),
            "historical_name": str(raw.get("历史区划") or "").strip(),
            "start_m": None,
            "end_m": None,
            "errors": [],
            "duplicate_of": None,
        }
        try:
            if route is None:
                raise geometry.StakeError(f"路线「{route_ref}」不在底图上")
            start_m = parse_stake(raw.get("起点桩号") or raw.get("start"))
            end_m = parse_stake(raw.get("终点桩号") or raw.get("end"))
            if end_m <= start_m:
                raise geometry.StakeError("终点桩号必须大于起点桩号")
            if end_m > route["length_m"]:
                raise geometry.StakeError(
                    f"{format_stake(end_m)} 超出路线全长 {format_stake(route['length_m'])}"
                )
            candidate["start_m"], candidate["end_m"] = start_m, end_m

            digest = _candidate_hash(route["code"], start_m, end_m)
            candidate["digest"] = digest
            exact = next((
                seg for seg in _state["network"]
                if seg["code"] == route["code"] and seg["start_m"] == start_m and seg["end_m"] == end_m
            ), None)
            in_draft = next((
                item for item in batch["candidates"]
                if item.get("digest") == digest and not item["errors"]
            ), None)
            ingested_seg = _state["ingested"].get(digest)
            if exact is not None:
                # 重复上传幂等：完全重合直接认作已发布，不生成任何切块。
                candidate["duplicate_of"] = exact["segment_code"]
            elif ingested_seg is not None:
                # 同一批线段已在历史批次处理过（含保留原线），重传不重新生成重叠/冲突。
                candidate["duplicate_of"] = f"{ingested_seg['segment_code']}（{ingested_seg['version']} 已处理）"
            elif in_draft is not None:
                candidate["errors"].append(
                    f"与本文件第{in_draft['raw_line']}行完全重复，重复上传不生成重叠路段"
                )
            else:
                pieces = geometry.split_by_published(
                    start_m, end_m,
                    [seg for seg in _state["network"] if seg["code"] == route["code"]],
                )
                for piece in pieces:
                    piece.update({
                        "id": _next_id("piece"),
                        "candidate_id": candidate["id"],
                        "route_code": route["code"],
                        "checked": False,
                        "decision": None,
                        "decided_at": None,
                        "published": False,
                        "new_seg_id": None,
                        "rollback": None,
                    })
                    batch["pieces"].append(piece)
        except geometry.StakeError as exc:
            candidate["errors"].append(str(exc))
            parse_errors.append(f"第{offset}行：{exc}")
        batch["candidates"].append(candidate)

    valid = [item for item in batch["candidates"] if not item["errors"] and item["duplicate_of"] is None]
    if not valid:
        detail = "；".join(parse_errors) or "全部线段均与已发布路段重复"
        raise ClippingError(f"没有可裁剪的有效线段：{detail}", status=400)

    batch["stage"] = "分段核对"
    _state["draft"] = batch
    return {"ok": True, "batch": serialize_draft(), "parse_errors": parse_errors}


def correct_candidate(candidate_id: int, start_stake: str, end_stake: str) -> dict[str, Any]:
    """分段核对时用正式桩号纠正候选线段，并按当前正式路网重新裁一遍。"""
    batch = _require_draft()
    if batch["stage"] != "分段核对":
        raise ClippingError("只有「分段核对」阶段可以纠正桩号")
    candidate = _find_candidate(candidate_id)
    route = _state["routes"][candidate["route_code"]]
    start_m, end_m = parse_stake(start_stake), parse_stake(end_stake)
    if end_m <= start_m:
        raise geometry.StakeError("终点桩号必须大于起点桩号")
    if end_m > route["length_m"]:
        raise geometry.StakeError(f"终点超出路线全长 {format_stake(route['length_m'])}")

    batch["pieces"] = [p for p in batch["pieces"] if p["candidate_id"] != candidate_id]
    pieces = geometry.split_by_published(
        start_m, end_m, [seg for seg in _state["network"] if seg["code"] == route["code"]]
    )
    for piece in pieces:
        piece.update({
            "id": _next_id("piece"),
            "candidate_id": candidate_id,
            "route_code": route["code"],
            "checked": False,
            "decision": None,
            "decided_at": None,
            "published": False,
            "new_seg_id": None,
            "rollback": None,
        })
        batch["pieces"].append(piece)
    candidate["start_m"], candidate["end_m"] = start_m, end_m
    return serialize_draft()


# ---------------------------------------------------------------------------
# 阶段二：分段核对（逐段确认，未核对不允许往后走）
# ---------------------------------------------------------------------------
def check_piece(piece_id: int, checked: bool) -> dict[str, Any]:
    batch = _require_draft()
    if batch["stage"] not in ("分段核对", "冲突裁定", "正式发布"):
        raise ClippingError("当前不在「分段核对」阶段，不能补核对")
    piece = _find_piece(piece_id)
    if piece.get("decision") is not None and checked is False:
        raise ClippingError("该切块已完成冲突裁定，不能撤核对")
    piece["checked"] = bool(checked)
    _advance_after_check(batch)
    return serialize_draft()


def check_all() -> dict[str, Any]:
    batch = _require_draft()
    if batch["stage"] not in ("分段核对", "冲突裁定"):
        raise ClippingError("已离开分段核对阶段")
    for piece in batch["pieces"]:
        piece["checked"] = True
    _advance_after_check(batch)
    return serialize_draft()


def _unchecked_pieces(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in batch["pieces"] if not p["checked"]]


def _unchecked_passables(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in batch["pieces"] if p["kind"] == "passable" and not p["checked"]]


def _open_conflicts(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in batch["pieces"] if p["kind"] == "conflict" and p["decision"] is None]


def _advance_after_check(batch: dict[str, Any]) -> None:
    """核对完整段才允许进入裁定；无冲突时直达待发布。"""
    if _unchecked_passables(batch):
        batch["stage"] = "分段核对"
    elif _open_conflicts(batch):
        batch["stage"] = "冲突裁定"
    else:
        batch["stage"] = "正式发布"


# ---------------------------------------------------------------------------
# 阶段三：冲突裁定（正式桩号优先；既有边界保留原线）
# ---------------------------------------------------------------------------
def decide_conflict(piece_id: int, decision: str) -> dict[str, Any]:
    batch = _require_draft()
    piece = _find_piece(piece_id)
    if piece["kind"] != "conflict":
        raise ClippingError("该切块不是冲突区")
    # 将被正式桩号合并的同候选相邻可通行段，也必须先完成分段核对，不能借裁定跳级。
    pending_members = [piece]
    if decision == "use_formal":
        ordered = _ordered_pieces(batch)
        index = ordered.index(piece)
        while index + 1 < len(ordered):
            nxt = ordered[index + 1]
            if (
                nxt["kind"] == "passable"
                and nxt["candidate_id"] == piece["candidate_id"]
                and nxt["start_m"] == pending_members[-1]["end_m"]
            ):
                pending_members.append(nxt)
                index += 1
            else:
                break
    unchecked = [p for p in pending_members if not p["checked"]]
    if unchecked:
        labels = "、".join(f"{p['stake_start']}~{p['stake_end']}" for p in unchecked)
        raise ClippingError(f"线段 {labels} 尚未完成分段核对，不能提前裁定/裁入")
    if batch["stage"] not in ("冲突裁定", "正式发布"):
        raise ClippingError("当前没有待裁定的冲突切块")
    if piece["published"]:
        raise ClippingError("该切块已经随上次中断发布进正式层，恢复时只会跳过")
    if decision not in ("keep_existing", "use_formal"):
        raise ClippingError("裁定结论只支持 keep_existing（保留原线）或 use_formal（按正式桩号裁入）")
    piece["decision"] = decision
    piece["decided_at"] = _stamp()
    # 正式桩号优先：裁定时只记录正式里程，候选里的历史区划名称不参与落图。
    piece["resolved_by"] = "正式桩号"
    if not _open_conflicts(batch):
        batch["stage"] = "正式发布"
    return serialize_draft()


# ---------------------------------------------------------------------------
# 阶段四：正式发布（原子版本；中断留草稿；恢复只补未发布线段）
# ---------------------------------------------------------------------------
def _ordered_pieces(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(batch["pieces"], key=lambda p: (p["candidate_id"], p["start_m"], p["id"]))


def _publish_units(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """把切块整理成发布单元。

    use_formal 冲突块与同一候选内紧跟其后的可通行段合并发布：正式桩号是一条完整新线，
    不能只替换重叠段、把后面的缺口另起一段（那样会留下与原线重复的断头）。
    keep_existing 时冲突块让位给既有路段，可通行段各自独立发布。
    """
    units: list[dict[str, Any]] = []
    absorbed: set[int] = set()
    for piece in _ordered_pieces(batch):
        if piece["published"] or piece["id"] in absorbed:
            continue
        if piece["kind"] == "conflict":
            if piece.get("decision") != "use_formal":
                continue
            members = [piece]
            ordered = _ordered_pieces(batch)
            index = ordered.index(piece)
            # 只吸收同一候选里里程严格紧邻的后续可通行段；遇到保留段/其他冲突即停。
            while index + 1 < len(ordered):
                nxt = ordered[index + 1]
                if (
                    nxt["kind"] == "passable"
                    and not nxt["published"]
                    and nxt["candidate_id"] == piece["candidate_id"]
                    and nxt["start_m"] == members[-1]["end_m"]
                ):
                    members.append(nxt)
                    absorbed.add(nxt["id"])
                    index += 1
                else:
                    break
            units.append({"start_m": members[0]["start_m"], "end_m": members[-1]["end_m"],
                          "members": members})
        else:
            units.append({"start_m": piece["start_m"], "end_m": piece["end_m"],
                          "members": [piece]})
    return units


def _apply_unit(batch: dict[str, Any], unit: dict[str, Any], version: str) -> None:
    """把单个发布单元落入正式路网，并记下回滚凭据（供废弃草稿时恢复原线）。"""
    pieces = unit["members"]
    primary = pieces[0]
    candidate = next(item for item in batch["candidates"] if item["id"] == primary["candidate_id"])
    start_m, end_m = unit["start_m"], unit["end_m"]
    removed: list[dict[str, Any]] = []
    remainder_ids: list[int] = []

    if primary["kind"] == "conflict":
        # 按正式桩号裁入：先从既有路段挖出被覆盖里程，剩余部分保留原线。
        ref_ids = {ref for piece in pieces for ref in piece["overlap_with"]}
        refs = [seg for seg in _state["network"] if seg["id"] in ref_ids]
        for seg in refs:
            removed.append(dict(seg))
            _state["network"].remove(seg)
            for a, b in geometry.difference_segment(seg, start_m, end_m):
                rest = dict(seg)
                rest["id"] = _next_id("segment")
                rest["segment_code"] = f"{seg['code']}-S{rest['id']:03d}"
                rest["start_m"], rest["end_m"] = a, b
                rest["version"] = version
                _state["network"].append(rest)
                remainder_ids.append(rest["id"])

    segment = {
        "id": _next_id("segment"),
        "code": primary["route_code"],
        "segment_code": "",
        "start_m": start_m,
        "end_m": end_m,
        "name": candidate["name"] or candidate["route_name"],
        "grade": candidate["grade"],
        "lanes": candidate["lanes"],
        "surface": candidate["surface"],
        "org": candidate["org"],
        "status": "正常",
        "version": version,
    }
    segment["segment_code"] = f"{primary['route_code']}-S{segment['id']:03d}"
    _state["network"].append(segment)

    for piece in pieces:
        piece["published"] = True
        piece["new_seg_id"] = segment["id"]
        piece["rollback"] = {
            "new_seg_id": segment["id"],
            "removed": removed,
            "remainder_ids": remainder_ids,
        }


def publish(*, fail_after: int | None = None) -> dict[str, Any]:
    """正式发布。fail_after 仅用于演示发布中断：处理 N 段后挂起并留下草稿。"""
    batch = _require_draft()
    if batch["status"] != "draft":
        raise ClippingError("该草稿已经处理完毕")
    unchecked = _unchecked_pieces(batch)
    if unchecked:
        labels = "、".join(f"{p['stake_start']}~{p['stake_end']}" for p in unchecked)
        raise ClippingError(f"线段 {labels} 尚未完成分段核对，不允许跳级发布", status=403)
    open_conflicts = _open_conflicts(batch)
    if open_conflicts:
        raise ClippingError(
            f"还有 {len(open_conflicts)} 个待裁切块未裁定，不允许跳级发布", status=403
        )

    units = [u for u in _publish_units(batch) if not all(p["published"] for p in u["members"])]
    already_done = sum(1 for p in batch["pieces"] if p["published"])
    if not units:
        raise ClippingError("没有可发布的新增线段（冲突区全部保留原线时无需发布）")

    if batch["version"] is None:
        batch["version"] = _version_label()
    version = batch["version"]
    resume_note = "恢复续发，仅补未发布线段" if already_done else "正式发布"

    total_pieces = already_done + sum(
        len([p for p in unit["members"] if not p["published"]]) for unit in units
    )
    done = already_done
    for unit in units:
        pending_members = [p for p in unit["members"] if not p["published"]]
        _apply_unit(batch, unit, version)
        done += len(pending_members)
        if fail_after is not None and done - already_done >= fail_after:
            batch["interrupted"] = True
            return {
                "ok": False,
                "interrupted": True,
                "message": (
                    f"发布在第 {done}/{total_pieces} 段中断，"
                    "裁剪草稿已保留，恢复后只补未发布线段"
                ),
                "done": done,
                "total": total_pieces,
            }

    # 全部切块落图后，台账/待办/工程边界围绕同一版本一次重算。
    derived = _recompute_derived(version)
    new_seg_ids = {p["new_seg_id"] for p in batch["pieces"] if p["new_seg_id"]}
    trimmed = sum(
        1 for p in batch["pieces"]
        if p["kind"] == "conflict" and p.get("decision") == "use_formal"
    )
    added = len(new_seg_ids) - trimmed
    _state["versions"].append({
        "version": version,
        "filename": batch["filename"],
        "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "added_segments": added,
        "trimmed_segments": trimmed,
        "new_segment_ids": [p["new_seg_id"] for p in batch["pieces"] if p["new_seg_id"]],
        "ledger_count": len(derived["ledger"]),
        "patrol_count": len(derived["patrol_todos"]),
        "formal_bounds": len(derived["project_bounds"]),
    })
    _state["current_version"] = version
    # 记录本批处理过的所有有效线段指纹：无论裁入还是保留原线，重复上传都不再生成重叠。
    for candidate in batch["candidates"]:
        digest = candidate.get("digest")
        if digest and not candidate["errors"] and candidate["duplicate_of"] is None:
            _state["ingested"][digest] = {
                "segment_code": candidate["route_code"],
                "version": version,
                "start_m": candidate["start_m"],
                "end_m": candidate["end_m"],
            }
    batch["status"] = "published"
    batch["stage"] = "正式发布"
    batch["interrupted"] = False
    _state["draft"] = None
    return {
        "ok": True,
        "interrupted": False,
        "message": f"{resume_note}完成：发布版本 {version}，新增 {added} 段、裁定改线 {trimmed} 处",
        "version": version,
        "ledger": len(derived["ledger"]),
        "patrol_todos": len(derived["patrol_todos"]),
        "project_bounds": len(derived["project_bounds"]),
        "dangling_bounds": len(derived["project_refs"]) - len(derived["project_bounds"]),
    }


def discard_draft() -> dict[str, Any]:
    """废弃草稿：按回滚凭据把已中断落入的线段撤出，正式路网恢复原样。"""
    batch = _require_draft()
    for piece in reversed(batch["pieces"]):
        spec = piece.get("rollback")
        if not spec:
            continue
        _state["network"] = [seg for seg in _state["network"] if seg["id"] != spec["new_seg_id"]]
        _state["network"] = [seg for seg in _state["network"] if seg["id"] not in set(spec["remainder_ids"])]
        _state["network"].extend(dict(seg) for seg in spec["removed"])
    batch["status"] = "discarded"
    _state["draft"] = None
    return {"ok": True, "message": "裁剪草稿已废弃，正式层未受影响"}


# ---------------------------------------------------------------------------
# 查询与序列化
# ---------------------------------------------------------------------------
def _require_draft() -> dict[str, Any]:
    if _state["draft"] is None:
        raise ClippingError("当前没有裁剪草稿，请先导入文件")
    return _state["draft"]


def _find_candidate(candidate_id: int) -> dict[str, Any]:
    batch = _require_draft()
    candidate = next((item for item in batch["candidates"] if item["id"] == candidate_id), None)
    if candidate is None:
        raise ClippingError(f"候选线段 {candidate_id} 不存在", status=404)
    return candidate


def _find_piece(piece_id: int) -> dict[str, Any]:
    batch = _require_draft()
    piece = next((item for item in batch["pieces"] if item["id"] == piece_id), None)
    if piece is None:
        raise ClippingError(f"切块 {piece_id} 不存在", status=404)
    return piece


def _piece_view(piece: dict[str, Any]) -> dict[str, Any]:
    refs = [
        {"id": seg["id"], "code": seg["segment_code"], "name": seg["name"]}
        for seg in _state["network"] if seg["id"] in set(piece["overlap_with"])
    ]
    return {
        "id": piece["id"],
        "candidate_id": piece["candidate_id"],
        "route_code": piece["route_code"],
        "kind": piece["kind"],
        "kind_label": "可通行线段" if piece["kind"] == "passable" else "待裁切块",
        "stake_start": piece["stake_start"],
        "stake_end": piece["stake_end"],
        "start_m": piece["start_m"],
        "end_m": piece["end_m"],
        "checked": piece["checked"],
        "decision": piece["decision"],
        "decision_label": {
            "keep_existing": "保留既有边界",
            "use_formal": "采用正式桩号裁入",
        }.get(piece["decision"] or "", "待裁定" if piece["kind"] == "conflict" else ""),
        "published": piece["published"],
        "overlap_with": refs,
    }


def serialize_draft() -> dict[str, Any]:
    batch = _state["draft"]
    if batch is None:
        return {"exists": False}
    candidates = []
    for item in batch["candidates"]:
        candidates.append({
            "id": item["id"],
            "raw_line": item["raw_line"],
            "route_code": item["route_code"],
            "route_name": item["route_name"],
            "name": item["name"],
            "historical_name": item["historical_name"],
            "grade": item["grade"],
            "lanes": item["lanes"],
            "surface": item["surface"],
            "org": item["org"],
            "stake_start": format_stake(item["start_m"]) if item["start_m"] is not None else "—",
            "stake_end": format_stake(item["end_m"]) if item["end_m"] is not None else "—",
            "errors": item["errors"],
            "duplicate_of": item["duplicate_of"],
        })
    return {
        "exists": True,
        "id": batch["id"],
        "filename": batch["filename"],
        "created_at": batch["created_at"],
        "stage": batch["stage"],
        "stage_index": STAGES.index(batch["stage"]),
        "stages": STAGES,
        "interrupted": batch["interrupted"],
        "version": batch["version"],
        "candidates": candidates,
        "pieces": [_piece_view(piece) for piece in batch["pieces"]],
        "counts": {
            "passable": sum(1 for p in batch["pieces"] if p["kind"] == "passable"),
            "conflict": len(_open_conflicts(batch)),
            "checked": sum(1 for p in batch["pieces"] if p["checked"]),
            "unchecked": len(_unchecked_pieces(batch)),
            "published": sum(1 for p in batch["pieces"] if p["published"]),
            "duplicate": sum(1 for c in batch["candidates"] if c["duplicate_of"]),
            "error_lines": sum(1 for c in batch["candidates"] if c["errors"]),
        },
        "can_publish": (
            not _unchecked_pieces(batch)
            and not _open_conflicts(batch)
            and bool(_publish_units(batch))
        ),
    }


def map_view() -> dict[str, Any]:
    """底图数据：路线骨架、正式路段、草稿切块（带子折线坐标）。"""
    bootstrap()
    routes = []
    for route in _state["routes"].values():
        segments = []
        for seg in _state["network"]:
            if seg["code"] != route["code"]:
                continue
            segments.append({
                "id": seg["id"],
                "code": seg["segment_code"],
                "name": seg["name"],
                "stake_start": format_stake(seg["start_m"]),
                "stake_end": format_stake(seg["end_m"]),
                "points": geometry.polyline_points(
                    geometry.sub_polyline(route, seg["start_m"], seg["end_m"])
                ),
                "version": seg["version"],
            })
        routes.append({
            "code": route["code"],
            "name": route["name"],
            "length": format_stake(route["length_m"]),
            "points": geometry.polyline_points(
                [(float(x), float(y)) for x, y in route["coords"]]
            ),
            "segments": segments,
        })

    draft_pieces: list[dict[str, Any]] = []
    batch = _state["draft"]
    if batch is not None:
        for piece in batch["pieces"]:
            route = _state["routes"][piece["route_code"]]
            offset = 10 if piece["kind"] == "passable" else -10
            points = geometry.sub_polyline(route, piece["start_m"], piece["end_m"])
            shifted = [(x, y + offset) for x, y in points]
            view = _piece_view(piece)
            view["points"] = geometry.polyline_points(shifted)
            view["label_xy"] = geometry.point_at_m(
                route, (piece["start_m"] + piece["end_m"]) // 2
            )
            draft_pieces.append(view)

    return {
        "routes": routes,
        "draft_pieces": draft_pieces,
        "current_version": _state["current_version"],
    }


def state() -> dict[str, Any]:
    bootstrap()
    refs = _state["derived"].get("project_refs", [])
    return {
        "current_version": _state["current_version"],
        "versions": list(reversed(_state["versions"])),
        "draft": serialize_draft(),
        "derived": {
            "ledger": _state["derived"]["ledger"],
            "patrol_todos": _state["derived"]["patrol_todos"],
            "project_refs": refs,
            "project_bounds": _state["derived"]["project_bounds"],
        },
        "stats": {
            "segments": len(_state["network"]),
            "ledger": len(_state["derived"]["ledger"]),
            "patrol_todos": len(_state["derived"]["patrol_todos"]),
            "dangling_bounds": sum(1 for row in refs if row["引用状态"] != "正式层内"),
            "formal_bounds": len(_state["derived"]["project_bounds"]),
        },
    }
