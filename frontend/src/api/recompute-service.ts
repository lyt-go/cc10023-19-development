/** 切片复算链路接口：分区检索、版本快照、冲突核对、清单提交、计算回执与覆盖核对。 */
import { request } from './client'
import type {
  BackfillCoverage,
  PartitionSearchResult,
  RecomputePlan,
  RecomputeReceipt,
  SelectionCheck,
} from '@/data/recompute-types'

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as T
}

// ── 数据切片侧 ──
export async function searchRecomputePartitions(
  sliceId: number,
  params: {
    start_date?: string
    end_date?: string
    page: number
    size: number
    missing_only: boolean
  },
): Promise<PartitionSearchResult> {
  const query = new URLSearchParams({
    page: String(params.page),
    size: String(params.size),
    missing_only: String(params.missing_only),
  })
  if (params.start_date) query.set('start_date', params.start_date)
  if (params.end_date) query.set('end_date', params.end_date)
  const response = await request(`/api/data_slice/${sliceId}/recompute/partitions?${query}`)
  if (!response.ok) throw new Error('可复算分区检索失败')
  return await response.json()
}

export async function freezeSnapshots(
  sliceId: number,
  searchId: number,
  bizDates: string[],
): Promise<{ ok: boolean; message: string; snapshots: { snapshot_id: number }[] }> {
  return postJson(`/api/data_slice/${sliceId}/recompute/snapshots`, {
    search_id: searchId,
    biz_dates: bizDates,
  })
}

export async function checkSnapshots(
  snapshotIds: number[],
): Promise<{ ok: boolean; available: boolean; message: string; checks: SelectionCheck[] }> {
  return postJson('/api/data_slice/recompute/check-snapshots', { snapshot_ids: snapshotIds })
}

export async function registerRevision(
  sliceId: number,
  bizDate: string,
  kind: 'revision' | 'backfill',
): Promise<{ ok: boolean; message: string }> {
  return postJson(`/api/data_slice/${sliceId}/recompute/revisions`, {
    biz_date: bizDate,
    kind,
  })
}

export async function listRecomputePlans(
  sliceId: number,
): Promise<{ ok: boolean; total: number; plans: RecomputePlan[] }> {
  const response = await request(`/api/data_slice/${sliceId}/recompute/plans`)
  if (!response.ok) throw new Error('复算清单读取失败')
  return await response.json()
}

export async function submitRecomputePlan(
  sliceId: number,
  snapshotIds: number[],
): Promise<{ ok: boolean; message: string; plan?: RecomputePlan; conflicts?: SelectionCheck[] }> {
  return postJson(`/api/data_slice/${sliceId}/recompute/plans`, { snapshot_ids: snapshotIds })
}

// ── 特征计算任务侧 ──
export async function startRecomputeTask(
  planId: number,
): Promise<{ ok: boolean; message: string; task_id?: number }> {
  return postJson('/api/feature_compute/recompute/tasks', { plan_id: planId })
}

export async function advanceRecomputeTask(
  taskId: number,
  bizDates?: string[],
): Promise<{ ok: boolean; message: string }> {
  return postJson(`/api/feature_compute/recompute/tasks/${taskId}/advance`, {
    biz_dates: bizDates ?? null,
  })
}

export async function fetchRecomputeReceipt(
  taskId: number,
): Promise<{ ok: boolean; message?: string; receipt: RecomputeReceipt }> {
  const response = await request(`/api/feature_compute/recompute/tasks/${taskId}/receipt`)
  if (!response.ok) throw new Error('复算回执读取失败')
  return await response.json()
}

export async function fetchRecomputeCoverage(
  taskId: number,
): Promise<{
  ok: boolean
  fully_covered?: boolean
  message: string
  coverage?: BackfillCoverage
}> {
  const response = await request(`/api/feature_compute/recompute/tasks/${taskId}/coverage`)
  if (!response.ok) throw new Error('回填覆盖核对失败')
  return await response.json()
}
