<template>
  <section class="recompute-panel" data-testid="slice-recompute-panel">
    <header class="panel-head">
      <h3>切片复算分区清单</h3>
      <p class="panel-desc">
        按日期检索切片缺失分区，跨页勾选会立即固化当时见到的分区版本；
        迟到修订后可核对现有选择是否仍可用于本轮回填。
      </p>
    </header>

    <form class="recompute-form" @submit.prevent="runSearch(1)">
      <label class="form-item">
        <span>数据切片</span>
        <select v-model.number="sliceId" data-testid="slice-select" @change="resetSelection">
          <option v-for="row in slices" :key="String(row.id)" :value="Number(row.id)">
            {{ row['切片编号'] }}｜{{ row['切片名称'] }}
          </option>
        </select>
      </label>
      <label class="form-item">
        <span>开始日期</span>
        <input v-model="startDate" data-testid="start-date" type="date" required />
      </label>
      <label class="form-item">
        <span>结束日期</span>
        <input v-model="endDate" data-testid="end-date" type="date" required />
      </label>
      <label class="form-item form-check">
        <input v-model="missingOnly" data-testid="missing-only" type="checkbox" />
        <span>仅看缺数分区</span>
      </label>
      <button class="btn primary" data-testid="recompute-search" type="submit">检索缺数范围</button>
    </form>

    <p v-if="searchResult" class="search-meta" data-testid="search-meta">
      检索编号 #{{ searchResult.search_id }}（同一次搜索翻页保持不变）｜
      共 {{ searchResult.total }} 个可复算分区，第 {{ searchResult.page }} 页
      <button class="link" type="button" @click="runAvailabilityCheck">核对已选可用性</button>
    </p>

    <table v-if="searchResult" class="data-table recompute-table">
      <thead>
        <tr>
          <th>选择</th>
          <th>业务日期</th>
          <th>分区状态</th>
          <th>当前版本</th>
          <th>行数</th>
          <th>版本链</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in searchResult.items" :key="item.biz_date" :data-testid="`partition-row-${item.biz_date}`">
          <td>
            <input
              :checked="selection.has(item.biz_date)"
              :data-testid="`check-${item.biz_date}`"
              type="checkbox"
              @change="toggleSelection(item, ($event.target as HTMLInputElement).checked)"
            />
          </td>
          <td>{{ item.biz_date }}</td>
          <td>
            <span :class="['state-tag', item.missing ? 'state-missing' : 'state-normal']">{{ item.state }}</span>
          </td>
          <td>{{ item.current_version === null ? '无版本（缺数）' : `v${item.current_version}` }}</td>
          <td>{{ item.row_count }}</td>
          <td>
            <span v-if="item.version_chain.length">{{ formatVersionChain(item.version_chain) }}</span>
            <span v-else>—</span>
          </td>
        </tr>
        <tr v-if="!searchResult.items.length">
          <td colspan="6" class="empty-state">该窗口内没有可复算分区</td>
        </tr>
      </tbody>
    </table>

    <div v-if="searchResult && searchResult.total > pageSize" class="pager">
      <button
        class="btn"
        data-testid="prev-page"
        type="button"
        :disabled="searchResult.page <= 1"
        @click="runSearch(searchResult.page - 1)"
      >
        上一页
      </button>
      <button
        class="btn"
        data-testid="next-page"
        type="button"
        :disabled="searchResult.page * pageSize >= searchResult.total"
        @click="runSearch(searchResult.page + 1)"
      >
        下一页
      </button>
    </div>

    <div v-if="frozenList.length" class="selection-box" data-testid="selection-box">
      <h4>本次已勾选（{{ frozenList.length }} 片，版本已固化，翻页不丢失）</h4>
      <ul class="snapshot-list">
        <li
          v-for="snap in frozenList"
          :key="snap.snapshot_id"
          :class="{ conflict: conflictMap.has(snap.snapshot_id) }"
          :data-testid="`snapshot-${snap.snapshot_id}`"
        >
          <span class="snap-date">{{ snap.biz_date }}</span>
          <span class="snap-version">
            固化版本：{{ snap.version === null ? '缺失态（无版本）' : `v${snap.version}` }}
          </span>
          <span v-if="conflictMap.has(snap.snapshot_id)" class="conflict-text">
            ⚠ {{ conflictMap.get(snap.snapshot_id) }}
          </span>
          <button class="link danger" type="button" @click="removeSelection(snap.biz_date)">移除</button>
        </li>
      </ul>
      <div class="selection-actions">
        <button class="btn" data-testid="check-availability" type="button" @click="runAvailabilityCheck">
          检查选择是否仍可回填
        </button>
        <button
          class="btn primary"
          data-testid="submit-plan"
          type="button"
          :disabled="submitting"
          @click="submitPlan"
        >
          {{ submitting ? '提交中…' : `提交复算清单（${frozenList.length} 片）` }}
        </button>
      </div>
    </div>

    <div class="revision-box" data-testid="revision-box">
      <h4>迟到数据模拟（演示冲突场景）</h4>
      <div class="revision-form">
        <input v-model="revisionDate" data-testid="revision-date" type="date" />
        <button class="btn" data-testid="make-revision" type="button" @click="makeRevision('revision')">
          登记已有分区修订
        </button>
        <button class="btn" data-testid="make-backfill" type="button" @click="makeRevision('backfill')">
          缺失分区补报
        </button>
      </div>
    </div>

    <div v-if="plans.length" class="plan-box" data-testid="plan-box">
      <h4>切片复算分区清单（{{ plans.length }}）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>清单编号</th>
            <th>分区（日期@版本）</th>
            <th>状态</th>
            <th>计算任务</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in plans" :key="plan.plan_id" :data-testid="`plan-row-${plan.plan_id}`">
            <td>#{{ plan.plan_id }}</td>
            <td>
              <span v-for="entry in plan.entries" :key="entry.snapshot_id" class="entry-chip">
                {{ entry.biz_date }}@{{ entry.version === null ? 'missing' : `v${entry.version}` }}
              </span>
            </td>
            <td>{{ plan.status }}</td>
            <td>{{ plan.task_id === null ? '未发起' : `#${plan.task_id}` }}</td>
            <td>
              <button
                v-if="plan.task_id === null"
                class="btn primary"
                data-testid="start-recompute"
                type="button"
                @click="$emit('start-recompute', plan.plan_id)"
              >
                发起复算
              </button>
              <span v-else>已在特征计算任务中跟踪</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="message" :class="messageKind" data-testid="recompute-message">{{ message }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  checkSnapshots,
  freezeSnapshots,
  listRecomputePlans,
  registerRevision,
  searchRecomputePartitions,
  submitRecomputePlan,
} from '@/api/recompute-service'
import { listEntries } from '@/api/backend-service'
import type {
  FrozenSnapshot,
  PartitionSearchResult,
  RecomputePartition,
  RecomputePlan,
  SelectionCheck,
} from '@/data/recompute-types'
import type { EntryRow } from '@/data/types'

