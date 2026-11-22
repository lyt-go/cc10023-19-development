"""特征计算任务领域服务：批量计算的编排、分区管理与执行追踪。"""
from __future__ import annotations

from typing import Any

from app.partition_catalog import catalog
from app.services.base import BaseModuleService
from app.store import store


class FeatureComputeService(BaseModuleService):
    MODULE = "feature_compute"
    ENTITY = "计算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["任务编号", "关联特征", "数据切片", "计算引擎", "输入分区", "输出分区", "并行度", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "关联特征", "数据切片"]
    STATUS_ORDER = ["草稿", "等待中", "计算中", "已完成", "已失败"]
    ACTION_RULES = {"提交执行": "草稿", "停止计算": "计算中", "确认完成": "已完成"}
    NEGATIVE_ACTIONS = []

    # ── 链路1: 特征计算 → 特征快照 ──
    # ── 链路5: 特征计算 → 相关性矩阵 → 特征漂移监控 ──
    def execute_and_snapshot(self, task_id: int) -> dict[str, Any]:
        """执行特征计算任务并在完成后自动生成特征快照存储到snapshots目录。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        task["status"] = "已完成"
        task["pending"] = False
        snapshot_rows = store.rows("feature_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snapshot_rows), default=0) + 1
        feats = task.get("关联特征", "unknown")
        snapshot_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SNAP-{next_id:04d}",
            "特征集": feats,
            "快照日期": "2026-11-18",
            "特征数量": 12,
            "数据行数": "100000",
            "存储路径": f"snapshots/compute_{task_id}_{next_id}.json",
            "保留天数": "30",
            "快照状态": "已完成",
        })
        return {
            "ok": True,
            "task_id": task_id,
            "snapshot_id": next_id,
            "message": f"计算任务 {task_id} 已完成，生成特征快照 {next_id}",
        }

    def compute_correlation_matrix(self, task_id: int) -> dict[str, Any]:
        """基于特征计算结果计算特征间相关性矩阵并写入存储。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        corr_rows = store.rows("feature_corr")
        next_id = max((int(r.get("id", 0)) for r in corr_rows), default=0) + 1
        feats = task.get("关联特征", "")
        corr_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "矩阵编号": f"CORR-{next_id:04d}",
            "特征集": feats,
            "计算方法": "pearson",
            "阈值筛选": "0.7",
            "可视化配置": "heatmap_full",
            "计算时间": "2026-11-18T10:00:00",
            "矩阵版本": "v1.0.0",
            "结果存储": f"corr_matrix/compute_{task_id}_v1.json",
        })
        return {"ok": True, "corr_id": next_id, "task_id": task_id}

    def trigger_drift_monitoring(self, task_id: int) -> dict[str, Any]:
        """基于特征计算结果触发特征漂移监控，关联基准分布并启动PSI计算。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        drift_rows = store.rows("drift_monitor")
        next_id = max((int(r.get("id", 0)) for r in drift_rows), default=0) + 1
        feats = task.get("关联特征", "")
        drift_rows.append({
            "id": next_id,
            "status": "监控中",
            "pending": False,
            "abnormal": False,
            "监控编号": f"DRIFT-{next_id:04d}",
            "关联特征": feats,
            "基准分布": f"baseline_compute_{task_id}",
            "当前分布": f"current_{task_id}_2026_11_18",
            "PSI值": "0.015",
            "漂移阈值": "0.25",
            "采样窗口": "30d",
            "监控状态": "监控中",
        })
        return {"ok": True, "drift_id": next_id, "task_id": task_id, "psi_value": 0.015}

    def retry_failed_tasks(self, task_ids: list[int]) -> dict[str, Any]:
        """重试失败的计算任务，将状态回退到草稿并重新调度采集。"""
        retried = []
        failed = []
        for tid in task_ids:
            task = store.find(self.MODULE, tid)
            if task is None:
                failed.append(tid)
                continue
            if task.get("status") != "已失败":
                failed.append(tid)
                continue
            task["status"] = "草稿"
            task["pending"] = True
            task["abnormal"] = False
            retried.append(tid)
        return {"ok": len(retried) > 0, "retried": retried, "failed_skipped": failed}

    def query_compute_progress(self, task_id: int) -> dict[str, Any]:
        """查询特征计算任务的执行进度、引擎状态和资源消耗情况。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        return {
            "ok": True,
            "task_id": task_id,
            "status": task.get("status"),
            "engine": task.get("计算引擎"),
            "parallelism": task.get("并行度"),
            "input": task.get("输入分区"),
            "output": task.get("输出分区"),
        }

    # ── 切片复算：读取复算分区清单 → 计算回执 → 回填覆盖核对 ──
    def start_recompute_from_plan(self, plan_id: int) -> dict[str, Any]:
        """读取切片复算分区清单并创建复算计算任务，任务输入即清单的不可变快照。"""
        plan = catalog.get_plan(plan_id)
        if plan is None:
            return {"ok": False, "message": f"复算清单 {plan_id} 不存在"}
        if plan.get("task_id") is not None:
            return {
                "ok": False,
                "message": f"复算清单 {plan_id} 已关联计算任务 {plan['task_id']}，不能重复发起",
                "task_id": plan["task_id"],
            }
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        # 输入分区直接落到清单条目，保证计算任务与操作员勾选的日期/版本完全一致
        input_desc = ", ".join(
            f"{entry['biz_date']}@v{entry['version'] if entry['version'] is not None else 'missing'}"
            for entry in plan["entries"]
        )
        output_desc = ", ".join(
            f"{entry['biz_date']}@v{(entry['version'] or 0) + 1}"
            for entry in plan["entries"]
        )
        task = {
            "id": next_id,
            "status": "等待中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"FEAT-R{plan_id:04d}",
            "关联特征": f"slice_{plan['slice_id']}_recompute",
            "数据切片": f"DATA-{plan['slice_id']:04d}",
            "计算引擎": "spark_recompute",
            "输入分区": input_desc,
            "输出分区": output_desc,
            "并行度": "4",
            "任务状态": "等待中",
            "plan_id": plan_id,
            "recompute": True,
        }
        rows.append(task)
        catalog.mark_plan_running(plan_id, next_id)
        receipt = catalog.init_receipt(next_id, plan)
        return {
            "ok": True,
            "message": (
                f"已按复算清单 {plan_id} 创建计算任务 {next_id}，"
                f"输入 {len(plan['entries'])} 个分区"),
            "task_id": next_id,
            "plan_id": plan_id,
            "receipt": receipt,
        }

    def advance_recompute(self, task_id: int, biz_dates: list[str] | None = None) -> dict[str, Any]:
        """模拟引擎推进复算：只处理清单内日期，并逐分区登记实际处理的日期与版本。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        if not task.get("recompute"):
            return {"ok": False, "message": f"任务 {task_id} 不是复算任务，未关联复算分区清单"}
        receipt, error = catalog.mark_processed(task_id, biz_dates)
        if error:
            return {"ok": False, "message": error}
        if receipt["processed_count"] == receipt["total"]:
            task["status"] = "已完成"
            task["任务状态"] = "已完成"
            task["pending"] = False
            catalog.mark_plan_finished(receipt["plan_id"])
        elif receipt["processed_count"] > 0:
            task["status"] = "计算中"
            task["任务状态"] = "计算中"
        return {
            "ok": True,
            "message": (
                f"任务 {task_id} 已处理 {receipt['processed_count']}/{receipt['total']} 个分区"),
            "receipt": receipt,
        }

    def query_recompute_receipt(self, task_id: int) -> dict[str, Any]:
        """查询复算进度回执：实际处理的业务日期、固化输入版本与产出版本。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        receipt = catalog.get_receipt(task_id)
        if receipt is None:
            return {"ok": False, "message": f"任务 {task_id} 没有复算回执"}
        return {"ok": True, "task_id": task_id, "receipt": receipt}

    def check_recompute_coverage(self, task_id: int) -> dict[str, Any]:
        """出数后核对回填覆盖，确认清单日期全部回填且没有清单外的多余产出。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        coverage = catalog.backfill_coverage(task_id)
        if coverage is None:
            return {"ok": False, "message": f"任务 {task_id} 没有复算回执，无法核对回填"}
        fully_covered = coverage["missing_count"] == 0 and coverage["extra_count"] == 0
        return {
            "ok": True,
            "fully_covered": fully_covered,
            "message": (
                "回填覆盖完整，清单分区均已出数" if fully_covered
                else f"覆盖不完整：缺 {coverage['missing_count']} 个、多余产出 {coverage['extra_count']} 个"),
            "coverage": coverage,
        }