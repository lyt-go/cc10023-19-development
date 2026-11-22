"""切片复算专用接口：分区检索、版本快照、冲突核对、清单提交与计算回执。

路由必须在通用资源路由 resource 之前注册：本路由全部是多段路径，
而通用路由 /{module}/{entry_id} 的 entry_id 声明为 int，注册顺序显式化后
语义更清晰，也避免后续扩展时被动态路径拦截。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api", tags=["切片复算"])


class FreezeSnapshotsPayload(BaseModel):
    search_id: int
    biz_dates: list[str] = Field(default_factory=list)


class PlanPayload(BaseModel):
    snapshot_ids: list[int] = Field(default_factory=list)
    operator: str | None = None


class RevisionPayload(BaseModel):
    biz_date: str
    kind: str = "revision"  # revision=已有分区修订；backfill=缺失分区补报


class StartRecomputePayload(BaseModel):
    plan_id: int


class AdvancePayload(BaseModel):
    biz_dates: list[str] | None = None


def _slice_service():
    return service_for("data_slice")


def _compute_service():
    return service_for("feature_compute")


# ── 数据切片：可复算分区检索与清单管理 ──
@router.get("/data_slice/{slice_id}/recompute/partitions")
def search_recompute_partitions(
    slice_id: int,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=5, ge=1, le=100),
    missing_only: bool = Query(default=True),
) -> dict[str, Any]:
    """按日期窗口分页检索切片的可复算（缺数）分区，返回稳定 search_id。"""
    return _slice_service().search_recompute_partitions(
        slice_id, start_date, end_date,
        page=page, size=size, missing_only=missing_only)


@router.get("/data_slice/{slice_id}/recompute/partitions/{biz_date}")
def partition_version_chain(slice_id: int, biz_date: str) -> dict[str, Any]:
    """查看单个日期的分区版本链。"""
    return _slice_service().query_partition_detail(slice_id, biz_date)


@router.post("/data_slice/{slice_id}/recompute/snapshots")
def freeze_snapshots(slice_id: int, payload: FreezeSnapshotsPayload) -> dict[str, Any]:
    """固化本次搜索勾选日期当时见到的分区版本，翻页与迟到修订都不改写快照。"""
    return _slice_service().freeze_partition_versions(
        slice_id, payload.search_id, payload.biz_dates)


@router.post("/data_slice/recompute/check-snapshots")
def check_snapshots(payload: PlanPayload) -> dict[str, Any]:
    """核对已勾选快照在迟到修订/补报后是否仍可用于本轮回填。"""
    return _slice_service().check_selection_availability(payload.snapshot_ids)


@router.post("/data_slice/{slice_id}/recompute/revisions")
def register_late_revision(slice_id: int, payload: RevisionPayload) -> dict[str, Any]:
    """登记迟到修订或缺失补报，模拟旧分区更正数据到达。"""
    return _slice_service().register_late_revision(
        slice_id, payload.biz_date, kind=payload.kind)


@router.get("/data_slice/{slice_id}/recompute/plans")
def list_recompute_plans(slice_id: int) -> dict[str, Any]:
    """查询切片下的复算分区清单。"""
    return _slice_service().list_recompute_plans(slice_id)


@router.post("/data_slice/{slice_id}/recompute/plans")
def submit_recompute_plan(slice_id: int, payload: PlanPayload) -> dict[str, Any]:
    """提交复算清单：按日期去重、拒绝未勾选分区、拦截失效快照。"""
    return _slice_service().submit_recompute_plan(
        slice_id, payload.snapshot_ids, operator=payload.operator)


# ── 特征计算任务：读取清单发起复算、回执与回填核对 ──
@router.post("/feature_compute/recompute/tasks")
def start_recompute_task(payload: StartRecomputePayload) -> dict[str, Any]:
    """特征计算任务读取切片复算分区清单并发起复算。"""
    return _compute_service().start_recompute_from_plan(payload.plan_id)


@router.post("/feature_compute/recompute/tasks/{task_id}/advance")
def advance_recompute_task(task_id: int, payload: AdvancePayload) -> dict[str, Any]:
    """推进复算处理（模拟引擎），回执记录实际处理的日期与版本。"""
    return _compute_service().advance_recompute(task_id, payload.biz_dates)


@router.get("/feature_compute/recompute/tasks/{task_id}/receipt")
def recompute_receipt(task_id: int) -> dict[str, Any]:
    """查询复算进度回执：实际处理的业务日期、输入版本、产出版本。"""
    return _compute_service().query_recompute_receipt(task_id)


@router.get("/feature_compute/recompute/tasks/{task_id}/coverage")
def recompute_coverage(task_id: int) -> dict[str, Any]:
    """出数后核对回填覆盖：清单日期是否全部出数、是否存在清单外产出。"""
    return _compute_service().check_recompute_coverage(task_id)