const emit = defineEmits<{ (event: 'start-recompute', planId: number): void }>()

const slices = ref<EntryRow[]>([])
const sliceId = ref(1)
const startDate = ref('2026-11-16')
const endDate = ref('2026-11-22')
const missingOnly = ref(true)
const pageSize = 5
const searchResult = ref<PartitionSearchResult | null>(null)
// 业务日期 -> 固化快照（快照在首次勾选时生成，之后翻页/修订都不改写它）
const selection = ref<Map<string, FrozenSnapshot>>(new Map())
const conflictMap = ref<Map<number, string>>(new Map())
const plans = ref<RecomputePlan[]>([])
const revisionDate = ref('2026-11-20')
const message = ref('')
const messageKind = ref('panel-message')
const submitting = ref(false)

const frozenList = computed(() => Array.from(selection.value.values()))

function formatVersionChain(chain: { version: number; state: string }[]): string {
  return chain.map((p) => `v${p.version}(${p.state})`).join(' → ')
}

onMounted(async () => {
  const payload = await listEntries('data_slice')
  slices.value = payload.items
  if (slices.value.length) sliceId.value = Number(slices.value[0].id)
  await loadPlans()
})

async function runSearch(page: number) {
  message.value = ''
  try {
    searchResult.value = await searchRecomputePartitions(sliceId.value, {
      start_date: startDate.value,
      end_date: endDate.value,
      page,
      size: pageSize,
      missing_only: missingOnly.value,
    })
  } catch (error) {
    message.value = error instanceof Error ? error.message : '分区检索失败'
  }
}

