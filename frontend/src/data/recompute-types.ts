/** 切片复算分区清单相关类型，字段与后端 recompute 路由返回结构保持一致。 */

export type PartitionVersion = {
  version: number
  state: string
  row_count: number
  updated_at: string | null
}

export type RecomputePartition = {
  biz_date: string
  state: string
  missing: boolean
  recomputable: boolean
  current_version: number | null
  row_count: number
  updated_at: string | null
  version_chain: PartitionVersion[]
}

export type PartitionSearchResult = {
  ok: boolean
  message?: string
  items: RecomputePartition[]
  total: number
  page: number
  size: number
  search_id: number
  slice_id: number
  start_date: string
  end_date: string
  missing_only: boolean
  slice_name?: string
}

export type FrozenSnapshot = {
  snapshot_id: number
  search_id: number
  slice_id: number
  biz_date: string
  version: number | null
  state: string
  created_at: string
}

export type SelectionCheck = {
  snapshot_id: number
  available: boolean
  conflict: boolean
  biz_date: string | null
  frozen_version: number | null
  frozen_state?: string
  current_version: number | null
  current_state?: string
  reason: string
}

export type PlanEntry = {
  snapshot_id: number
  biz_date: string
  version: number | null
  state: string
}

export type RecomputePlan = {
  plan_id: number
  slice_id: number
  search_id: number
  operator: string
  status: string
  created_at: string
  task_id: number | null
  entries: PlanEntry[]
}

export type ReceiptPartition = {
  biz_date: string
  input_version: number | null
  state: string
  processed: boolean
  output_version: number | null
  output_path: string | null
  processed_at: string | null
}

export type RecomputeReceipt = {
  task_id: number
  plan_id: number
  slice_id: number
  status: string
  total: number
  processed_count: number
  partitions: ReceiptPartition[]
  started_at: string
}

export type BackfillCoverage = {
  task_id: number
  plan_id: number
  total: number
  covered_count: number
  missing_count: number
  extra_count: number
  coverage_rate: number
  covered: {
    biz_date: string
    version: number
    input_version: number | null
    output_path: string
  }[]
  missing_dates: string[]
  extra_outputs: { biz_date: string; output_path: string }[]
}
