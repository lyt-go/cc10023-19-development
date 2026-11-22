"""切片复算分区清单端到端测试。

覆盖业务场景：
1. 按过去一周检索缺数范围，跨页勾选并固化当时见到的分区版本；
2. 浏览过程中旧分区收到迟到修订/补报，未提交选择能核对是否仍可回填；
3. 分两页勾选三片，插入迟到修订后提交复算，守卫拒绝重复日期与未勾选分区；
4. 特征计算任务读取复算清单，回执可查实际处理的日期/版本，出数后核对回填覆盖。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app
from app.partition_catalog import catalog
from app.services import service_for

SLICE_ID = 1
WINDOW_START = "2026-11-16"   # 以 2026-11-22 为今天的过去一周
WINDOW_END = "2026-11-22"
# 种子数据中切片 1 在该窗口内的缺数日期：11-18、11-20、11-22
MISSING_DATES = ["2026-11-18", "2026-11-20", "2026-11-22"]


def assert_ok(result, msg=""):
    assert isinstance(result, dict) and result.get("ok"), f"{msg}: {result}"
    return result


# ── 场景1：分页检索缺数分区，同一次搜索翻页复用 search_id ──
def test_search_missing_partitions_paginated():
    catalog.reset()
    svc_slice = service_for("data_slice")
    page1 = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=1, size=2, missing_only=True),
        "第一页检索")
    page2 = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=2, size=2, missing_only=True),
        "第二页检索")
    assert page1["total"] == 3, f"窗口内应有 3 个缺数分区: {page1}"
    assert [item["biz_date"] for item in page1["items"]] == MISSING_DATES[:2]
    assert [item["biz_date"] for item in page2["items"]] == MISSING_DATES[2:]
    # 同条件翻页必须归属同一次搜索，快照才能追溯回当时的检索上下文
    assert page1["search_id"] == page2["search_id"]
    assert all(item["missing"] and item["recomputable"] for item in page1["items"])
    assert page1["items"][0]["current_version"] is None


# ── 场景2：快照固化当时版本，迟到修订后可核对选择是否仍可用 ──
def test_snapshot_pins_version_and_detects_conflict():
    catalog.reset()
    svc_slice = service_for("data_slice")
    # 2.1 缺数分区：固化时版本为 None，补报后旧选择应判冲突
    search = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=1, size=5),
        "缺数检索")
    frozen = assert_ok(
        svc_slice.freeze_partition_versions(
            SLICE_ID, search["search_id"], ["2026-11-18"]),
        "固化缺数分区快照")
    missing_snapshot = frozen["snapshots"][0]
    assert missing_snapshot["version"] is None
    before = assert_ok(
        svc_slice.check_selection_availability([missing_snapshot["snapshot_id"]]),
        "补报前可用性核对")
    assert before["available"] is True

    assert_ok(
        svc_slice.register_late_revision(
            SLICE_ID, "2026-11-18", kind="backfill"),
        "缺失分区迟到补报")
    after = assert_ok(
        svc_slice.check_selection_availability([missing_snapshot["snapshot_id"]]),
        "补报后可用性核对")
    assert after["available"] is False
    conflict = after["checks"][0]
    assert conflict["conflict"] is True
    assert conflict["frozen_version"] is None and conflict["current_version"] == 1
    assert "补报" in conflict["reason"]

    # 2.2 已有分区：固化 v1，修订到 v2 后旧快照保持 v1 且判冲突
    search_all = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=1, size=20, missing_only=False),
        "全量分区检索")
    target = next(item for item in search_all["items"] if item["biz_date"] == "2026-11-16")
    assert target["current_version"] == 1
    frozen_v1 = assert_ok(
        svc_slice.freeze_partition_versions(
            SLICE_ID, search_all["search_id"], ["2026-11-16"]),
        "固化 v1 快照")
    snapshot_v1 = frozen_v1["snapshots"][0]
    assert_ok(
        svc_slice.register_late_revision(
            SLICE_ID, "2026-11-16", kind="revision"),
        "迟到修订到 v2")
    # 快照本身不被修订改写，仍定位当时见到的版本
    assert catalog.get_snapshot(snapshot_v1["snapshot_id"])["version"] == 1
    check = assert_ok(
        svc_slice.check_selection_availability([snapshot_v1["snapshot_id"]]),
        "修订后可用性核对")
    assert check["available"] is False
    assert check["checks"][0]["current_version"] == 2
    # 版本链里旧版本置为已更正
    detail = assert_ok(svc_slice.query_partition_detail(SLICE_ID, "2026-11-16"), "版本链查询")
    assert [p["version"] for p in detail["version_chain"]] == [1, 2]
    assert detail["version_chain"][0]["state"] == "已更正"
    assert detail["current_version"] == 2


# ── 场景3：分两页勾选三片，插入迟到修订后提交，守卫去重/防夹带 ──
def test_two_page_selection_plan_with_guards():
    catalog.reset()
    svc_slice = service_for("data_slice")
    search = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=1, size=2),
        "第一页")
    search_id = search["search_id"]
    freeze_p1 = assert_ok(
        svc_slice.freeze_partition_versions(
            SLICE_ID, search_id, ["2026-11-18", "2026-11-20"]),
        "第一页勾选两片")
    # 翻到第二页再勾一个日期，仍使用同一次搜索的 search_id
    page2 = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=2, size=2),
        "第二页")
    assert page2["search_id"] == search_id
    freeze_p2 = assert_ok(
        svc_slice.freeze_partition_versions(
            SLICE_ID, search_id, ["2026-11-22"]),
        "第二页勾选一片")

    # 3.1 同一日期重复快照：不能多算同一日期
    dup_freeze = assert_ok(
        svc_slice.freeze_partition_versions(
            SLICE_ID, search_id, ["2026-11-22"]),
        "再次固化同一日期")
    dup_ids = [freeze_p2["snapshots"][0]["snapshot_id"],
               dup_freeze["snapshots"][0]["snapshot_id"]]
    dup_plan = svc_slice.submit_recompute_plan(SLICE_ID, dup_ids)
    assert dup_plan["ok"] is False and "同一日期" in dup_plan["message"]

    # 3.2 重复提交同一个快照 id 也被拦截
    sid = freeze_p1["snapshots"][0]["snapshot_id"]
    repeated = svc_slice.submit_recompute_plan(SLICE_ID, [sid, sid])
    assert repeated["ok"] is False and "重复" in repeated["message"]

    # 3.3 跨切片快照不能夹带进来
    slice2_search = assert_ok(
        service_for("data_slice").search_recompute_partitions(
            2, WINDOW_START, WINDOW_END, page=1, size=5),
        "切片2检索")
    slice2_snapshot = assert_ok(
        service_for("data_slice").freeze_partition_versions(
            2, slice2_search["search_id"], ["2026-11-22"]),
        "切片2快照")
    foreign_sid = slice2_snapshot["snapshots"][0]["snapshot_id"]
    cross = svc_slice.submit_recompute_plan(SLICE_ID, [sid, foreign_sid])
    assert cross["ok"] is False and "归属切片" in cross["message"]

    # 3.4 插入迟到补报命中已选日期（11-20 仍缺失），提交必须被冲突拦截
    assert_ok(
        svc_slice.register_late_revision(SLICE_ID, "2026-11-20", kind="backfill"),
        "对已选缺数分区补报")
    stale_ids = [s["snapshot_id"] for s in freeze_p1["snapshots"]] + \
        [freeze_p2["snapshots"][0]["snapshot_id"]]
    rejected = svc_slice.submit_recompute_plan(SLICE_ID, stale_ids)
    assert rejected["ok"] is False
    assert rejected.get("conflicts"), "冲突明细应回传给前端提示"
    assert any(c["biz_date"] == "2026-11-20" for c in rejected["conflicts"])

    # 3.5 重新检索并重新固化 11-20，三片选择全部可用后提交成功
    refreshed = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=1, size=5, missing_only=False),
        "补报后重新检索")
    refreeze = assert_ok(
        svc_slice.freeze_partition_versions(
            SLICE_ID, refreshed["search_id"], ["2026-11-20"]),
        "重新固化 11-20")
    valid_ids = [
        freeze_p1["snapshots"][0]["snapshot_id"],   # 11-18 仍缺失，快照仍有效
        refreeze["snapshots"][0]["snapshot_id"],    # 11-20 补报后的 v1
        freeze_p2["snapshots"][0]["snapshot_id"],   # 11-22 仍缺失
    ]
    availability = assert_ok(
        svc_slice.check_selection_availability(valid_ids), "提交前可用性核对")
    assert availability["available"] is True
    submitted = assert_ok(svc_slice.submit_recompute_plan(SLICE_ID, valid_ids), "提交复算清单")
    plan = submitted["plan"]
    assert len(plan["entries"]) == 3
    dates = [entry["biz_date"] for entry in plan["entries"]]
    assert dates == ["2026-11-18", "2026-11-20", "2026-11-22"]
    assert len(set(dates)) == 3, "同一日期不能在清单里出现两次"
    versions = {entry["biz_date"]: entry["version"] for entry in plan["entries"]}
    assert versions["2026-11-18"] is None
    assert versions["2026-11-20"] == 1
    assert versions["2026-11-22"] is None
    return plan["plan_id"]


# ── 场景4：计算任务读取清单，回执可查日期/版本，出数后核对回填覆盖 ──
def test_compute_task_receipt_and_coverage(plan_id: int):
    svc_compute = service_for("feature_compute")
    started = assert_ok(
        svc_compute.start_recompute_from_plan(plan_id), "按清单发起复算")
    task_id = started["task_id"]
    assert len(started["receipt"]["partitions"]) == 3

    # 同一清单不能重复发起任务
    again = svc_compute.start_recompute_from_plan(plan_id)
    assert again["ok"] is False

    # 输入分区必须与清单逐条对应，且不能出现清单外日期
    from app.store import store
    task = store.find("feature_compute", task_id)
    input_desc = task["输入分区"]
    for biz in ["2026-11-18", "2026-11-20", "2026-11-22"]:
        assert biz in input_desc, f"输入分区缺少 {biz}"
    assert "2026-11-16" not in input_desc, "未勾选分区不得进入计算输入"

    # 引擎只能处理清单内日期，清单外日期直接拒绝
    guarded = svc_compute.advance_recompute(task_id, ["2026-11-16"])
    assert guarded["ok"] is False and "不在复算清单" in guarded["message"]

    # 部分推进：回执能查到实际处理的日期和输入/产出版本
    partial = assert_ok(
        svc_compute.advance_recompute(task_id, ["2026-11-18"]), "处理首个分区")
    receipt = partial["receipt"]
    assert receipt["processed_count"] == 1 and receipt["total"] == 3
    processed = next(p for p in receipt["partitions"] if p["biz_date"] == "2026-11-18")
    assert processed["processed"] is True
    assert processed["input_version"] is None
    assert processed["output_version"] == 1
    assert processed["output_path"].endswith("dt=2026-11-18/v1")

    # 全部处理完，任务与清单都转到已完成
    finished = assert_ok(svc_compute.advance_recompute(task_id), "处理剩余分区")
    assert finished["receipt"]["status"] == "已完成"
    assert store.find("feature_compute", task_id)["status"] == "已完成"
    assert catalog.get_plan(plan_id)["status"] == "已完成"
    v20 = next(p for p in finished["receipt"]["partitions"] if p["biz_date"] == "2026-11-20")
    assert v20["input_version"] == 1 and v20["output_version"] == 2

    # 出数后回填覆盖核对：3 个清单日期全部出数，无缺失、无清单外产出
    coverage = assert_ok(svc_compute.check_recompute_coverage(task_id), "回填覆盖核对")
    assert coverage["fully_covered"] is True
    assert coverage["coverage"]["total"] == 3
    assert coverage["coverage"]["covered_count"] == 3
    assert coverage["coverage"]["missing_count"] == 0
    assert coverage["coverage"]["extra_count"] == 0
    assert coverage["coverage"]["coverage_rate"] == 1.0
    covered_dates = [item["biz_date"] for item in coverage["coverage"]["covered"]]
    assert covered_dates == ["2026-11-18", "2026-11-20", "2026-11-22"]


# ── 路由注册：复算专用路径必须先于通用 /{module}/{entry_id} 注册 ──
def test_recompute_routes_registered_before_generic():
    from app.routers import ROUTERS, recompute as recompute_module, resource as resource_module
    assert ROUTERS[0] is recompute_module and ROUTERS[1] is resource_module

    paths: list[str] = []
    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            paths.extend(getattr(r, "path", "") for r in original.routes)
        else:
            paths.append(getattr(route, "path", ""))
    assert "/api/data_slice/{slice_id}/recompute/partitions" in paths
    assert "/api/data_slice/{slice_id}/recompute/plans" in paths
    assert "/api/feature_compute/recompute/tasks/{task_id}/receipt" in paths
    assert "/api/feature_compute/recompute/tasks/{task_id}/coverage" in paths
    assert paths.index("/api/data_slice/{slice_id}/recompute/partitions") < paths.index(
        "/api/{module}/{entry_id}")


# ── 输入校验：非法日期、越窗选择、未知切片等都返回业务错误而非异常 ──
def test_input_validation_guards():
    catalog.reset()
    svc_slice = service_for("data_slice")

    # 不存在的切片
    assert svc_slice.search_recompute_partitions(999)["ok"] is False
    assert svc_slice.submit_recompute_plan(999, [1])["ok"] is False

    # 先做一次合法检索拿到 search_id，再验证各路径的日期/参数守卫
    search = assert_ok(
        svc_slice.search_recompute_partitions(
            SLICE_ID, WINDOW_START, WINDOW_END, page=1, size=5),
        "合法检索")
    search_id = search["search_id"]

    # 非法日期格式
    bad_search = svc_slice.search_recompute_partitions(
        SLICE_ID, "2026/11/16", WINDOW_END)
    assert bad_search["ok"] is False and "YYYY-MM-DD" in bad_search["message"]
    bad_freeze = svc_slice.freeze_partition_versions(SLICE_ID, search_id, ["11-18"])
    assert bad_freeze["ok"] is False and "YYYY-MM-DD" in bad_freeze["message"]
    bad_revision = svc_slice.register_late_revision(SLICE_ID, "bad-date", kind="backfill")
    assert bad_revision["ok"] is False and "YYYY-MM-DD" in bad_revision["message"]

    # 未知检索记录与空选择
    assert svc_slice.freeze_partition_versions(SLICE_ID, 9999, ["2026-11-18"])["ok"] is False
    assert svc_slice.freeze_partition_versions(SLICE_ID, search_id, [])["ok"] is False
    assert svc_slice.check_selection_availability([])["ok"] is False
    assert svc_slice.submit_recompute_plan(SLICE_ID, [])["ok"] is False

    # 日期超出检索窗口
    out_of_range = svc_slice.freeze_partition_versions(
        SLICE_ID, search_id, ["2026-11-01"])
    assert out_of_range["ok"] is False and "超出本次检索窗口" in out_of_range["message"]

    # 修订类型与分区现状不匹配
    assert svc_slice.register_late_revision(
        SLICE_ID, "2026-11-18", kind="revision")["ok"] is False
    assert svc_slice.register_late_revision(
        SLICE_ID, "2026-11-16", kind="backfill")["ok"] is False

    # 不存在的清单不能发起复算
    assert service_for("feature_compute").start_recompute_from_plan(9999)["ok"] is False


if __name__ == "__main__":
    passed = failed = 0
    # 场景3/4 共享同一份复算清单，必须连续执行；其余测试各自 reset，顺序无关
    ordered: list[tuple[str, object]] = [
        ("test_search_missing_partitions_paginated", test_search_missing_partitions_paginated),
        ("test_snapshot_pins_version_and_detects_conflict", test_snapshot_pins_version_and_detects_conflict),
        ("test_two_page_selection_plan_with_guards", test_two_page_selection_plan_with_guards),
    ]
    plan_id = None
    for name, test in ordered:
        try:
            result = test()
            if isinstance(result, int):
                plan_id = result
            print(f"[PASS] {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name}: {exc}")
            failed += 1

    tail_tests: list[tuple[str, object]] = [
        ("test_compute_task_receipt_and_coverage",
         lambda: test_compute_task_receipt_and_coverage(plan_id)),
        ("test_input_validation_guards", test_input_validation_guards),
        ("test_recompute_routes_registered_before_generic",
         test_recompute_routes_registered_before_generic),
    ]
    for name, test in tail_tests:
        try:
            test()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name}: {exc}")
            failed += 1
    print(f"\n复算链路测试: {passed} 通过, {failed} 失败")
    if failed:
        sys.exit(1)
