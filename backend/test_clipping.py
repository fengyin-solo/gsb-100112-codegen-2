"""线段裁剪台业务规则测试：只依赖标准库 unittest，直接驱动服务层。

覆盖：
- 四阶段顺序强约束，未核对/未裁定不许发布（不许跳级）
- 导入即裁剪：可通行线段 + 待裁切块；端点相接不算冲突（既有边界保留原线）
- 正式桩号优先；保留原线不改既有路段
- 发布原子重算台账/巡查待办/工程边界；悬空引用不进正式层
- 发布中断留草稿，恢复只补未发布线段
- 重复上传幂等，不产生重叠路段
- 废弃草稿回滚
"""
from __future__ import annotations

import unittest

from app import geometry
from app.services import clipping


def sample_csv(lines: list[tuple[str, str, str]] | None = None) -> str:
    rows = [
        "路线,起点桩号,终点桩号,路段名称,历史区划,道路等级,车道数,路面类型,管养单位"
    ]
    for code, start, end in (lines or [
        ("G104", "K1+200", "K3+000"),
        ("S205", "K1+200", "K2+200"),
    ]):
        rows.append(f"{code},{start},{end},{code}新线段,老区划,主干路,4,沥青,市政处")
    return "\n".join(rows)


class StakeTests(unittest.TestCase):
    def test_parse_formats(self) -> None:
        self.assertEqual(geometry.parse_stake("K2+500"), 2500)
        self.assertEqual(geometry.parse_stake("2+500"), 2500)
        self.assertEqual(geometry.parse_stake("2500"), 2500)
        self.assertEqual(geometry.parse_stake("2.5km"), 2500)
        self.assertEqual(geometry.format_stake(2500), "K2+500")

    def test_invalid(self) -> None:
        for bad in ("", None, "abc", "K2+", "三公里"):
            with self.assertRaises(geometry.StakeError):
                geometry.parse_stake(bad)

    def test_split_touching_boundary_no_conflict(self) -> None:
        occupied = [{"id": 1, "start_m": 0, "end_m": 1200}]
        pieces = geometry.split_by_published(1200, 2000, occupied)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]["kind"], "passable")

    def test_split_overlap(self) -> None:
        occupied = [{"id": 1, "start_m": 0, "end_m": 2500}]
        pieces = geometry.split_by_published(1200, 3000, occupied)
        kinds = [(p["kind"], p["start_m"], p["end_m"]) for p in pieces]
        self.assertEqual(kinds, [("conflict", 1200, 2500), ("passable", 2500, 3000)])


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        clipping.reset_state()

    def test_full_flow_with_versioned_recompute(self) -> None:
        result = clipping.import_file("s.csv", sample_csv())
        draft = result["batch"]
        self.assertEqual(draft["stage"], "分段核对")
        # G104 1200~3000：conflict 1200~2500 + passable 2500~3000；S205：passable 1200~2200
        kinds = sorted((p["kind"], p["start_m"], p["end_m"]) for p in draft["pieces"])
        self.assertIn(("conflict", 1200, 2500), kinds)
        self.assertIn(("passable", 2500, 3000), kinds)
        self.assertIn(("passable", 1200, 2200), kinds)
        self.assertEqual(draft["counts"]["conflict"], 1)
        self.assertEqual(draft["counts"]["passable"], 2)

        # 未核对先发布：403，跳级被拦
        with self.assertRaises(clipping.ClippingError) as ctx:
            clipping.publish()
        self.assertEqual(ctx.exception.status, 403)

        clipping.check_all()
        # 核对完仍有冲突，停在冲突裁定
        self.assertEqual(clipping.state()["draft"]["stage"], "冲突裁定")
        with self.assertRaises(clipping.ClippingError) as ctx2:
            clipping.publish()
        self.assertEqual(ctx2.exception.status, 403)

        conflict = next(p for p in draft["pieces"] if p["kind"] == "conflict")
        # 保留既有边界：原线不动；G104 2500~3000 与 S205 1200~2200 两段新线发布
        clipping.decide_conflict(conflict["id"], "keep_existing")
        publish = clipping.publish()
        self.assertTrue(publish["ok"])
        self.assertEqual(publish["version"], clipping.state()["current_version"])

        state = clipping.state()
        version = state["current_version"]
        # 台账：初始 3 段 + 新增 2 段 = 5，全部带同一发布版本
        self.assertEqual(len(state["derived"]["ledger"]), 5)
        self.assertTrue(all(row["发布版本"] in (version, clipping.BASELINE_VERSION)
                            for row in state["derived"]["ledger"]))
        self.assertEqual(len(state["derived"]["patrol_todos"]), 5)
        # PROJ-0002（G104 2600~2900）与 PROJ-0003（S205 1500~2000）落入正式层；PROJ-0001 本就在
        self.assertEqual(state["stats"]["dangling_bounds"], 0)
        self.assertGreaterEqual(state["stats"]["formal_bounds"], 3)
        self.assertFalse(state["draft"]["exists"])

    def test_dangling_reference_blocked_until_network_covers(self) -> None:
        # 基线：PROJ-0002(G104 2600~2900) 与 PROJ-0003(S205 1500~2000) 悬空
        refs = clipping.state()["derived"]["project_refs"]
        dangling = {row["工程编号"]: row for row in refs if row["引用状态"] != "正式层内"}
        self.assertIn("PROJ-0002", dangling)
        self.assertIn("PROJ-0003", dangling)
        self.assertIn("K2+600~K2+900", dangling["PROJ-0002"]["缺失区间"])
        # 正式边界层不接收悬空工程
        self.assertEqual(clipping.state()["stats"]["formal_bounds"], 1)

    def test_formal_stake_decision_requires_merged_piece_checked(self) -> None:
        # G104 1200~3000：冲突 1200~2500 + 相邻可通行 2500~3000，只勾冲突块直接裁入应被拦
        clipping.import_file("s.csv", sample_csv([("G104", "K1+200", "K3+000")]))
        conflict = next(p for p in clipping.state()["draft"]["pieces"] if p["kind"] == "conflict")
        gap = next(p for p in clipping.state()["draft"]["pieces"] if p["kind"] == "passable")
        clipping.check_piece(conflict["id"], True)
        with self.assertRaises(clipping.ClippingError):
            clipping.decide_conflict(conflict["id"], "use_formal")
        # 勾完相邻可通行段后可裁入；最终一条完整新线 1200~3000
        clipping.check_piece(gap["id"], True)
        clipping.decide_conflict(conflict["id"], "use_formal")
        clipping.publish()
        g104 = sorted(
            (seg["start_m"], seg["end_m"])
            for seg in clipping._state["network"] if seg["code"] == "G104"
        )
        self.assertEqual(g104, [(0, 1200), (1200, 3000)])

    def test_formal_stake_decision_trims_existing(self) -> None:
        clipping.import_file("s.csv", sample_csv([("G104", "K1+200", "K3+000")]))
        clipping.check_all()
        conflict = next(p for p in clipping.state()["draft"]["pieces"] if p["kind"] == "conflict")
        before = {seg["id"]: (seg["start_m"], seg["end_m"]) for seg in clipping._state["network"]}
        clipping.decide_conflict(conflict["id"], "use_formal")
        clipping.publish()
        g104 = sorted(
            (seg["start_m"], seg["end_m"])
            for seg in clipping._state["network"] if seg["code"] == "G104"
        )
        # 原 1200~2500 被整段替换为新线段（0~1200 保留原线）
        self.assertEqual(g104, [(0, 1200), (1200, 3000)])
        self.assertNotIn(
            next(sid for sid, span in before.items() if span == (1200, 2500)),
            [seg["id"] for seg in clipping._state["network"]],
        )

    def test_publish_interrupt_then_resume_only_unpublished(self) -> None:
        clipping.import_file("s.csv", sample_csv())
        clipping.check_all()
        conflict = next(p for p in clipping.state()["draft"]["pieces"] if p["kind"] == "conflict")
        clipping.decide_conflict(conflict["id"], "keep_existing")
        interrupted = clipping.publish(fail_after=1)
        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(interrupted["done"], 1)
        draft = clipping.state()["draft"]
        self.assertTrue(draft["interrupted"])
        self.assertEqual(draft["counts"]["published"], 1)
        # 中断期间派生层仍是基线（台账未重算）
        self.assertEqual(len(clipping.state()["derived"]["ledger"]), 3)

        resumed = clipping.publish()
        self.assertTrue(resumed["ok"])
        self.assertIn("恢复续发", resumed["message"])
        # 恢复只补未发布：最终仍是 5 段，没有重叠
        network = clipping._state["network"]
        for code in ("G104", "S205"):
            spans = sorted((s["start_m"], s["end_m"]) for s in network if s["code"] == code)
            for (a, b), (c, d) in zip(spans, spans[1:]):
                self.assertLessEqual(b, c, "路段里程出现重叠")
        self.assertEqual(len(clipping.state()["derived"]["ledger"]), 5)

    def test_repeated_upload_is_idempotent(self) -> None:
        # 第一次发一段
        clipping.import_file("s.csv", sample_csv([("S205", "K1+200", "K2+200")]))
        clipping.check_all()
        clipping.publish()
        count_after_first = len(clipping._state["network"])
        # 完全重合再传：整段被认作已发布，没有有效线段，导入被拒且不产生重叠
        with self.assertRaises(clipping.ClippingError) as ctx:
            clipping.import_file("s.csv", sample_csv([("S205", "K1+200", "K2+200")]))
        self.assertIn("重复", str(ctx.exception))
        self.assertFalse(clipping.state()["draft"]["exists"])
        self.assertEqual(len(clipping._state["network"]), count_after_first)

    def test_repeated_upload_after_keep_existing_is_idempotent(self) -> None:
        # G104 1200~3000：冲突段保留原线、2500~3000 新线段发布
        clipping.import_file("s.csv", sample_csv([("G104", "K1+200", "K3+000")]))
        clipping.check_all()
        conflict = next(p for p in clipping.state()["draft"]["pieces"] if p["kind"] == "conflict")
        clipping.decide_conflict(conflict["id"], "keep_existing")
        clipping.publish()
        network_size = len(clipping._state["network"])
        # 同一文件再传：整批认定已处理，不重新生成冲突草稿，不产生重叠路段
        with self.assertRaises(clipping.ClippingError) as ctx:
            clipping.import_file("s.csv", sample_csv([("G104", "K1+200", "K3+000")]))
        self.assertIn("重复", str(ctx.exception))
        self.assertFalse(clipping.state()["draft"]["exists"])
        self.assertEqual(len(clipping._state["network"]), network_size)

    def test_duplicate_within_same_file(self) -> None:
        rows = [("G104", "K2+600", "K2+900"), ("G104", "K2+600", "K2+900")]
        draft = clipping.import_file("s.csv", sample_csv(rows))["batch"]
        self.assertEqual(draft["counts"]["error_lines"], 1)

    def test_discard_rolls_back_partial_publish(self) -> None:
        baseline = sorted((s["code"], s["start_m"], s["end_m"]) for s in clipping._state["network"])
        clipping.import_file("s.csv", sample_csv([("S205", "K1+200", "K2+200")]))
        clipping.check_all()
        clipping.publish(fail_after=1)
        clipping.discard_draft()
        after = sorted((s["code"], s["start_m"], s["end_m"]) for s in clipping._state["network"])
        self.assertEqual(baseline, after)
        # 中断草稿已撤出
        with self.assertRaises(clipping.ClippingError):
            clipping.discard_draft()

    def test_correct_stake_reclips(self) -> None:
        clipping.import_file(
            "s.csv", sample_csv([("G104", "K1+200", "K2+900")])
        )
        candidate = clipping.state()["draft"]["candidates"][0]
        # 起桩号纠正为 2500，与既有路段端点相接：冲突消失，只剩可通行线段
        clipping.correct_candidate(candidate["id"], "K2+500", "K2+900")
        draft = clipping.state()["draft"]
        self.assertEqual(draft["counts"]["conflict"], 0)
        self.assertEqual(draft["pieces"][0]["stake_start"], "K2+500")

    def test_invalid_route_and_overlong_rejected(self) -> None:
        with self.assertRaises(clipping.ClippingError):
            clipping.import_file("s.csv", sample_csv([("X999", "K0", "K1")]))
        # 终点超路线全长：该行进解析错误，另带一行有效线段保证批次仍建立
        rows = [("S205", "K1+300", "K1+800"), ("G104", "K2+000", "K9+999")]
        draft = clipping.import_file("s.csv", sample_csv(rows))["batch"]
        self.assertEqual(draft["counts"]["error_lines"], 1)


if __name__ == "__main__":
    unittest.main()