async function toggleSelection(item: RecomputePartition, checked: boolean) {
  message.value = ''
  if (!checked) {
    removeSelection(item.biz_date)
    return
  }
  if (selection.value.has(item.biz_date)) return
  if (!searchResult.value) return
  try {
    // 勾选即固化：记录此刻见到的版本，保证向后翻页后仍能定位当时版本
    const result = await freezeSnapshots(
      sliceId.value,
      searchResult.value.search_id,
      [item.biz_date],
    )
    if (!result.ok) throw new Error(result.message)
    const snapshotId = result.snapshots[0].snapshot_id
    selection.value.set(item.biz_date, {
      snapshot_id: snapshotId,
      search_id: searchResult.value.search_id,
      slice_id: sliceId.value,
      biz_date: item.biz_date,
      version: item.current_version,
      state: item.state,
      created_at: new Date().toISOString(),
    })
    // 触发响应式更新
    selection.value = new Map(selection.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '版本固化失败'
  }
}

function removeSelection(bizDate: string) {
  const removed = selection.value.get(bizDate)
  selection.value.delete(bizDate)
  selection.value = new Map(selection.value)
  if (removed) {
    conflictMap.value.delete(removed.snapshot_id)
    conflictMap.value = new Map(conflictMap.value)
  }
}

async function resetSelection() {
  selection.value = new Map()
  conflictMap.value = new Map()
  searchResult.value = null
  message.value = ''
  await loadPlans()
}

async function runAvailabilityCheck() {
  const ids = frozenList.value.map((snap) => snap.snapshot_id)
  if (!ids.length) {
    message.value = '还没有勾选任何分区'
    return
  }
  try {
    const result = await checkSnapshots(ids)
    conflictMap.value = new Map(
      result.checks
        .filter((check: SelectionCheck) => check.conflict)
        .map((check: SelectionCheck) => [check.snapshot_id, check.reason]),
    )
    message.value = result.message
    messageKind.value = result.available ? 'panel-message ok' : 'panel-message warn'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '可用性核对失败'
  }
}

async function makeRevision(kind: 'revision' | 'backfill') {
  message.value = ''
  try {
    const result = await registerRevision(sliceId.value, revisionDate.value, kind)
    message.value = result.message
    messageKind.value = result.ok ? 'panel-message warn' : 'panel-message'
    if (searchResult.value) await runSearch(searchResult.value.page)
    if (frozenList.value.length) await runAvailabilityCheck()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '迟到数据登记失败'
  }
}

async function submitPlan() {
  submitting.value = true
  message.value = ''
  try {
    const ids = frozenList.value.map((snap) => snap.snapshot_id)
    const result = await submitRecomputePlan(sliceId.value, ids)
    if (!result.ok) {
      message.value = result.message
      messageKind.value = 'panel-message warn'
      if (result.conflicts?.length) {
        conflictMap.value = new Map(
          result.conflicts.map((check) => [check.snapshot_id, check.reason]),
        )
      }
      return
    }
    message.value = result.message
    messageKind.value = 'panel-message ok'
    selection.value = new Map()
    conflictMap.value = new Map()
    await loadPlans()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复算清单提交失败'
  } finally {
    submitting.value = false
  }
}

async function loadPlans() {
  try {
    const result = await listRecomputePlans(sliceId.value)
    plans.value = result.plans
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复算清单读取失败'
  }
}

defineExpose({ refreshPlans: loadPlans })
</script>

<style scoped>
.recompute-panel {
  margin-top: 24px;
  padding: 16px;
  border: 1px solid #d9e1ec;
  border-radius: 10px;
  background: #fbfcfe;
}
.panel-head h3 {
  margin: 0 0 6px;
}
.panel-desc {
  margin: 0 0 14px;
  color: #5b6b7f;
  font-size: 13px;
}
.recompute-form {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.form-item input,
.form-item select {
  padding: 6px 8px;
  border: 1px solid #c7d2e0;
  border-radius: 6px;
}
.form-check {
  flex-direction: row;
  align-items: center;
  gap: 6px;
}
.search-meta {
  margin: 12px 0;
  font-size: 13px;
  color: #3d4c60;
}
.recompute-table td,
.recompute-table th {
  font-size: 13px;
}
.state-tag {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.state-missing {
  background: #fdecec;
  color: #c0392b;
}
.state-normal {
  background: #e8f6ee;
  color: #1e8449;
}
.pager {
  display: flex;
  gap: 10px;
  margin: 12px 0;
}
.selection-box,
.revision-box,
.plan-box {
  margin-top: 16px;
  padding: 12px;
  border: 1px dashed #b8c6d8;
  border-radius: 8px;
  background: #fff;
}
.selection-box h4,
.revision-box h4,
.plan-box h4 {
  margin: 0 0 10px;
}
.snapshot-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.snapshot-list li {
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 13px;
}
.snapshot-list li.conflict {
  color: #c0392b;
}
.snap-date {
  font-weight: 600;
  min-width: 96px;
}
.conflict-text {
  color: #c0392b;
}
.selection-actions,
.revision-form {
  display: flex;
  gap: 10px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.revision-form input {
  padding: 6px 8px;
  border: 1px solid #c7d2e0;
  border-radius: 6px;
}
.entry-chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 2px 8px;
  border-radius: 10px;
  background: #eef3fb;
  font-size: 12px;
}
.panel-message {
  margin-top: 12px;
  font-size: 13px;
  color: #c0392b;
}
.panel-message.ok {
  color: #1e8449;
}
.panel-message.warn {
  color: #b9770e;
}
.link.danger {
  color: #c0392b;
}
</style>
