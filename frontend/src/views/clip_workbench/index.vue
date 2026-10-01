<template>
  <section class="page clip-page" data-module="clip_workbench">
    <header class="page-head">
      <div>
        <h2>线段裁剪台 · 管养路段底图</h2>
        <p class="page-desc">
          导入底图先在地图上裁出可通行线段，冲突区直接显示为待裁切块。
          必须按「解析 → 分段核对 → 冲突裁定 → 正式发布」推进，未完成前序核对的线段不许跳级发布。
        </p>
      </div>
      <div class="page-actions">
        <button v-if="draft?.interrupted" class="btn warn" type="button" @click="resumePublish">
          恢复续发（只补未发布线段）
        </button>
        <button class="btn" type="button" @click="loadSample">填入示例文件</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前正式版本</span>
        <strong class="stat-value">{{ state.current_version ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">路段台账 / 巡查待办 / 工程清单</span>
        <strong class="stat-value">
          {{ summary.ledger_total }} · {{ summary.patrol_total }} · {{ summary.project_total }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">悬空引用隔离</span>
        <strong class="stat-value warn-text">{{ summary.quarantine_total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">草稿状态</span>
        <strong class="stat-value">{{ draft ? draft.stage + (draft.interrupted ? '（中断）' : '') : '无草稿' }}</strong>
      </article>
    </div>

    <!-- 阶段流水线：未过门禁不能前进 -->
    <ol class="stage-track">
      <li
        v-for="(stage, index) in state.stages"
        :key="stage"
        class="stage-step"
        :class="{
          active: draft && draft.stage === stage,
          done: isStageDone(index),
          locked: draft && stageIndex > index,
        }"
      >
        <span class="stage-no">{{ index + 1 }}</span>
        <span class="stage-name">{{ stage }}</span>
        <small v-if="draft && draft.stage === stage" class="stage-hint">当前阶段</small>
      </li>
    </ol>

    <div v-if="message" class="notice" :class="messageOk ? 'ok' : 'err'">{{ message }}</div>

    <div class="workbench-grid">
      <!-- 左：裁剪台地图 -->
      <div class="map-card">
        <div class="card-head">
          <h3>底图裁剪台</h3>
          <div class="legend">
            <span><i class="dot formal" />正式层（既有边界保留原线）</span>
            <span><i class="dot draft" />本次草稿线段</span>
            <span><i class="dot block" />待裁切块</span>
          </div>
        </div>
        <svg :viewBox="`0 0 ${map.width} ${map.height}`" class="clip-map">
          <g v-for="lane in map.lanes" :key="lane.line">
            <line :x1="96" :y1="lane.y - 6" :x2="map.width - 24" :y2="lane.y - 6" class="axis" />
            <line :x1="96" :y1="lane.y + 20" :x2="map.width - 24" :y2="lane.y + 20" class="axis dashed" />
            <text :x="8" :y="lane.y + 4" class="lane-label">{{ lane.line }}</text>
          </g>
          <g v-for="item in map.items" :key="`${item.kind}-${item.code ?? item.id}`">
            <rect
              :x="item.x0" :y="item.kind === 'formal' ? item.y - 12 : item.y - 12"
              :width="Math.max(item.x1 - item.x0, 4)" height="14" rx="3"
              :class="['seg', item.kind, {
                checked: item.checked, clipped: item.state === '待裁剪',
                warn: item.clipped_by_formal && item.kind === 'draft',
              }]"
            />
            <text :x="(item.x0 + item.x1) / 2" :y="(item.kind === 'formal' ? item.y : item.y) + 18"
                  class="seg-label">{{ item.label }}</text>
          </g>
          <g v-for="block in map.blocks" :key="`block-${block.id}`">
            <rect :x="block.x0 - 2" :y="block.y - 15" :width="Math.max(block.x1 - block.x0 + 4, 8)"
                  height="20" rx="3" class="block-rect" :class="{ resolved: block.resolved }" />
            <text :x="(block.x0 + block.x1) / 2" :y="block.y - 20" class="block-label"
                  text-anchor="middle">待裁切块 #{{ block.id }}{{ block.resolved ? '（已裁定）' : '' }}</text>
          </g>
        </svg>
        <p class="map-note">
          导入线段先按正式桩号与正式层求差，只留可通行部分；同路线批内重叠在地图上直接框为红色待裁切块。
        </p>
      </div>

      <!-- 右：阶段操作面板 -->
      <div class="panel-card">
        <!-- 解析 -->
        <section v-show="!draft || stageIndex === 0" class="stage-panel">
          <h3>① 解析导入</h3>
          <p class="muted">CSV：路线, 线段名称, 起桩号, 止桩号, 历史区划名称（K0+000 格式；正式桩号优先于历史名称）</p>
          <input v-model="fileName" class="text-input" placeholder="文件名，如 管养底图_20261001.csv" />
          <textarea v-model="fileContent" class="text-area" rows="9"
            placeholder="G104,G104东延段,K2+500,K6+000,东郊片"></textarea>
          <div class="btn-row">
            <button class="btn primary" type="button" :disabled="!fileContent.trim()" @click="doImport">解析并裁剪</button>
          </div>
          <template v-if="draft && stageIndex === 0">
            <ul v-if="draft.duplicates.length" class="note-list dup">
              <li v-for="(item, i) in draft.duplicates" :key="`dup-${i}`">重复上传已去重：{{ item }}</li>
            </ul>
            <ul v-if="draft.parse_errors.length" class="note-list err">
              <li v-for="(item, i) in draft.parse_errors" :key="`err-${i}`">{{ item }}</li>
            </ul>
            <button class="btn primary" type="button" @click="doAdvance">进入分段核对 →</button>
          </template>
        </section>

        <!-- 分段核对 -->
        <section v-show="draft && stageIndex === 1" class="stage-panel">
          <h3>② 分段核对</h3>
          <p class="muted">逐线段按正式桩号核对起终与边界；未核对的线段后面一律发不出去。</p>
          <table class="data-table compact">
            <thead><tr><th>路线</th><th>正式桩号</th><th>历史区划</th><th>状态</th><th></th></tr></thead>
            <tbody>
              <tr v-for="seg in activeSegments" :key="seg.id" :class="{ checked: seg.checked }">
                <td>{{ seg.路线 }}</td>
                <td>{{ seg.正式桩号 }}<small v-if="seg.clipped_by_formal" class="warn-text">（按正式层裁后）</small></td>
                <td>{{ seg.历史区划名称 || '—' }}</td>
                <td>{{ seg.checked ? '已核对' : '待核对' }}</td>
                <td>
                  <button v-if="!seg.checked" class="link" type="button" @click="doCheck(seg.id)">核对</button>
                  <span v-else class="ok-text">✓</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div class="btn-row">
            <button class="btn" type="button" @click="doCheckAll()">全部按正式桩号核对</button>
            <button class="btn primary" type="button" @click="doAdvance">进入冲突裁定 →</button>
          </div>
        </section>

        <!-- 冲突裁定 -->
        <section v-show="draft && stageIndex === 2" class="stage-panel">
          <h3>③ 冲突裁定</h3>
          <p class="muted">每个待裁切块只能保留一条正式桩号线段，其余判离为「待裁剪」，不进入正式层。</p>
          <template v-if="!draft?.conflicts.length">
            <p class="empty-state">本批线段互不重叠，没有待裁切块。</p>
          </template>
          <article v-for="block in draft?.conflicts ?? []" :key="block.id" class="conflict-card"
                   :class="{ resolved: block.resolved }">
            <header>待裁切块 #{{ block.id }}：{{ block.line }} · {{ blockRange(block) }}</header>
            <ul>
              <li v-for="sid in block.segment_ids" :key="sid">
                <label>
                  <input type="radio" :name="`conflict-${block.id}`" :value="sid"
                         :checked="block.winner_id === sid" :disabled="block.resolved"
                         @change="doResolve(block.id, sid)" />
                  <span :class="{ 'loser-line': block.resolved && block.winner_id !== sid }">
                    {{ segOf(sid).路线 }} {{ segOf(sid).正式桩号 }}（{{ segOf(sid).路段名称 }}）
                  </span>
                  <em v-if="block.winner_id === sid" class="ok-text">保留</em>
                  <em v-else-if="block.resolved" class="warn-text">判离·待裁剪</em>
                </label>
              </li>
            </ul>
          </article>
          <div class="btn-row">
            <button class="btn primary" type="button" @click="doAdvance">进入正式发布 →</button>
          </div>
        </section>

        <!-- 正式发布 -->
        <section v-show="draft && stageIndex === 3" class="stage-panel">
          <h3>④ 正式发布</h3>
          <p class="muted">
            只有「已核对且不在败诉待裁切块」里的线段可发布。发布后路段台账、巡查待办、工程清单
            按同一版本原子重算，悬空引用全部隔离。
          </p>
          <ul class="ready-list">
            <li v-for="seg in activeSegments" :key="seg.id"
                :class="{ ready: canPublish(seg), blocked: !canPublish(seg) }">
              {{ seg.路线 }} {{ seg.正式桩号 }}
              <span v-if="canPublish(seg)" class="ok-text">可发布</span>
              <span v-else class="warn-text">{{ blockReason(seg) }}</span>
            </li>
          </ul>
          <label class="filter-item">
            <span>中断演练（本次最多发布 N 条，留草稿待恢复）</span>
            <input v-model.number="publishLimit" type="number" min="1" placeholder="留空=一次发完" />
          </label>
          <div class="btn-row">
            <button class="btn primary" type="button" @click="doPublish">正式发布并同版重算</button>
          </div>
        </section>
      </div>
    </div>

    <!-- 发布版本与同版重算结果 -->
    <div class="version-block">
      <h3>发布版本（正式层不可变）</h3>
      <table class="data-table compact">
        <thead><tr><th>版本号</th><th>状态</th><th>基线版本</th><th>来源文件</th><th>创建</th><th>完成</th><th>线段数</th></tr></thead>
        <tbody>
          <tr v-for="ver in [...state.versions].reverse()" :key="ver.version">
            <td><strong>{{ ver.version }}</strong></td>
            <td>
              <span :class="ver.status === '正式发布' ? 'ok-text' : 'warn-text'">{{ ver.status }}</span>
            </td>
            <td>{{ ver.base_version }}</td>
            <td>{{ ver.source_file }}</td>
            <td>{{ ver.created_at }}</td>
            <td>{{ ver.completed_at ?? '—' }}</td>
            <td>{{ ver.segments.length }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="layers-block">
      <div class="layer-tabs">
        <button v-for="tab in layerTabs" :key="tab.key" class="btn"
                :class="{ primary: activeLayer === tab.key }" type="button"
                @click="activeLayer = tab.key">{{ tab.label }}</button>
      </div>
      <table class="data-table compact">
        <thead>
          <tr><th v-for="col in layerColumns[activeLayer]" :key="col">{{ col }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in layers[activeLayer]" :key="String(row.id ?? row.编号)">
            <td v-for="col in layerColumns[activeLayer]" :key="col">{{ row[col] ?? '—' }}</td>
          </tr>
          <tr v-if="!layers[activeLayer].length">
            <td :colspan="layerColumns[activeLayer].length" class="empty-state">暂无数据</td>
          </tr>
        </tbody>
      </table>
      <p class="map-note">
        三张表均锚定版本 <strong>{{ layers.version ?? state.current_version }}</strong>：底图发布时同步重算，
        不会只更新底图；隔离层中的悬空引用不允许进入任何正式表。
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Stage = string
type Segment = Record<string, string | number | boolean | null> & {
  id: number
  state: string
  checked: boolean
  conflict_ids: number[]
  clipped_by_formal: boolean
}
type Conflict = {
  id: number
  line: string
  range: [number, number]
  segment_ids: number[]
  resolved: boolean
  winner_id: number | null
}
type Draft = {
  source_file: string
  stage: Stage
  interrupted: boolean
  version: string | null
  base_version: string
  segments: Segment[]
  conflicts: Conflict[]
  duplicates: string[]
  parse_errors: string[]
  blockers: string[]
}

const ENDPOINT = '/api/clip_workbench'

const state = ref<{ stages: string[]; draft: Draft | null; versions: any[]; current_version: string | null }>({
  stages: ['解析', '分段核对', '冲突裁定', '正式发布'],
  draft: null,
  versions: [],
  current_version: null,
})
const mapData = ref<{ width: number; height: number; lanes: any[]; items: any[]; blocks: any[] }>(
  { width: 960, height: 180, lanes: [], items: [], blocks: [] },
)
const layersData = ref<{ version: string | null; road_section: any[]; patrol: any[]; project: any[]; quarantine: any[] }>({
  version: null, road_section: [], patrol: [], project: [], quarantine: [],
})

const fileName = ref('管养底图_20261001.csv')
const fileContent = ref('')
const publishLimit = ref<number | null>(null)
const message = ref('')
const messageOk = ref(true)
const activeLayer = ref<'road_section' | 'patrol' | 'project' | 'quarantine'>('road_section')

const draft = computed(() => state.value.draft)
const stageIndex = computed(() => (draft.value ? state.value.stages.indexOf(draft.value.stage) : -1))
const activeSegments = computed(() =>
  (draft.value?.segments ?? []).filter((s) => s.state !== '待裁剪'),
)
const summary = computed(() => {
  const layers = layersData.value
  return {
    ledger_total: layers.road_section.length,
    patrol_total: layers.patrol.length,
    project_total: layers.project.length,
    quarantine_total: layers.quarantine.length,
  }
})
const map = computed(() => mapData.value)
const layers = computed(() => ({ ...layersData.value }))

const layerTabs = [
  { key: 'road_section' as const, label: '路段台账' },
  { key: 'patrol' as const, label: '巡查待办' },
  { key: 'project' as const, label: '工程清单' },
  { key: 'quarantine' as const, label: '悬空引用隔离' },
]
const layerColumns: Record<string, string[]> = {
  road_section: ['路段编号', '路段名称', '路线', '起止桩号', '历史区划名称', '管养单位', '边界来源', '发布版本'],
  patrol: ['巡查编号', '巡查路段', '路段名称', '巡查区间', '巡查日期', '巡查人员', '挂载依据', '发布版本'],
  project: ['工程编号', '工程名称', '工程类型', '施工路段', '工程边界', '承建单位', '工程状态', '挂载依据', '发布版本'],
  quarantine: ['类型', '编号', '名称', '悬空引用', '原因', '尝试版本'],
}

function isStageDone(index: number): boolean {
  return Boolean(draft.value && stageIndex.value > index)
}

function segOf(id: number): Segment {
  return draft.value!.segments.find((s) => s.id === id)!
}

function blockRange(block: Conflict): string {
  const [a, b] = block.range
  const fmt = (m: number) => `K${Math.floor(m / 1000)}+${String(Math.round(m % 1000)).padStart(3, '0')}`
  return `${fmt(a)}～${fmt(b)}`
}

function canPublish(seg: Segment): boolean {
  if (!seg.checked || seg.state === '待裁剪') return false
  const d = draft.value!
  return seg.conflict_ids.every((cid) => {
    const block = d.conflicts.find((c) => c.id === cid)!
    return block.resolved && block.winner_id === seg.id
  })
}

function blockReason(seg: Segment): string {
  if (!seg.checked) return '未完成分段核对，禁止跳级'
  const d = draft.value!
  const pending = seg.conflict_ids.find((cid) => {
    const block = d.conflicts.find((c) => c.id === cid)!
    return !block.resolved || block.winner_id !== seg.id
  })
  return pending === undefined ? '可发布' : `待裁切块 #${pending} 未裁定/已判离`
}

function loadSample() {
  fileContent.value = [
    '路线,线段名称,起桩号,止桩号,历史区划名称',
    'G104,G104东延段,K2+500,K6+000,东郊片',
    'G104,G104东延争议段,K3+000,K7+000,东郊片',
    'G205,G205北环延伸段,K3+800,K5+200,北环片',
    'G104,G104东延段,K2+500,K6+000,东郊片',
  ].join('\n')
}

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

async function post(path: string, body?: unknown) {
  const response = await request(`${ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  return response.json()
}

async function refresh() {
  const [s, m, l] = await Promise.all([
    request(`${ENDPOINT}/state`).then((r) => r.json()),
    request(`${ENDPOINT}/map`).then((r) => r.json()),
    request(`${ENDPOINT}/layers`).then((r) => r.json()),
  ])
  state.value = s
  mapData.value = m
  layersData.value = l
}

async function doImport() {
  const result = await post('/import', { file_name: fileName.value, content: fileContent.value })
  flash(result.message, result.ok)
  await refresh()
}

async function doCheck(segId: number) {
  const result = await post('/check', { segment_id: segId })
  flash(result.message, result.ok)
  await refresh()
}

async function doCheckAll() {
  const result = await post('/check', {})
  flash(result.message, result.ok)
  await refresh()
}

async function doResolve(conflictId: number, winnerId: number) {
  const result = await post('/conflicts/resolve', { conflict_id: conflictId, winner_id: winnerId })
  flash(result.message, result.ok)
  await refresh()
}

async function doAdvance() {
  const result = await post('/advance')
  flash(result.message, result.ok)
  await refresh()
}

async function doPublish() {
  const payload = publishLimit.value ? { limit: publishLimit.value } : {}
  const result = await post('/publish', payload)
  flash(result.message, result.ok)
  publishLimit.value = null
  await refresh()
}

async function resumePublish() {
  const result = await post('/resume')
  flash(result.message, result.ok)
  await refresh()
}

onMounted(refresh)
</script>

<style scoped>
.clip-page { display: flex; flex-direction: column; gap: 12px; }
.warn { color: #b54708; }
.btn.warn { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
.warn-text { color: #b54708; }
.ok-text { color: #067647; }
.muted { color: var(--muted); font-size: 12px; }

.stage-track {
  list-style: none; display: flex; gap: 0; margin: 0; padding: 0;
  background: #fff; border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
}
.stage-step {
  flex: 1; display: flex; align-items: center; gap: 8px; padding: 12px 14px;
  border-right: 1px solid var(--border); font-size: 13px; color: var(--muted);
}
.stage-step:last-child { border-right: none; }
.stage-no {
  width: 22px; height: 22px; border-radius: 50%; background: #e2e8f0; color: #475569;
  display: inline-flex; align-items: center; justify-content: center; font-size: 12px;
}
.stage-step.done { background: #ecfdf3; color: #067647; }
.stage-step.done .stage-no { background: #12b76a; color: #fff; }
.stage-step.active { background: #eff6ff; color: #1d4ed8; font-weight: 600; }
.stage-step.active .stage-no { background: var(--brand); color: #fff; }
.stage-hint { font-size: 11px; color: var(--brand); }

.notice { padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.notice.ok { background: #ecfdf3; border: 1px solid #a6f4c5; color: #067647; }
.notice.err { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; }

.workbench-grid { display: grid; grid-template-columns: 1.5fr 1fr; gap: 12px; }
.map-card, .panel-card, .version-block, .layers-block {
  background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px;
}
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.card-head h3, .version-block h3 { margin: 0 0 8px; font-size: 14px; }
.legend { display: flex; gap: 12px; font-size: 11px; color: var(--muted); }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 4px; }
.dot.formal { background: #12b76a; }
.dot.draft { background: #60a5fa; }
.dot.block { background: #f04438; }
.clip-map { width: 100%; height: auto; background: #fbfdff; border: 1px solid #eef2f7; border-radius: 6px; }
.axis { stroke: #e2e8f0; stroke-width: 1; }
.axis.dashed { stroke-dasharray: 4 4; }
.lane-label { font-size: 11px; fill: #475569; }
.seg { stroke-width: 1; }
.seg.formal { fill: #12b76a; stroke: #027a48; }
.seg.draft { fill: #60a5fa; stroke: #1d4ed8; }
.seg.draft.checked { fill: #84caff; }
.seg.draft.warn { stroke: #f59e0b; stroke-width: 1.5; }
.seg.clipped { fill: #e5e7eb; stroke: #98a2b3; }
.seg-label { font-size: 9px; fill: #334155; text-anchor: middle; }
.block-rect { fill: rgba(240, 68, 56, 0.22); stroke: #f04438; stroke-width: 1.4; stroke-dasharray: 5 3; }
.block-rect.resolved { fill: rgba(18, 183, 106, 0.18); stroke: #12b76a; }
.block-label { font-size: 10px; fill: #b42318; }
.map-note { font-size: 12px; color: var(--muted); margin: 8px 0 0; }

.stage-panel h3 { margin: 0 0 6px; font-size: 14px; }
.text-input, .text-area {
  width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 8px;
  font-size: 13px; margin-bottom: 8px; font-family: inherit;
}
.text-area { resize: vertical; }
.btn-row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.note-list { margin: 8px 0; padding-left: 18px; font-size: 12px; }
.note-list.dup li { color: #b54708; }
.note-list.err li { color: #b42318; }
.data-table.compact th, .data-table.compact td { padding: 5px 8px; font-size: 12px; }
tr.checked { background: #f6fef9; }
.conflict-card { border: 1px solid #fecdca; background: #fef3f2; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; }
.conflict-card.resolved { border-color: #a6f4c5; background: #f6fef9; }
.conflict-card header { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
.conflict-card ul { margin: 0; padding-left: 18px; font-size: 12px; }
.loser-line { color: #98a2b3; text-decoration: line-through; }
.ready-list { list-style: none; margin: 0 0 8px; padding: 0; font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
.ready-list li { display: flex; justify-content: space-between; padding: 5px 8px; border-radius: 5px; }
.ready-list li.ready { background: #ecfdf3; }
.ready-list li.blocked { background: #fef3f2; }
.layers-block { display: flex; flex-direction: column; gap: 8px; }
.layer-tabs { display: flex; gap: 8px; }
</style>
