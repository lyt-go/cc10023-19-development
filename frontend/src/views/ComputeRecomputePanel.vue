<template>
  <section class="compute-panel" data-testid="compute-recompute-panel">
    <header class="panel-head">
      <h3>复算任务与回填核对</h3>
      <p class="panel-desc">
        复算任务读取切片复算分区清单作为不可变输入；进度回执可查实际处理的业务日期与版本，
        出数后核对回填覆盖，防止漏算或清单外出数。
      </p>
    </header>

    <div class="task-picker">
      <label class="form-item">
        <span>复算任务</span>
        <select v-model.number="selectedTaskId" data-testid="recompute-task-select" @change="loadTaskDetail">
          <option :value="0" disabled>请选择任务</option>
          <option v-for="task in recomputeTasks" :key="String(task.id)" :value="Number(task.id)">
            {{ task['任务编号'] }}（#{{ task.id }}，{{ task.status }}）
          </option>
        </select>
      </label>
      <button class="btn" data-testid="refresh-tasks" type="button" @click="loadTasks">刷新任务列表</button>
    </div>

    <template v-if="selectedTask">
      <div class="task-summary" data-testid="task-summary">
        <span>任务编号：{{ selectedTask['任务编号'] }}</span>
        <span>状态：{{ selectedTask.status }}</span>
        <span>数据切片：{{ selectedTask['数据切片'] }}</span>
      </div>
      <div class="io-box">
        <h4>输入分区（来自复算清单，逐日期锁定版本）</h4>
        <p class="io-text" data-testid="input-partitions">{{ selectedTask['输入分区'] }}</p>
        <h4>计划输出分区</h4>
        <p class="io-text">{{ selectedTask['输出分区'] }}</p>
      </div>

      <div v-if="receipt" class="receipt-box" data-testid="receipt-box">
        <div class="receipt-head">
          <h4>进度回执</h4>
          <span data-testid="receipt-progress">
            {{ receipt.processed_count }}/{{ receipt.total }}｜{{ receipt.status }}
          </span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>业务日期</th>
              <th>清单输入版本</th>
              <th>处理状态</th>
              <th>实际产出版本</th>
              <th>产出路径</th>
              <th>处理时间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="part in receipt.partitions"
              :key="part.biz_date"
              :data-testid="`receipt-row-${part.biz_date}`"
            >
              <td>{{ part.biz_date }}</td>
              <td>{{ part.input_version === null ? '缺失态（无版本）' : `v${part.input_version}` }}</td>
              <td>
                <span :class="['state-tag', part.processed ? 'state-done' : 'state-pending']">
                  {{ part.state }}
                </span>
              </td>
              <td>{{ part.output_version === null ? '—' : `v${part.output_version}` }}</td>
              <td class="path-cell">{{ part.output_path ?? '—' }}</td>
              <td>{{ part.processed_at ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="receipt-actions">
          <button
            class="btn primary"
            data-testid="advance-one"
            type="button"
            :disabled="allProcessed"
            @click="advanceNext"
          >
            处理下一个清单分区
          </button>
          <button
            class="btn"
            data-testid="advance-all"
            type="button"
            :disabled="allProcessed"
            @click="advanceAll"
          >
            一键处理全部
          </button>
          <button class="btn" data-testid="check-coverage" type="button" @click="loadCoverage">
            核对回填覆盖
          </button>
        </div>
      </div>

      <div v-if="coverage" class="coverage-box" data-testid="coverage-box">
        <h4>回填覆盖核对</h4>
        <p :class="coverage.fully_covered ? 'ok-text' : 'warn-text'" data-testid="coverage-summary">
          {{ coverage.message }}（覆盖率 {{ (coverage.coverage.coverage_rate * 100).toFixed(0) }}%，
          已覆盖 {{ coverage.coverage.covered_count }}/{{ coverage.coverage.total }}，
          漏回填 {{ coverage.coverage.missing_count }}，清单外产出 {{ coverage.coverage.extra_count }}）
        </p>
        <ul class="coverage-list">
          <li v-for="item in coverage.coverage.covered" :key="item.biz_date" data-testid="coverage-item">
            {{ item.biz_date }}：输入 {{ item.input_version === null ? 'missing' : `v${item.input_version}` }}
            → 产出 v{{ item.version }}（{{ item.output_path }}）
          </li>
        </ul>
        <p v-if="coverage.coverage.missing_dates.length" class="warn-text">
          尚未回填：{{ coverage.coverage.missing_dates.join('、') }}
        </p>
      </div>
    </template>

    <p v-else class="empty-state">请选择一个由复算清单发起的计算任务</p>
    <p v-if="message" class="panel-message" data-testid="compute-message">{{ message }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  advanceRecomputeTask,
  fetchRecomputeCoverage,
  fetchRecomputeReceipt,
} from '@/api/recompute-service'
import { listEntries } from '@/api/backend-service'
import type { BackfillCoverage, RecomputeReceipt } from '@/data/recompute-types'
import type { EntryRow } from '@/data/types'

const recomputeTasks = ref<EntryRow[]>([])
const selectedTaskId = ref(0)
const receipt = ref<RecomputeReceipt | null>(null)
const coverage = ref<{
  fully_covered?: boolean
  message: string
  coverage: BackfillCoverage
} | null>(null)
const message = ref('')

const selectedTask = computed(() =>
  recomputeTasks.value.find((task) => Number(task.id) === selectedTaskId.value) ?? null,
)
const allProcessed = computed(() =>
  receipt.value ? receipt.value.processed_count >= receipt.value.total : false,
)

onMounted(loadTasks)

async function loadTasks() {
  message.value = ''
  try {
    const payload = await listEntries('feature_compute')
    recomputeTasks.value = payload.items.filter((row) => Boolean(row.recompute))
    if (selectedTaskId.value && !recomputeTasks.value.some(
      (task) => Number(task.id) === selectedTaskId.value,
    )) {
      selectedTaskId.value = 0
      receipt.value = null
      coverage.value = null
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复算任务读取失败'
  }
}

async function loadTaskDetail() {
  receipt.value = null
  coverage.value = null
  message.value = ''
  if (!selectedTaskId.value) return
  try {
    const result = await fetchRecomputeReceipt(selectedTaskId.value)
    if (!result.ok) throw new Error(result.message ?? '回执读取失败')
    receipt.value = result.receipt
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回执读取失败'
  }
}

async function refreshReceipt() {
  if (!selectedTaskId.value) return
  const result = await fetchRecomputeReceipt(selectedTaskId.value)
  if (result.ok) receipt.value = result.receipt
}

async function advanceNext() {
  message.value = ''
  if (!receipt.value) return
  const next = receipt.value.partitions.find((part) => !part.processed)
  if (!next) return
  try {
    const result = await advanceRecomputeTask(selectedTaskId.value, [next.biz_date])
    if (!result.ok) throw new Error(result.message)
    await refreshReceipt()
    await loadTasks()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '分区处理失败'
  }
}

async function advanceAll() {
  message.value = ''
  try {
    const result = await advanceRecomputeTask(selectedTaskId.value)
    if (!result.ok) throw new Error(result.message)
    await refreshReceipt()
    await loadTasks()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '分区处理失败'
  }
}

async function loadCoverage() {
  message.value = ''
  try {
    const result = await fetchRecomputeCoverage(selectedTaskId.value)
    if (!result.ok) throw new Error(result.message)
    coverage.value = {
      fully_covered: result.fully_covered,
      message: result.message,
      coverage: result.coverage as BackfillCoverage,
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '覆盖核对失败'
  }
}
</script>

<style scoped>
.compute-panel {
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
.task-picker {
  display: flex;
  gap: 12px;
  align-items: flex-end;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.form-item select {
  padding: 6px 8px;
  border: 1px solid #c7d2e0;
  border-radius: 6px;
  min-width: 280px;
}
.task-summary {
  display: flex;
  gap: 18px;
  margin: 14px 0;
  font-size: 13px;
  font-weight: 600;
}
.io-box,
.receipt-box,
.coverage-box {
  margin-top: 14px;
  padding: 12px;
  border: 1px dashed #b8c6d8;
  border-radius: 8px;
  background: #fff;
}
.io-box h4,
.receipt-box h4,
.coverage-box h4 {
  margin: 0 0 8px;
}
.io-text {
  font-size: 12px;
  color: #3d4c60;
  word-break: break-all;
  margin: 4px 0 10px;
}
.receipt-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.state-tag {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.state-done {
  background: #e8f6ee;
  color: #1e8449;
}
.state-pending {
  background: #fef5e7;
  color: #b9770e;
}
.path-cell {
  font-size: 12px;
  color: #5b6b7f;
}
.receipt-actions {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}
.coverage-list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ok-text {
  color: #1e8449;
}
.warn-text {
  color: #b9770e;
}
.panel-message {
  margin-top: 12px;
  font-size: 13px;
  color: #c0392b;
}
.empty-state {
  margin-top: 16px;
  color: #7f8c9b;
  font-size: 13px;
}
</style>
