"""切片时间分区目录：模拟底层分区表、迟到修订，以及复算清单与计算回执的内存存储。

真实项目里这一层对应 Hive/Iceberg 分区目录 + 任务编排库；当前实现只依赖标准库，
随进程启动用确定性种子数据初始化，保证服务起来即可演示完整复算链路。

核心概念：
- 分区版本链：同一 (slice_id, biz_date) 每次迟到修订都会追加一个新版本，旧版本置为「已更正」。
- 缺失分区：分区表中没有任何版本行，状态为「缺失」，是最典型的可复算对象。
- 搜索快照(search snapshot)：一次检索在固化时刻看到的分区版本，后续翻页/修订都不改写它。
- 复算清单(plan)：由快照条目组成的不可变输入，每个业务日期只允许出现一次。
- 进度回执(receipt)：计算任务实际处理到的日期与版本，回填产出按日期登记后供覆盖核对。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Any

# 分区生命周期状态
STATE_NORMAL = "正常"        # 最新版本可用
STATE_MISSING = "缺失"      # 分区表无数据，等待复算/补报
STATE_SUPERSEDED = "已更正"  # 旧版本已被迟到修订替代
STATE_BACKFILLED = "已补报"  # 原缺失分区迟到补报后形成的新版本


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_biz_date(value: str) -> date:
    """解析业务日期字符串，统一 YYYY-MM-DD，非法格式直接抛 ValueError。"""
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


class PartitionCatalog:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 分区版本行：{slice_id, biz_date, version, state, row_count, updated_at}
        self._partitions: list[dict[str, Any]] = []
        # 检索记录：同条件翻页复用同一个 search_id
        self._searches: list[dict[str, Any]] = []
        # 固化快照：{snapshot_id, search_id, slice_id, biz_date, version, state, created_at}
        self._snapshots: list[dict[str, Any]] = []
        # 切片复算分区清单
        self._plans: list[dict[str, Any]] = []
        # 计算任务进度回执：task_id -> receipt
        self._receipts: dict[int, dict[str, Any]] = {}
        # 回填产出登记：{task_id, slice_id, biz_date, version, output_path, created_at}
        self._backfills: list[dict[str, Any]] = []
        self._search_seq = 0
        self._snapshot_seq = 0
        self._plan_seq = 0
        self._seeded = False

    # ────────────────────────── 种子数据 ──────────────────────────
    def reset(self) -> None:
        """清空全部复算状态并重新播种，供测试与演示重置使用。"""
        with self._lock:
            self._partitions.clear()
            self._searches.clear()
            self._snapshots.clear()
            self._plans.clear()
            self._receipts.clear()
            self._backfills.clear()
            self._search_seq = 0
            self._snapshot_seq = 0
            self._plan_seq = 0
            self._seeded = False
            self.seed()

    def seed(self) -> None:
        """注入确定性的切片分区数据：10 天窗口里穿插缺失分区，方便演示分页与复算。"""
        with self._lock:
            if self._seeded:
                return
            self._seeded = True
            today = date(2026, 11, 22)
            # 切片 1：2026-11-13 ~ 2026-11-22 共 10 天，其中 4 天缺数
            missing_dates_slice1 = {
                today - timedelta(days=7),   # 11-15
                today - timedelta(days=4),   # 11-18
                today - timedelta(days=2),   # 11-20
                today,                       # 11-22
            }
            for offset in range(9, -1, -1):
                biz = today - timedelta(days=offset)
                if biz in missing_dates_slice1:
                    continue
                self._partitions.append(self._make_partition(1, biz, 1, STATE_NORMAL, 12000))
            # 切片 2：少量分区，用于验证切片之间相互隔离
            self._partitions.append(
                self._make_partition(2, today - timedelta(days=1), 1, STATE_NORMAL, 8000))
            # 11-22 在切片 2 上同样缺数

    @staticmethod
    def _make_partition(
        slice_id: int, biz_date_: date, version: int, state: str, row_count: int,
    ) -> dict[str, Any]:
        return {
            "slice_id": slice_id,
            "biz_date": biz_date_.isoformat(),
            "version": version,
            "state": state,
            "row_count": row_count,
            "updated_at": _now(),
        }

    # ────────────────────────── 分区查询 ──────────────────────────
    def _version_chain(self, slice_id: int, biz_date: str) -> list[dict[str, Any]]:
        rows = [
            p for p in self._partitions
            if p["slice_id"] == slice_id and p["biz_date"] == biz_date
        ]
        return sorted(rows, key=lambda p: p["version"])

    def _latest_partition(self, slice_id: int, biz_date: str) -> dict[str, Any] | None:
        chain = self._version_chain(slice_id, biz_date)
        return chain[-1] if chain else None

    def _enumerate_dates(self, start: date, end: date) -> list[str]:
        if start > end:
            return []
        days = (end - start).days
        return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]

    def search_partitions(
        self,
        slice_id: int,
        start_date: str,
        end_date: str,
        *,
        page: int = 1,
        size: int = 5,
        missing_only: bool = True,
    ) -> dict[str, Any]:
        """按日期窗口检索切片分区，返回可复算分区分页结果与稳定的 search_id。

        同一切片 + 同一窗口 + 同一过滤条件复用同一条检索记录，保证向后翻页时
        前端勾选动作归属「同一次搜索」，快照可追溯回当时的检索上下文。
        """
        with self._lock:
            start = parse_biz_date(start_date)
            end = parse_biz_date(end_date)
            search = self._find_or_create_search(slice_id, start, end, missing_only)
            items: list[dict[str, Any]] = []
            for biz in self._enumerate_dates(start, end):
                latest = self._latest_partition(slice_id, biz)
                if latest is None:
                    item = {
                        "biz_date": biz,
                        "state": STATE_MISSING,
                        "missing": True,
                        "recomputable": True,
                        "current_version": None,
                        "row_count": 0,
                        "updated_at": None,
                        "version_chain": [],
                    }
                else:
                    item = {
                        "biz_date": biz,
                        "state": latest["state"],
                        "missing": False,
                        # 已有分区同样可以发起复算（重算修正），缺数分区优先级最高
                        "recomputable": True,
                        "current_version": latest["version"],
                        "row_count": latest["row_count"],
                        "updated_at": latest["updated_at"],
                        "version_chain": [
                            {
                                "version": p["version"],
                                "state": p["state"],
                                "row_count": p["row_count"],
                                "updated_at": p["updated_at"],
                            }
                            for p in self._version_chain(slice_id, biz)
                        ],
                    }
                if missing_only and not item["missing"]:
                    continue
                items.append(item)
            total = len(items)
            begin = (page - 1) * size
            page_items = items[begin:begin + size]
            return {
                "items": page_items,
                "total": total,
                "page": page,
                "size": size,
                "search_id": search["search_id"],
                "slice_id": slice_id,
                "start_date": start_date,
                "end_date": end_date,
                "missing_only": missing_only,
            }

    def _find_or_create_search(
        self, slice_id: int, start: date, end: date, missing_only: bool,
    ) -> dict[str, Any]:
        for search in self._searches:
            if (
                search["slice_id"] == slice_id
                and search["start_date"] == start.isoformat()
                and search["end_date"] == end.isoformat()
                and search["missing_only"] == missing_only
            ):
                return search
        self._search_seq += 1
        search = {
            "search_id": self._search_seq,
            "slice_id": slice_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "missing_only": missing_only,
            "created_at": _now(),
        }
        self._searches.append(search)
        return search

    def get_search(self, search_id: int) -> dict[str, Any] | None:
        with self._lock:
            return next((s for s in self._searches if s["search_id"] == search_id), None)

    # ────────────────────────── 快照固化 ──────────────────────────
    def materialize_snapshots(
        self, search_id: int, biz_dates: list[str]
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """把检索中勾选的日期固化为分区版本快照，翻页或后续修订都不会改写快照。"""
        with self._lock:
            search = self.get_search(search_id)
            if search is None:
                return None, f"检索记录 {search_id} 不存在，请重新发起分区检索"
            if not biz_dates:
                return None, "至少选择一个分区日期后再固化版本"
            start = parse_biz_date(search["start_date"])
            end = parse_biz_date(search["end_date"])
            normalized: list[str] = []
            for raw in biz_dates:
                biz = parse_biz_date(raw)
                if not (start <= biz <= end):
                    return None, f"日期 {raw} 超出本次检索窗口 {search['start_date']}~{search['end_date']}"
                normalized.append(biz.isoformat())
            if len(set(normalized)) != len(normalized):
                return None, "同一日期不能重复固化，请按日期去重后再提交"
            snapshots: list[dict[str, Any]] = []
            for biz in normalized:
                latest = self._latest_partition(search["slice_id"], biz)
                self._snapshot_seq += 1
                snapshot = {
                    "snapshot_id": self._snapshot_seq,
                    "search_id": search_id,
                    "slice_id": search["slice_id"],
                    "biz_date": biz,
                    # 缺数分区没有版本号，记录 None；一旦被补报，可用性检查即报冲突
                    "version": latest["version"] if latest else None,
                    "state": latest["state"] if latest else STATE_MISSING,
                    "created_at": _now(),
                }
                self._snapshots.append(snapshot)
                snapshots.append(dict(snapshot))
            return snapshots, ""

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = next(
                (s for s in self._snapshots if s["snapshot_id"] == snapshot_id), None)
            return dict(row) if row else None

    def check_snapshots(self, snapshot_ids: list[int]) -> list[dict[str, Any]]:
        """核对固化快照相对于当前分区目录是否仍可用于本轮回填。"""
        with self._lock:
            results: list[dict[str, Any]] = []
            for snapshot_id in snapshot_ids:
                snapshot = next(
                    (s for s in self._snapshots if s["snapshot_id"] == snapshot_id), None)
                if snapshot is None:
                    results.append({
                        "snapshot_id": snapshot_id,
                        "available": False,
                        "conflict": True,
                        "biz_date": None,
                        "frozen_version": None,
                        "current_version": None,
                        "reason": "快照不存在或已被清理",
                    })
                    continue
                latest = self._latest_partition(snapshot["slice_id"], snapshot["biz_date"])
                current_version = latest["version"] if latest else None
                if snapshot["version"] is None:
                    if latest is None:
                        available, reason = True, "分区仍缺失，可按复算清单补算"
                    else:
                        available, reason = False, "原缺失分区已迟到补报，旧选择基于缺失态，需重新检索后选择"
                elif latest is None:
                    available, reason = False, "已选分区当前缺失，目录状态异常，请重新检索确认"
                elif latest["version"] == snapshot["version"]:
                    available, reason = True, "固化版本仍为当前版本，可用于回填"
                else:
                    available, reason = False, (
                        f"分区已发布修订版本 v{latest['version']}，"
                        f"固化的 v{snapshot['version']} 已过期")
                results.append({
                    "snapshot_id": snapshot_id,
                    "available": available,
                    "conflict": not available,
                    "biz_date": snapshot["biz_date"],
                    "frozen_version": snapshot["version"],
                    "frozen_state": snapshot["state"],
                    "current_version": current_version,
                    "current_state": latest["state"] if latest else STATE_MISSING,
                    "reason": reason,
                })
            return results

    # ────────────────────────── 迟到修订 ──────────────────────────
    def register_revision(
        self, slice_id: int, biz_date: str, *, kind: str = "revision", row_count: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记迟到数据：revision 给已有分区追加修订版，backfill 给缺失分区补报首版。"""
        with self._lock:
            biz = parse_biz_date(biz_date).isoformat()
            latest = self._latest_partition(slice_id, biz)
            if kind == "backfill":
                if latest is not None:
                    return None, f"分区 {biz} 已存在 v{latest['version']}，补报仅适用于缺失分区"
                row = self._make_partition(
                    slice_id, parse_biz_date(biz), 1, STATE_BACKFILLED,
                    row_count if row_count is not None else 9000)
                self._partitions.append(row)
                return dict(row), ""
            if kind != "revision":
                return None, f"未知修订类型：{kind}"
            if latest is None:
                return None, f"分区 {biz} 当前缺失，应使用 backfill 补报而非 revision 修订"
            # 旧版本整条置为已更正，再追加新版本
            for part in self._version_chain(slice_id, biz):
                part["state"] = STATE_SUPERSEDED
            new_row = self._make_partition(
                slice_id, parse_biz_date(biz), latest["version"] + 1, STATE_NORMAL,
                row_count if row_count is not None else latest["row_count"] + 500)
            self._partitions.append(new_row)
            return dict(new_row), ""

    # ────────────────────────── 复算清单 ──────────────────────────
    def create_plan(
        self, slice_id: int, snapshot_ids: list[int], operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """基于快照创建切片复算分区清单。

        守卫规则：
        - 快照必须存在且属于该切片；
        - 同一业务日期只允许一条输入（不能多算同一日期）；
        - 条目只能来自显式勾选的快照（不能夹带没选过的分区）；
        - 快照当前必须仍可用，过期选择要先重新检索。
        """
        with self._lock:
            if not snapshot_ids:
                return None, "复算清单不能为空，请先勾选分区", []
            duplicates = {
                sid for sid in snapshot_ids if snapshot_ids.count(sid) > 1
            }
            if duplicates:
                return None, f"快照 {sorted(duplicates)} 被重复提交，每个日期只能计算一次", []
            snapshots: list[dict[str, Any]] = []
            for snapshot_id in snapshot_ids:
                snapshot = next(
                    (s for s in self._snapshots if s["snapshot_id"] == snapshot_id), None)
                if snapshot is None:
                    return None, f"快照 {snapshot_id} 不存在", []
                if snapshot["slice_id"] != slice_id:
                    return None, (
                        f"快照 {snapshot_id} 归属切片 {snapshot['slice_id']}，"
                        f"不能加入切片 {slice_id} 的复算清单"), []
                snapshots.append(snapshot)
            date_seen: set[str] = set()
            for snapshot in snapshots:
                if snapshot["biz_date"] in date_seen:
                    return None, f"业务日期 {snapshot['biz_date']} 出现多个快照，同一日期不能多算", []
                date_seen.add(snapshot["biz_date"])
            checks = self.check_snapshots(snapshot_ids)
            conflicts = [c for c in checks if not c["available"]]
            if conflicts:
                return None, "存在已过期或被补报的选择，请重新检索后再提交", conflicts
            self._plan_seq += 1
            plan = {
                "plan_id": self._plan_seq,
                "slice_id": slice_id,
                "search_id": snapshots[0]["search_id"],
                "operator": operator or "数据工程组",
                "status": "待计算",
                "created_at": _now(),
                "task_id": None,
                "entries": [
                    {
                        "snapshot_id": s["snapshot_id"],
                        "biz_date": s["biz_date"],
                        "version": s["version"],
                        "state": s["state"],
                    }
                    for s in snapshots
                ],
            }
            self._plans.append(plan)
            return dict(plan), "", []

    def list_plans(self, slice_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            plans = self._plans if slice_id is None else [
                p for p in self._plans if p["slice_id"] == slice_id]
            return [dict(p) for p in plans]

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        with self._lock:
            plan = next((p for p in self._plans if p["plan_id"] == plan_id), None)
            return dict(plan) if plan else None

    def mark_plan_running(self, plan_id: int, task_id: int) -> None:
        with self._lock:
            plan = next(p for p in self._plans if p["plan_id"] == plan_id)
            plan["status"] = "计算中"
            plan["task_id"] = task_id

    def mark_plan_finished(self, plan_id: int) -> None:
        with self._lock:
            plan = next(p for p in self._plans if p["plan_id"] == plan_id)
            plan["status"] = "已完成"

    # ────────────────────────── 计算回执与回填 ──────────────────────────
    def init_receipt(self, task_id: int, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            receipt = {
                "task_id": task_id,
                "plan_id": plan["plan_id"],
                "slice_id": plan["slice_id"],
                "status": "计算中",
                "partitions": [
                    {
                        "biz_date": entry["biz_date"],
                        "input_version": entry["version"],
                        "state": "待处理",
                        "processed": False,
                        "output_version": None,
                        "output_path": None,
                        "processed_at": None,
                    }
                    for entry in plan["entries"]
                ],
                "started_at": _now(),
            }
            self._receipts[task_id] = receipt
            return dict(receipt)

    def get_receipt(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            receipt = self._receipts.get(task_id)
            if receipt is None:
                return None
            result = dict(receipt)
            result["partitions"] = [dict(p) for p in receipt["partitions"]]
            result["total"] = len(receipt["partitions"])
            result["processed_count"] = sum(
                1 for p in receipt["partitions"] if p["processed"])
            return result

    def mark_processed(
        self, task_id: int, biz_dates: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """模拟计算引擎处理分区：只允许处理清单内日期，处理后登记回填产出。"""
        with self._lock:
            receipt = self._receipts.get(task_id)
            if receipt is None:
                return None, f"任务 {task_id} 没有复算回执，可能未关联复算清单"
            planned = {p["biz_date"]: p for p in receipt["partitions"]}
            targets = list(planned) if biz_dates is None else list(biz_dates)
            unknown = [d for d in targets if d not in planned]
            if unknown:
                return None, f"日期 {unknown} 不在复算清单内，禁止处理未勾选的分区"
            for biz in targets:
                part = planned[biz]
                if part["processed"]:
                    continue
                output_version = (part["input_version"] or 0) + 1
                output_path = (
                    f"backfill/slice_{receipt['slice_id']}/dt={biz}/v{output_version}")
                part.update({
                    "state": "已处理",
                    "processed": True,
                    "output_version": output_version,
                    "output_path": output_path,
                    "processed_at": _now(),
                })
                self._backfills.append({
                    "task_id": task_id,
                    "slice_id": receipt["slice_id"],
                    "biz_date": biz,
                    "input_version": part["input_version"],
                    "version": output_version,
                    "output_path": output_path,
                    "created_at": _now(),
                })
            if all(p["processed"] for p in receipt["partitions"]):
                receipt["status"] = "已完成"
            return self.get_receipt(task_id), ""

    def backfill_coverage(self, task_id: int) -> dict[str, Any] | None:
        """出数后核对回填覆盖：计划日期是否都有产出，是否存在清单外的多余产出。"""
        with self._lock:
            receipt = self._receipts.get(task_id)
            if receipt is None:
                return None
            planned_dates = [p["biz_date"] for p in receipt["partitions"]]
            produced = [
                b for b in self._backfills
                if b["task_id"] == task_id and b["biz_date"] in planned_dates
            ]
            extra = [
                b for b in self._backfills
                if b["task_id"] == task_id and b["biz_date"] not in planned_dates
            ]
            produced_dates = {b["biz_date"] for b in produced}
            missing = [d for d in planned_dates if d not in produced_dates]
            covered = [
                {
                    "biz_date": b["biz_date"],
                    "version": b["version"],
                    "input_version": next(
                        (p["input_version"] for p in receipt["partitions"]
                         if p["biz_date"] == b["biz_date"]), None),
                    "output_path": b["output_path"],
                }
                for b in produced
            ]
            total = len(planned_dates)
            return {
                "task_id": task_id,
                "plan_id": receipt["plan_id"],
                "total": total,
                "covered_count": len(covered),
                "missing_count": len(missing),
                "extra_count": len(extra),
                "coverage_rate": round(len(covered) / total, 4) if total else 0.0,
                "covered": sorted(covered, key=lambda x: x["biz_date"]),
                "missing_dates": missing,
                "extra_outputs": [
                    {"biz_date": b["biz_date"], "output_path": b["output_path"]} for b in extra
                ],
            }


catalog = PartitionCatalog()
catalog.seed()
