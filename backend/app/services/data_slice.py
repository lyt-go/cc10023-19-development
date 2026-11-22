"""数据切片配置领域服务：定义分析数据集的筛选逻辑、时间窗口与版本管理。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.partition_catalog import STATE_MISSING, catalog, parse_biz_date
from app.services.base import BaseModuleService
from app.store import store


class DataSliceService(BaseModuleService):
    MODULE = "data_slice"
    ENTITY = "切片配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["切片编号", "切片名称", "数据来源", "筛选条件", "时间窗口", "切片大小", "业务含义", "切片版本"]
    REQUIRED_FIELDS = ["切片编号", "切片名称", "数据来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已过期"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    # ── 链路2: 数据切片 → 特征筛选 → 特征重要性分析 ──
    def execute_slice_and_push_to_selection(self, slice_id: int) -> dict[str, Any]:
        """将数据切片配置关联到特征筛选策略，触发筛选流程的数据准备阶段。"""
        sl = store.find(self.MODULE, slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        sl["status"] = "已生效"
        sl["pending"] = False
        selection_rows = store.rows("feature_selection")
        next_id = max((int(r.get("id", 0)) for r in selection_rows), default=0) + 1
        selection_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "策略编号": f"SEL-{next_id:04d}",
            "特征集": f"slice_{slice_id}_features",
            "筛选方法": "variance_corr_combined",
            "保留数量": 20,
            "相关性阈值": "0.85",
            "方差阈值": "0.01",
            "信息增益": "0.15",
            "策略版本": "v1.0.0",
        })
        return {
            "ok": True,
            "slice_id": slice_id,
            "selection_id": next_id,
            "message": f"切片 {slice_id} 已生效，并自动创建筛选策略 {next_id}",
        }

    def validate_slice_window(self, slice_id: int) -> dict[str, Any]:
        """校验数据切片的时间窗口有效性，确保不超出数据源的历史分区范围。"""
        sl = store.find(self.MODULE, slice_id)
        if sl is None:
            return {"ok": False, "message": "切片不存在"}
        time_window = sl.get("时间窗口", "")
        if not time_window or time_window == "未设置":
            sl["abnormal"] = True
            return {"ok": False, "message": f"切片 {slice_id} 缺少时间窗口配置", "slice_id": slice_id}
        sl["abnormal"] = False
        return {"ok": True, "slice_id": slice_id, "time_window": time_window}

    def standardize_slice_condition(self, slice_id: int) -> dict[str, Any]:
        """标准化切片筛选条件为结构化表达式，便于跨模块查询和复用。"""
        sl = store.find(self.MODULE, slice_id)
        if sl is None:
            return {"ok": False}
        raw = sl.get("筛选条件", "")
        parsed = {
            "raw": raw,
            "operators": ["eq", "gt", "in"] if raw else [],
            "field_count": len(raw.split(",")) if raw else 0,
        }
        return {"ok": True, "slice_id": slice_id, "standardized_condition": parsed}

    def query_slice_statistics(self, slice_id: int) -> dict[str, Any]:
        """查询数据切片的统计信息，包括行数、列数和数据分布概览。"""
        sl = store.find(self.MODULE, slice_id)
        if sl is None:
            return {"ok": False}
        size_str = sl.get("切片大小", "0")
        try:
            row_count = int(float(str(size_str))) if size_str else 0
        except ValueError:
            row_count = 0
        return {
            "ok": True,
            "slice_id": slice_id,
            "slice_name": sl.get("切片名称"),
            "row_count": row_count,
            "source": sl.get("数据来源"),
            "business_meaning": sl.get("业务含义"),
        }

    # ── 切片复算分区清单：按日期检索缺数分区 → 固化版本 → 提交复算 ──
    def _require_slice(self, slice_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, slice_id)

    @staticmethod
    def _default_window() -> tuple[str, str]:
        """默认检索过去一周（含今天）的缺数范围。"""
        today = date(2026, 11, 22)
        return (today - timedelta(days=6)).isoformat(), today.isoformat()

    def search_recompute_partitions(
        self,
        slice_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        page: int = 1,
        size: int = 5,
        missing_only: bool = True,
    ) -> dict[str, Any]:
        """从切片时间分区数据中查询可复算分区，分页返回并给出稳定 search_id。"""
        sl = self._require_slice(slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        if not start_date or not end_date:
            start_date, end_date = self._default_window()
        try:
            result = catalog.search_partitions(
                slice_id, start_date, end_date,
                page=page, size=size, missing_only=missing_only)
        except ValueError as exc:
            return {"ok": False, "message": f"日期格式非法：{exc}，应为 YYYY-MM-DD"}
        missing_count = sum(1 for item in result["items"] if item["missing"])
        result.update({
            "ok": True,
            "message": f"检索到 {result['total']} 个可复算分区，其中本页缺数分区 {missing_count} 个",
            "slice_name": sl.get("切片名称"),
        })
        return result

    def freeze_partition_versions(
        self, slice_id: int, search_id: int, biz_dates: list[str],
    ) -> dict[str, Any]:
        """把本次搜索勾选的日期固化成分区版本快照，支撑翻页后仍定位当时版本。"""
        sl = self._require_slice(slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        try:
            snapshots, error = catalog.materialize_snapshots(search_id, biz_dates)
        except ValueError as exc:
            return {"ok": False, "message": f"日期格式非法：{exc}，应为 YYYY-MM-DD"}
        if error:
            return {"ok": False, "message": error}
        return {
            "ok": True,
            "message": f"已固化 {len(snapshots)} 个分区的版本快照",
            "search_id": search_id,
            "snapshots": snapshots,
        }

    def check_selection_availability(self, snapshot_ids: list[int]) -> dict[str, Any]:
        """提交前（或迟到修订发生后）核对现有选择是否还能用于本轮回填。"""
        if not snapshot_ids:
            return {"ok": False, "message": "缺少待核对的快照编号"}
        checks = catalog.check_snapshots(snapshot_ids)
        conflicts = [item for item in checks if item["conflict"]]
        return {
            "ok": True,
            "available": not conflicts,
            "message": "全部选择仍可用于本轮回填" if not conflicts
            else f"{len(conflicts)} 个选择已失效，需重新检索选择",
            "checks": checks,
        }

    def submit_recompute_plan(
        self,
        slice_id: int,
        snapshot_ids: list[int],
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """提交切片复算分区清单：只接受显式快照、按日期去重，并拦截失效选择。"""
        sl = self._require_slice(slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        plan, error, conflicts = catalog.create_plan(slice_id, snapshot_ids, operator)
        if error:
            payload = {"ok": False, "message": error}
            if conflicts:
                payload["conflicts"] = conflicts
            return payload
        return {
            "ok": True,
            "message": f"复算清单 {plan['plan_id']} 已提交，共 {len(plan['entries'])} 个分区",
            "plan": plan,
        }

    def list_recompute_plans(self, slice_id: int) -> dict[str, Any]:
        """查询切片下已形成的复算分区清单。"""
        sl = self._require_slice(slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        plans = catalog.list_plans(slice_id)
        return {"ok": True, "slice_id": slice_id, "total": len(plans), "plans": plans}

    def register_late_revision(
        self, slice_id: int, biz_date: str, *, kind: str = "revision",
    ) -> dict[str, Any]:
        """登记迟到数据（修订/补报），用于触发已勾选版本与当前目录的冲突核对。"""
        sl = self._require_slice(slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        try:
            row, error = catalog.register_revision(slice_id, biz_date, kind=kind)
        except ValueError as exc:
            return {"ok": False, "message": f"日期格式非法：{exc}，应为 YYYY-MM-DD"}
        if error:
            return {"ok": False, "message": error}
        return {
            "ok": True,
            "message": f"分区 {biz_date} 已登记迟到数据，当前版本 v{row['version']}（{row['state']}）",
            "partition": row,
        }

    def query_partition_detail(self, slice_id: int, biz_date: str) -> dict[str, Any]:
        """查看单个日期的完整分区版本链，确认修订前后的版本与状态。"""
        sl = self._require_slice(slice_id)
        if sl is None:
            return {"ok": False, "message": f"切片配置 {slice_id} 不存在"}
        try:
            normalized = parse_biz_date(biz_date).isoformat()
        except ValueError as exc:
            return {"ok": False, "message": f"日期格式非法：{exc}"}
        latest = catalog._latest_partition(slice_id, normalized)
        if latest is None:
            return {
                "ok": True,
                "slice_id": slice_id,
                "biz_date": normalized,
                "state": STATE_MISSING,
                "current_version": None,
                "version_chain": [],
            }
        return {
            "ok": True,
            "slice_id": slice_id,
            "biz_date": normalized,
            "state": latest["state"],
            "current_version": latest["version"],
            "version_chain": [
                {"version": p["version"], "state": p["state"],
                 "row_count": p["row_count"], "updated_at": p["updated_at"]}
                for p in catalog._version_chain(slice_id, normalized)
            ],
        }