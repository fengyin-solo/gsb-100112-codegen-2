"""线段裁剪台：管养路段底图改在这里裁剪、核对、裁定、发布。

推进顺序固定为「解析 → 分段核对 → 冲突裁定 → 正式发布」，任何线段没有完成前序
核对都不允许跳级发布。业务规则：

- 正式桩号优先于历史区划名称，历史名称只作挂载参考；
- 既有正式边界保留原线：新导入线段撞上正式层时，只裁出不重叠的可通行部分；
- 重复上传按「路线+桩号区间」去重，任何情况下都不生成重叠路段；
- 发布中断只留下裁剪草稿，恢复续发时补未发布线段，沿用同一个版本号；
- 每次发布都按同一版本重算路段台账、巡查待办、工程清单，悬空引用一律隔离，
  不得进入正式层。

状态全部放在内存里（与项目其他模块一致，只依赖标准库）。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.store import store

STAGES = ["解析", "分段核对", "冲突裁定", "正式发布"]

# 线段核对状态
ST_TODO = "待核对"
ST_CHECKED = "已核对"
ST_CLIPPED_OUT = "待裁剪"   # 冲突裁定的败诉线段，不进正式层

STAKE_RE = re.compile(r"K\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
EPS = 0.5  # 米；桩号重合的容差


def parse_stake(text: Any) -> float | None:
    """把 K3+200 这样的正式桩号解析成米；解析不了返回 None（视为桩号缺失）。"""
    match = STAKE_RE.search(str(text or ""))
    if not match:
        return None
    return int(match.group(1)) * 1000 + float(match.group(2))


def format_stake(value: float) -> str:
    """米还原成正式桩号写法。"""
    return f"K{int(value // 1000)}+{value % 1000:03.0f}"


def stake_range(start_m: float, end_m: float) -> str:
    return f"{format_stake(start_m)}～{format_stake(end_m)}"


def overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return min(a1, b1) - max(a0, b0) > EPS


class ClipWorkbenchService:
    def __init__(self) -> None:
        self._seg_seq = 0
        self._conflict_seq = 0
        self.draft: dict[str, Any] | None = None
        self.versions: list[dict[str, Any]] = []
        self.published_segments: list[dict[str, Any]] = []
        self.quarantine: list[dict[str, Any]] = []
        # 台账之外流转下来的历史引用（按历史区划名称/路段名挂到正式桩号线段上）
        self.legacy_patrol_refs = [
            {"ref_id": "L-PATR-01", "引用": "老城区片日常巡查", "历史名称": "老城区片",
             "巡查日期": "2026-10-02", "巡查人员": "王建国", "巡查车辆": "巡查车-07"},
            {"ref_id": "L-PATR-02", "引用": "北环线撤销段巡查", "历史名称": "北环已撤销片区",
             "巡查日期": "2026-10-02", "巡查人员": "李秀兰", "巡查车辆": "巡查车-03"},
        ]
        self.legacy_projects = [
            {"工程编号": "PROJ-1001", "工程名称": "G104城区段罩面工程", "工程类型": "中修",
             "施工路段引用": "G104城区段", "承建单位": "恒通路桥", "开工日期": "2026-09-20",
             "竣工日期": "2026-11-30", "工程状态": "施工中", "status": "施工中",
             "pending": True, "abnormal": True},
            {"工程编号": "PROJ-1002", "工程名称": "开发区段路口渠化", "工程类型": "专项",
             "施工路段引用": "开发区片", "承建单位": "市政工程处", "开工日期": "2026-10-10",
             "竣工日期": "2026-12-20", "工程状态": "待开工", "status": "待开工",
             "pending": True, "abnormal": False},
            {"工程编号": "PROJ-1003", "工程名称": "南环废弃线修缮", "工程类型": "小修",
             "施工路段引用": "南环废弃线", "承建单位": "远达养护", "开工日期": "2026-10-15",
             "竣工日期": "2026-10-25", "工程状态": "待开工", "status": "待开工",
             "pending": True, "abnormal": False},
        ]
        self._seed_baseline()

    # ------------------------------------------------------------------ 视图

    def state(self) -> dict[str, Any]:
        return {
            "stages": STAGES,
            "draft": self._draft_view(),
            "versions": list(self.versions),
            "current_version": self.versions[-1]["version"] if self.versions else None,
            "formal_summary": self._formal_summary(),
            "quarantine": list(self.quarantine),
        }

    def _draft_view(self) -> dict[str, Any] | None:
        if self.draft is None:
            return None
        draft = self.draft
        active = [s for s in draft["segments"] if s["state"] != ST_CLIPPED_OUT]
        return {
            "source_file": draft["source_file"],
            "stage": draft["stage"],
            "interrupted": draft["interrupted"],
            "version": draft.get("version"),
            "base_version": draft["base_version"],
            "segment_count": len(active),
            "checked_count": sum(1 for s in active if s["checked"]),
            "conflict_count": len(draft["conflicts"]),
            "resolved_count": sum(1 for c in draft["conflicts"] if c["resolved"]),
            "published_count": sum(1 for s in draft["segments"] if s["published_version"]),
            "segments": [self._segment_view(s) for s in draft["segments"]],
            "conflicts": [dict(c) for c in draft["conflicts"]],
            "duplicates": list(draft["duplicates"]),
            "parse_errors": list(draft["parse_errors"]),
            "blockers": self._stage_blockers(),
        }

    def _segment_view(self, seg: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": seg["id"],
            "路线": seg["line"],
            "路段名称": seg["name"],
            "正式桩号": stake_range(seg["start_m"], seg["end_m"]),
            "历史区划名称": seg["legacy_region"],
            "state": seg["state"],
            "checked": seg["checked"],
            "clipped_by_formal": seg["clipped_by_formal"],
            "conflict_ids": list(seg["conflict_ids"]),
            "published_version": seg["published_version"],
            "code": seg.get("code"),
        }

    def _stage_blockers(self) -> list[str]:
        """当前草稿要进入下一阶段还差什么——门禁说明直接给前端展示。"""
        if self.draft is None:
            return ["尚未导入文件，先在裁剪台导入底图文件"]
        draft = self.draft
        active = [s for s in draft["segments"] if s["state"] != ST_CLIPPED_OUT]
        if draft["stage"] == STAGES[0]:
            return [] if draft["segments"] else ["解析结果为空，没有可裁剪线段"]

        if draft["stage"] == STAGES[1]:
            unchecked = [s for s in active if not s["checked"]]
            return [f"线段 {s['line']} {stake_range(s['start_m'], s['end_m'])} 未完成分段核对"
                    for s in unchecked]
        if draft["stage"] == STAGES[2]:
            pending = [c for c in draft["conflicts"] if not c["resolved"]]
            return [f"待裁切块 {c['line']} {stake_range(c['range'][0], c['range'][1])} 尚未裁定"
                    for c in pending]
        return []  # 正式发布阶段由 publish 自己把关

    # ------------------------------------------------------------------ 导入

    def import_file(self, file_name: str, content: str) -> tuple[dict[str, Any] | None, str]:
        if self.draft is not None and not self.draft.get("finished"):
            if self.draft["interrupted"]:
                return None, "上一版发布中断，裁剪草稿仍在：请先恢复续发，补齐未发布线段"
            return None, "已有裁剪草稿在推进中，完成或放弃后才能重新导入"

        segments: list[dict[str, Any]] = []
        parse_errors: list[str] = []
        duplicates: list[str] = []
        seen: set[tuple[str, int, int]] = set()

        for lineno, raw in enumerate(str(content).splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if lineno == 1 and line.startswith("路线,"):
                continue  # 表头
            if len(parts) < 4:
                parse_errors.append(f"第 {lineno} 行字段不足，已跳过：{line}")
                continue
            line_code, name, start_text, end_text = parts[:4]
            legacy = parts[4] if len(parts) > 4 else ""
            start_m, end_m = parse_stake(start_text), parse_stake(end_text)
            if start_m is None or end_m is None or end_m <= start_m:
                parse_errors.append(
                    f"第 {lineno} 行桩号无法解析或起终倒置（{start_text}→{end_text}），已跳过：{line}")
                continue
            key = (line_code, int(start_m), int(end_m))
            if self._is_published_exact(line_code, start_m, end_m) or key in seen:
                scope = "正式层已存在" if self._is_published_exact(line_code, start_m, end_m) else "本文件内重复"
                duplicates.append(
                    f"{line_code} {stake_range(start_m, end_m)}（{name}）与{scope}完全重叠，"
                    f"重复上传不生成重叠路段")
                continue
            seen.add(key)
            segments.append(self._new_segment(line_code, name, start_m, end_m, legacy))

        if not segments:
            # 解析不出线段时，把去重/解析错误原因一并说明，避免只收到一句"没有线段"
            if duplicates:
                return None, "未生成新线段（重复上传不生成重叠路段）：" + "；".join(duplicates)
            if parse_errors:
                return None, "没有可通行线段进入裁剪台：" + "；".join(parse_errors)
            return None, "文件里没有解析出任何线段"
        # 既有边界保留原线：沿正式层裁掉重叠，只剩可通行线段；全包则跳过、不生成重叠
        clipped: list[dict[str, Any]] = []
        for seg in segments:
            raw_start, raw_end = seg["raw_range"]
            parts_remain = self._subtract_published(seg["line"], seg["start_m"], seg["end_m"])
            if not parts_remain:
                duplicates.append(
                    f"{seg['line']} {stake_range(raw_start, raw_end)}"
                    f"（{seg['name']}）与正式层完全重叠，按既有边界保留原线跳过")
                continue
            for index, (part_start, part_end) in enumerate(parts_remain):
                if index == 0:
                    seg["start_m"], seg["end_m"] = part_start, part_end
                    seg["clipped_by_formal"] = (part_start, part_end) != (raw_start, raw_end)
                    clipped.append(seg)
                else:
                    clipped.append(self._new_segment(
                        seg["line"], f"{seg['name']}（裁后分段）", part_start, part_end,
                        seg["legacy_region"], clipped_by_formal=True))

        conflicts = self._build_conflicts(clipped)
        if not clipped:
            # 解析过但全部按既有边界裁掉/去重：把原因带回去，不能只说"没解析出线段"
            reasons = "；".join(duplicates) or "全部线段桩号无效"
            return None, f"没有可通行线段进入裁剪台：{reasons}"
        self.draft = {
            "source_file": file_name or "导入文件",
            "stage": STAGES[0],
            "segments": clipped,
            "conflicts": conflicts,
            "duplicates": duplicates,
            "parse_errors": parse_errors,
            "interrupted": False,
            "finished": False,
            "base_version": self.versions[-1]["version"] if self.versions else "（空底图）",
            "version": None,
        }
        return self._draft_view(), ""

    def _new_segment(self, line: str, name: str, start_m: float, end_m: float,
                     legacy: str, *, clipped_by_formal: bool = False) -> dict[str, Any]:
        self._seg_seq += 1
        return {
            "id": self._seg_seq,
            "line": line,
            "name": name,
            "start_m": float(start_m),
            "end_m": float(end_m),
            "raw_range": (float(start_m), float(end_m)),
            "legacy_region": legacy,
            "checked": False,
            "state": ST_TODO,
            "conflict_ids": [],
            "clipped_by_formal": clipped_by_formal,
            "published_version": None,
            "code": None,
        }

    def _is_published_exact(self, line: str, start_m: float, end_m: float) -> bool:
        for pub in self.published_segments:
            if pub["line"] == line and abs(pub["start_m"] - start_m) <= EPS \
                    and abs(pub["end_m"] - end_m) <= EPS:
                return True
        return False

    def _subtract_published(self, line: str, start_m: float,
                            end_m: float) -> list[tuple[float, float]]:
        """区间减法：新线段减去同路线正式层，返回剩余可通行区间（可能多段或为空）。"""
        parts = [(start_m, end_m)]
        for pub in self.published_segments:
            if pub["line"] != line:
                continue
            remain: list[tuple[float, float]] = []
            for lo, hi in parts:
                if not overlaps(lo, hi, pub["start_m"], pub["end_m"]):
                    remain.append((lo, hi))
                    continue
                if lo < pub["start_m"]:
                    remain.append((lo, pub["start_m"]))
                if pub["end_m"] < hi:
                    remain.append((pub["end_m"], hi))
            parts = remain
        return [(lo, hi) for lo, hi in parts if hi - lo > EPS]

    def _build_conflicts(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同路线批内重叠 → 合并成待裁切块（并查集聚类，重叠对不重复出块）。"""
        parent = {s["id"]: s["id"] for s in segments}

        def find(x: int) -> int:
            parent[x] = x if parent[x] == x else find(parent[x])
            return parent[x]

        def union(a: int, b: int) -> None:
            parent[find(a)] = find(b)

        by_line: dict[str, list[dict[str, Any]]] = {}
        for seg in segments:
            by_line.setdefault(seg["line"], []).append(seg)
        for peers in by_line.values():
            for i, a in enumerate(peers):
                for b in peers[i + 1:]:
                    if overlaps(a["start_m"], a["end_m"], b["start_m"], b["end_m"]):
                        union(a["id"], b["id"])

        groups: dict[int, list[dict[str, Any]]] = {}
        for seg in segments:
            groups.setdefault(find(seg["id"]), []).append(seg)

        conflicts: list[dict[str, Any]] = []
        for peers in groups.values():
            if len(peers) < 2:
                continue
            self._conflict_seq += 1
            cid = self._conflict_seq
            block = {
                "id": cid,
                "line": peers[0]["line"],
                "range": [min(s["start_m"] for s in peers), max(s["end_m"] for s in peers)],
                "segment_ids": [s["id"] for s in peers],
                "resolved": False,
                "winner_id": None,
                "rule": "正式桩号区间优先，历史区划名称仅作参考",
            }
            conflicts.append(block)
            for seg in peers:
                seg["conflict_ids"].append(cid)
        return conflicts

    # ------------------------------------------------------------------ 核对

    def check_segment(self, seg_id: int) -> tuple[bool, str]:
        draft = self._require_draft()
        if draft["stage"] != STAGES[1]:
            return False, f"当前处于「{draft['stage']}」阶段，分段核对只在「{STAGES[1]}」进行"
        seg = self._find_segment(seg_id)
        if seg is None:
            return False, f"线段 {seg_id} 不在当前裁剪草稿里"
        if seg["state"] == ST_CLIPPED_OUT:
            return False, "该线段已在冲突裁定中被判离，不能再核对"
        if seg["end_m"] <= seg["start_m"]:
            return False, "正式桩号起终倒置，不能通过核对"
        seg["checked"] = True
        seg["state"] = ST_CHECKED
        return True, f"已按正式桩号核对：{seg['line']} {stake_range(seg['start_m'], seg['end_m'])}"

    def check_all(self) -> tuple[int, str]:
        draft = self._require_draft()
        if draft["stage"] != STAGES[1]:
            return 0, f"当前处于「{draft['stage']}」阶段，不能批量核对"
        count = 0
        for seg in draft["segments"]:
            if seg["state"] != ST_CLIPPED_OUT and not seg["checked"]:
                seg["checked"] = True
                seg["state"] = ST_CHECKED
                count += 1
        return count, f"已按正式桩号批量核对 {count} 条线段"

    # ------------------------------------------------------------------ 裁定

    def resolve_conflict(self, conflict_id: int, winner_id: int) -> tuple[bool, str]:
        draft = self._require_draft()
        if draft["stage"] != STAGES[2]:
            return False, f"当前处于「{draft['stage']}」阶段，冲突裁定只在「{STAGES[2]}」进行"
        block = next((c for c in draft["conflicts"] if c["id"] == conflict_id), None)
        if block is None:
            return False, f"待裁切块 {conflict_id} 不存在"
        if block["resolved"]:
            return False, "该待裁切块已经裁定"
        if winner_id not in block["segment_ids"]:
            return False, "裁定保留的线段不属于这个待裁切块"
        block["resolved"] = True
        block["winner_id"] = winner_id
        for seg_id in block["segment_ids"]:
            seg = self._find_segment(seg_id)
            if seg is None:
                continue
            if seg_id == winner_id:
                seg["state"] = ST_CHECKED if seg["checked"] else ST_TODO
            else:
                seg["state"] = ST_CLIPPED_OUT  # 败诉：待裁剪，不允许发布
        winner = self._find_segment(winner_id)
        return True, (f"待裁切块已裁定：保留 {winner['line']} "
                      f"{stake_range(winner['start_m'], winner['end_m'])}，重叠段按正式桩号裁离")

    # ------------------------------------------------------------------ 推进

    def advance(self) -> tuple[bool, str]:
        draft = self._require_draft()
        blockers = self._stage_blockers()
        if blockers:
            return False, "还不能进入下一阶段：" + "；".join(blockers)
        idx = STAGES.index(draft["stage"])
        if idx >= len(STAGES) - 1:
            return False, "已到正式发布阶段，请执行发布"
        draft["stage"] = STAGES[idx + 1]
        return True, f"已进入「{draft['stage']}」阶段"

    # ------------------------------------------------------------------ 发布

    def publish(self, *, limit: int | None = None) -> tuple[dict[str, Any] | None, str]:
        draft = self._require_draft()
        if draft["stage"] != STAGES[3]:
            return None, f"当前处于「{draft['stage']}」，未完成解析、核对、裁定前不许跳级发布"
        eligible = self._publishable_segments()
        if not eligible:
            return None, "没有完成全部前序核对的线段可发布（未核对/未裁定的线段一律拦下）"
        if limit is not None:
            eligible = eligible[:max(limit, 0)]
        if not eligible:
            return None, "本次发布数量为 0"

        if draft["version"] is None:
            draft["version"] = self._next_version()
            self.versions.append({
                "version": draft["version"],
                "status": "部分发布",
                "base_version": draft["base_version"],
                "source_file": draft["source_file"],
                "created_at": str(date.today()),
                "completed_at": None,
                "segments": [],
            })

        codes_now: list[str] = []
        for seq_index, seg in enumerate(eligible, start=len(self.published_segments) + 1):
            code = f"{seg['line']}-S{seq_index:03d}"
            seg["code"] = code
            seg["published_version"] = draft["version"]
            codes_now.append(code)
            self.published_segments.append({
                "code": code,
                "line": seg["line"],
                "name": seg["name"],
                "start_m": seg["start_m"],
                "end_m": seg["end_m"],
                "legacy_region": seg["legacy_region"],
                "first_version": draft["version"],
                "boundary": "原线",
            })

        version = self._find_version(draft["version"])
        version["segments"] = [s["code"] for s in self.published_segments]
        layers = self._recompute_layers(draft["version"])

        remaining = self._publishable_segments()
        if remaining:
            # 发布中断：裁剪草稿保留，恢复时只补这些未发布线段
            draft["interrupted"] = True
            message = (f"版本 {draft['version']} 已发布 {len(codes_now)} 条后中断，"
                       f"剩余 {len(remaining)} 条保留在裁剪草稿，恢复后只补未发布线段")
            status = "部分发布"
        else:
            draft["interrupted"] = False
            draft["finished"] = True
            version["status"] = "正式发布"
            version["completed_at"] = str(date.today())
            message = f"版本 {draft['version']} 已正式发布，台账/巡查/工程清单已按同版本重算完成"
            status = "正式发布"
            self.draft = None

        return {
            "version": draft["version"] if self.draft else version["version"],
            "status": status,
            "published_now": codes_now,
            "remaining": len(remaining),
            "layers": layers,
        }, message

    def resume(self) -> tuple[dict[str, Any] | None, str]:
        if self.draft is None or not self.draft["interrupted"]:
            return None, "没有中断的裁剪草稿需要恢复"
        return self.publish()

    def _publishable_segments(self) -> list[dict[str, Any]]:
        draft = self.draft
        if draft is None:
            return []
        result = []
        for seg in draft["segments"]:
            if seg["published_version"] or seg["state"] == ST_CLIPPED_OUT:
                continue
            if not seg["checked"]:
                continue  # 未完成分段核对，不许跳级
            if any(not self._conflict_resolved_for(seg, cid) for cid in seg["conflict_ids"]):
                continue  # 所属待裁切块还没裁定
            result.append(seg)
        result.sort(key=lambda s: (s["line"], s["start_m"]))
        return result

    def _conflict_resolved_for(self, seg: dict[str, Any], conflict_id: int) -> bool:
        block = next(c for c in self.draft["conflicts"] if c["id"] == conflict_id)
        return block["resolved"] and block["winner_id"] == seg["id"]

    def _next_version(self) -> str:
        year = date.today().year
        max_seq = 0
        prefix = f"v{year}."
        for ver in self.versions:
            if ver["version"].startswith(prefix):
                try:
                    max_seq = max(max_seq, int(ver["version"][len(prefix):]))
                except ValueError:
                    continue
        return f"{prefix}{max_seq + 1}"

    def _find_version(self, version_no: str) -> dict[str, Any]:
        return next(v for v in self.versions if v["version"] == version_no)

    # ------------------------------------------------------------- 同版重算

    def _recompute_layers(self, version_no: str) -> dict[str, Any]:
        """发布即重算：路段台账、巡查待办、工程清单全部锚定同一个发布版本。

        历史引用先按正式桩号线段、再按名称/历史区划挂载；挂不上的就是悬空引用，
        只进隔离清单，绝不写入正式层。
        """
        ordered = sorted(self.published_segments, key=lambda s: (s["line"], s["start_m"]))

        # 1) 路段台账（正式底图）
        ledger_rows: list[dict[str, Any]] = []
        for idx, seg in enumerate(ordered, start=1):
            ledger_rows.append({
                "id": idx,
                "status": "正常", "pending": False, "abnormal": False,
                "路段编号": seg["code"],
                "路段名称": seg["name"],
                "路线": seg["line"],
                "起止桩号": stake_range(seg["start_m"], seg["end_m"]),
                "道路等级": "一级公路",
                "车道数": "4",
                "路面类型": "沥青混凝土",
                "管养单位": "市政养护一处",
                "路段状态": "正常",
                "边界来源": "既有边界保留原线" if seg["first_version"] != version_no else "本次裁剪",
                "历史区划名称": seg["legacy_region"],
                "发布版本": seg["first_version"] if seg["first_version"] == version_no
                else self._segment_version_tag(seg, version_no),
            })
        store.replace_rows("road_section", ledger_rows)

        # 2) 巡查待办 / 巡查区间
        patrol_rows: list[dict[str, Any]] = []
        pid = 0
        for seg in ordered:
            pid += 1
            patrol_rows.append({
                "id": pid,
                "status": "待巡查", "pending": True, "abnormal": False,
                "巡查编号": f"PATR-{pid:04d}",
                "巡查路段": seg["code"],
                "路段名称": seg["name"],
                "巡查区间": stake_range(seg["start_m"], seg["end_m"]),
                "巡查日期": str(date.today()),
                "巡查人员": "按版本派单",
                "巡查车辆": "—",
                "发现问题": "—",
                "处置措施": "—",
                "巡查状态": "待巡查",
                "发布版本": version_no,
            })
        for ref in self.legacy_patrol_refs:
            seg = self._resolve_reference(ref["历史名称"])
            if seg is None:
                continue
            pid += 1
            patrol_rows.append({
                "id": pid,
                "status": "待巡查", "pending": True, "abnormal": False,
                "巡查编号": ref["ref_id"],
                "巡查路段": seg["code"],
                "路段名称": seg["name"],
                "巡查区间": stake_range(seg["start_m"], seg["end_m"]),
                "巡查日期": ref["巡查日期"],
                "巡查人员": ref["巡查人员"],
                "巡查车辆": ref["巡查车辆"],
                "发现问题": "—",
                "处置措施": "—",
                "巡查状态": "待巡查",
                "挂载依据": "历史区划名称→正式桩号线段",
                "发布版本": version_no,
            })
        store.replace_rows("patrol", patrol_rows)

        # 3) 工程清单 / 工程边界
        project_rows: list[dict[str, Any]] = []
        quarantine: list[dict[str, Any]] = []
        for proj in self.legacy_projects:
            seg = self._resolve_reference(proj["施工路段引用"])
            if seg is None:
                quarantine.append({
                    "类型": "养护工程",
                    "编号": proj["工程编号"],
                    "名称": proj["工程名称"],
                    "悬空引用": proj["施工路段引用"],
                    "原因": "正式层中找不到对应正式桩号线段，禁止进入正式工程清单",
                    "尝试版本": version_no,
                })
                continue
            row = dict(proj)
            row["id"] = len(project_rows) + 1
            row["施工路段"] = seg["code"]
            row["工程边界"] = stake_range(seg["start_m"], seg["end_m"])
            row["挂载依据"] = ("正式桩号匹配" if proj["施工路段引用"] == seg["name"]
                           else "历史区划名称→正式桩号线段")
            row["发布版本"] = version_no
            project_rows.append(row)
        for ref in self.legacy_patrol_refs:
            if self._resolve_reference(ref["历史名称"]) is not None:
                continue
            quarantine.append({
                "类型": "巡查待办",
                "编号": ref["ref_id"],
                "名称": ref["引用"],
                "悬空引用": ref["历史名称"],
                "原因": "正式层中找不到对应正式桩号线段，巡查区间无法生成",
                "尝试版本": version_no,
            })
        store.replace_rows("project", project_rows)
        self.quarantine = quarantine

        return {
            "version": version_no,
            "ledger_total": len(ledger_rows),
            "patrol_total": len(patrol_rows),
            "project_total": len(project_rows),
            "quarantine_total": len(quarantine),
        }

    def _segment_version_tag(self, seg: dict[str, Any], current_version: str) -> str:
        return f"{seg['first_version']}（沿用至 {current_version}）"

    def _resolve_reference(self, ref_text: str) -> dict[str, Any] | None:
        """历史引用解析：能对上正式桩号线段名称最好，其次对历史区划名称。"""
        for seg in self.published_segments:
            if ref_text == seg["name"]:
                return seg
        for seg in self.published_segments:
            if ref_text == seg["legacy_region"]:
                return seg
        return None

    def _formal_summary(self) -> dict[str, Any]:
        return {
            "ledger_total": len(store.rows("road_section")),
            "patrol_total": len(store.rows("patrol")),
            "project_total": len(store.rows("project")),
            "quarantine_total": len(self.quarantine),
        }

    def layers(self, version_no: str | None = None) -> dict[str, Any]:
        target = version_no or (self.versions[-1]["version"] if self.versions else None)
        return {
            "version": target,
            "road_section": store.rows("road_section"),
            "patrol": store.rows("patrol"),
            "project": store.rows("project"),
            "quarantine": self.quarantine,
        }

    # ------------------------------------------------------------- 裁剪台地图

    def map_view(self) -> dict[str, Any]:
        """给前端地图返回摆位坐标：每条路线一条泳道，上正式层、下草稿层，
        待裁切块用红色区间直接画在草稿线段上。"""
        width, pad_left, band, lane_gap = 960, 96, 96, 26
        draft = self.draft
        draft_segments = draft["segments"] if draft else []
        lines = sorted({s["line"] for s in self.published_segments}
                       | {s["line"] for s in draft_segments})
        max_end = max([s["end_m"] for s in self.published_segments]
                      + [s["end_m"] for s in draft_segments] + [1000])

        def x_of(meters: float) -> float:
            return pad_left + meters / max_end * (width - pad_left - 24)

        items: list[dict[str, Any]] = []
        lane_rows: list[dict[str, Any]] = []
        for idx, line in enumerate(lines):
            y = 28 + idx * band
            lane_rows.append({"line": line, "y": y})
            for seg in self.published_segments:
                if seg["line"] != line:
                    continue
                items.append({
                    "kind": "formal", "code": seg["code"], "name": seg["name"],
                    "line": line, "x0": x_of(seg["start_m"]), "x1": x_of(seg["end_m"]),
                    "y": y, "label": stake_range(seg["start_m"], seg["end_m"]),
                })
            for seg in draft_segments:
                if seg["line"] != line:
                    continue
                items.append({
                    "kind": "draft", "id": seg["id"], "name": seg["name"],
                    "state": seg["state"], "checked": seg["checked"],
                    "line": line, "x0": x_of(seg["start_m"]), "x1": x_of(seg["end_m"]),
                    "y": y + lane_gap,
                    "label": stake_range(seg["start_m"], seg["end_m"]),
                    "clipped_by_formal": seg["clipped_by_formal"],
                })

        blocks = []
        if draft:
            line_y = {row["line"]: row["y"] for row in lane_rows}
            for block in draft["conflicts"]:
                blocks.append({
                    "id": block["id"], "line": block["line"],
                    "x0": x_of(block["range"][0]), "x1": x_of(block["range"][1]),
                    "y": line_y[block["line"]] + lane_gap,
                    "label": stake_range(block["range"][0], block["range"][1]),
                    "resolved": block["resolved"], "winner_id": block["winner_id"],
                })

        height = max(28 + len(lines) * band, 180)
        return {"width": width, "height": height, "lanes": lane_rows,
                "items": items, "blocks": blocks}

    # ------------------------------------------------------------------ 基建

    def _require_draft(self) -> dict[str, Any]:
        if self.draft is None:
            raise ValueError("裁剪台上没有草稿，请先导入底图文件")
        return self.draft

    def _find_segment(self, seg_id: int) -> dict[str, Any] | None:
        if self.draft is None:
            return None
        return next((s for s in self.draft["segments"] if s["id"] == seg_id), None)

    def _seed_baseline(self) -> None:
        """预置一个已发布基线版本，让台账/巡查/工程清单从一开始就锚定正式层。"""
        baseline = [
            ("G104", "G104城区段", 0.0, 2500.0, "老城区片"),
            ("G104", "G104开发区段", 2500.0, 5000.0, "开发区片"),
            ("G205", "G205北环段", 1000.0, 3800.0, "北环片"),
        ]
        version_no = f"v{date.today().year}.0"
        for idx, (line, name, start_m, end_m, legacy) in enumerate(baseline, start=1):
            code = f"{line}-S{idx:03d}"
            self.published_segments.append({
                "code": code, "line": line, "name": name,
                "start_m": start_m, "end_m": end_m, "legacy_region": legacy,
                "first_version": version_no, "boundary": "原线",
            })
        self.versions.append({
            "version": version_no,
            "status": "正式发布",
            "base_version": "（空底图）",
            "source_file": "基线导入",
            "created_at": str(date.today()),
            "completed_at": str(date.today()),
            "segments": [s["code"] for s in self.published_segments],
        })
        self._recompute_layers(version_no)


service = ClipWorkbenchService()
