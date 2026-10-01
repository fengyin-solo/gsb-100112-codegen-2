<template>
  <section class="page clipping-page" data-module="clipping">
    <header class="page-head">
      <div>
        <h2>线段裁剪台</h2>
        <p class="page-desc">
          管养路段底图唯一入口：导入文件先在地图上裁出可通行线段，冲突区直接成为待裁切块。
          严格按「解析 → 分段核对 → 冲突裁定 → 正式发布」推进，未完成前序核对的线段不允许跳级发布；
          发布后台账、巡查待办、工程边界按同一版本重算，悬空引用不进正式层。
        </p>
      </div>
      <div class="page-actions">
        <label class="btn primary">
          导入线段文件
          <input class="hidden-file" type="file" accept=".csv,.json,.txt" @change="onFile" />
        </label>
        <button class="btn" type="button" @click="loadSample">载入演示样例</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card"><span class="stat-label">当前发布版本</span><strong class="stat-value sm">{{ state.current_version }}</strong></article>
      <article class="stat-card"><span class="stat-label">正式路段</span><strong class="stat-value">{{ state.stats.segments }}</strong></article>
      <article class="stat-card"><span class="stat-label">台账 / 巡查待办</span><strong class="stat-value sm">{{ state.stats.ledger }} / {{ state.stats.patrol_todos }}</strong></article>
      <article class="stat-card" :class="{ warn: state.stats.dangling_bounds > 0 }">
        <span class="stat-label">悬空工程边界</span><strong class="stat-value">{{ state.stats.dangling_bounds }}</strong>
      </article>
    </div>

    <!-- 四阶段流水线 -->
    <ol class="stage-bar">
      <li
        v-for="(name, index) in stages"
        :key="name"
        class="stage-item"
        :class="stageClass(index)"
      >
        <span class="stage-no">{{ index + 1 }}</span>
        <span class="stage-name">{{ name }}</span>
      </li>
    </ol>

    <div v-if="message" class="notice" :class="messageKind">{{ message }}</div>

    <!-- 底图 -->
    <div class="map-card">
      <div class="map-head">
        <h3>底图：线段裁剪台</h3>
        <div class="legend">
          <span><i class="lg formal"></i>正式路段</span>
          <span><i class="lg passable"></i>可通行线段（待核对）</span>
          <span><i class="lg conflict"></i>待裁切块（冲突区）</span>
        </div>
      </div>
      <svg class="map-svg" viewBox="0 0 800 400" role="img" aria-label="管养路段底图">
        <defs>
          <pattern id="hatch" patternUnits="userSpaceOnUse" width="7" height="7" patternTransform="rotate(45)">
            <rect width="7" height="7" fill="#fee4e2" />
            <line x1="0" y1="0" x2="0" y2="7" stroke="#d92d20" stroke-width="2" />
          </pattern>
        </defs>
        <template v-for="route in mapData.routes" :key="route.code">
          <polyline :points="route.points" fill="none" stroke="#e2e8f0" stroke-width="14" stroke-linecap="round" />
          <polyline
            v-for="seg in route.segments"
            :key="seg.id"
            :points="seg.points"
            fill="none"
            :stroke="seg.version === state.current_version ? '#1f6feb' : '#334155'"
            stroke-width="8"
            stroke-linecap="round"
          >
            <title>{{ seg.code }} {{ seg.name }} {{ seg.stake_start }}~{{ seg.stake_end }}（{{ seg.version }}）</title>
          </polyline>
        </template>
        <template v-for="piece in mapData.draft_pieces" :key="piece.id">
          <polyline
            :points="piece.points"
            fill="none"
            :stroke="piece.kind === 'conflict' ? 'url(#hatch)' : '#f79009'"
            :stroke-width="piece.kind === 'conflict' ? 12 : 7"
            stroke-linecap="round"
            :class="{ selected: selectedPiece === piece.id }"
            @click="selectedPiece = piece.id"
          >
            <title>{{ piece.kind_label }} {{ piece.stake_start }}~{{ piece.stake_end }}</title>
          </polyline>
          <circle
            :cx="piece.label_xy[0]"
            :cy="piece.label_xy[1] + (piece.kind === 'conflict' ? -18 : 20)"
            r="9"
            :fill="piece.kind === 'conflict' ? '#d92d20' : '#f79009'"
            class="map-badge"
            @click="selectedPiece = piece.id"
          />
          <text
            :x="piece.label_xy[0]"
            :y="piece.label_xy[1] + (piece.kind === 'conflict' ? -14 : 24)"
            class="badge-text"
            text-anchor="middle"
            @click="selectedPiece = piece.id"
          >{{ piece.id }}</text>
        </template>
        <template v-for="route in mapData.routes" :key="`${route.code}-label`">
          <text :x="firstPoint(route.points).x - 34" :y="firstPoint(route.points).y + 4" class="route-label">{{ route.code }}</text>
        </template>
      </svg>
    </div>

    <!-- 草稿工作台 -->
    <div v-if="draft.exists" class="draft-card">
      <div class="draft-head">
        <h3>裁剪草稿 #{{ draft.id }} · {{ draft.filename }}</h3>
        <span class="draft-meta">
          {{ draft.stage }} · 版本 {{ draft.version ?? '待分配' }}
          <em v-if="draft.interrupted" class="tag danger">发布中断 · 已留草稿</em>
        </span>
      </div>

      <!-- 候选线段（解析结果） -->
      <section class="block">
        <h4>① 解析结果：候选线段</h4>
        <table class="data-table">
          <thead>
            <tr><th>行</th><th>路线</th><th>路段名称（导入名）</th><th>历史区划名称（仅留痕）</th><th>正式桩号区间</th><th>解析结果</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in draft.candidates" :key="c.id">
              <td>{{ c.raw_line }}</td>
              <td>{{ c.route_code }}</td>
              <td>{{ c.name || '—' }}</td>
              <td class="muted">{{ c.historical_name || '—' }}</td>
              <td>{{ c.stake_start }} ~ {{ c.stake_end }}</td>
              <td>
                <template v-if="c.duplicate_of"><span class="tag ok">重复上传，已发布：{{ c.duplicate_of }}</span></template>
                <template v-else-if="c.errors.length"><span v-for="e in c.errors" :key="e" class="tag danger">{{ e }}</span></template>
                <template v-else>
                  <span class="tag">已裁剪，进入分段核对</span>
                  <button v-if="canCorrect(c)" class="link" type="button" @click="correct(c)">按正式桩号纠正</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 分段核对 / 冲突裁定 -->
      <section class="block">
        <h4>
          ② 分段核对 与 ③ 冲突裁定
          <button v-if="draft.stage === '分段核对' || draft.stage === '冲突裁定'" class="btn sm" type="button" @click="checkAll">全部核对通过</button>
        </h4>
        <table class="data-table">
          <thead>
            <tr><th>切块#</th><th>路线</th><th>正式桩号</th><th>类型</th><th>重叠的既有路段</th><th>核对</th><th>裁定 / 状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in draft.pieces" :key="p.id" :class="{ picked: selectedPiece === p.id }">
              <td>#{{ p.id }}</td>
              <td>{{ p.route_code }}</td>
              <td>{{ p.stake_start }} ~ {{ p.stake_end }}</td>
              <td>
                <span :class="['tag', p.kind]">{{ p.kind_label }}</span>
              </td>
              <td>
                <span v-if="!p.overlap_with.length" class="muted">无（新增里程）</span>
                <span v-else>{{ p.overlap_with.map((s) => `${s.code} ${s.name}`).join('；') }}</span>
              </td>
              <td>
                <label class="check-cell">
                  <input type="checkbox" :checked="p.checked" :disabled="p.published || !checkEnabled(p)" @change="toggleCheck(p)" />
                  {{ p.checked ? '已核对' : '待核对' }}
                </label>
              </td>
              <td>
                <span v-if="p.published" class="tag ok">已随中断发布入正式层（恢复时跳过）</span>
                <span v-else-if="p.kind === 'conflict'">
                  <em v-if="p.decision" class="tag" :class="p.decision">{{ p.decision_label }}</em>
                  <span v-else class="muted">待裁定</span>
                </span>
                <span v-else class="muted">无需裁定</span>
              </td>
              <td class="row-actions">
                <template v-if="p.kind === 'conflict' && !p.published">
                  <button class="link" type="button" :disabled="!decideEnabled(p, 'use_formal')" @click="decide(p, 'use_formal')">正式桩号裁入</button>
                  <button class="link" type="button" :disabled="!decideEnabled(p, 'keep_existing')" @click="decide(p, 'keep_existing')">保留既有边界</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="hint">
          规则：正式桩号优先，历史区划名称仅留痕不落图；保留既有边界时原线不动；
          红色冲突区未裁定、任一线段未核对，都不能发布。
        </p>
      </section>

      <!-- 发布操作 -->
      <section class="block publish-bar">
        <button class="btn primary" type="button" :disabled="!draft.can_publish" @click="publish(false)">④ 正式发布（同版本重算台账/待办/工程边界）</button>
        <button class="btn" type="button" :disabled="!draft.can_publish" @click="publish(true)">模拟：发布 1 段后中断</button>
        <button v-if="draft.interrupted" class="btn primary" type="button" @click="resume">恢复续发（只补未发布线段）</button>
        <button class="btn ghost danger-text" type="button" @click="discard">废弃草稿（回滚已入线段）</button>
      </section>
    </div>

    <div v-else class="empty-block">
      当前没有裁剪草稿。导入线段文件（或载入演示样例）开始「解析 → 分段核对 → 冲突裁定 → 正式发布」。
    </div>

    <!-- 同一版本重算的三张表 -->
    <div class="version-head">
      <h3>同一发布版本重算结果</h3>
      <span class="muted">正式层数据均带发布版本；悬空引用只在工程清单中标注，不进入工程边界正式层。</span>
    </div>

    <section class="block">
      <h4>路段台账（版本 {{ state.current_version }}，共 {{ state.derived.ledger.length }} 段）</h4>
      <table class="data-table">
        <thead><tr><th>路段编号</th><th>路线</th><th>路段名称</th><th>正式桩号区间</th><th>等级</th><th>管养单位</th><th>发布版本</th></tr></thead>
        <tbody>
          <tr v-for="row in state.derived.ledger" :key="row.路段编号">
            <td>{{ row.路段编号 }}</td><td>{{ row.路线 }}</td><td>{{ row.路段名称 }}</td>
            <td>{{ row.起止桩号 }}</td><td>{{ row.道路等级 }}</td><td>{{ row.管养单位 }}</td>
            <td><span class="tag" :class="{ ok: row.发布版本 === state.current_version }">{{ row.发布版本 }}</span></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="block">
      <h4>巡查待办（按同版本区间重算，共 {{ state.derived.patrol_todos.length }} 条）</h4>
      <table class="data-table">
        <thead><tr><th>巡查编号</th><th>巡查区间（正式桩号）</th><th>计划日期</th><th>责任单位</th><th>引用状态</th><th>版本</th></tr></thead>
        <tbody>
          <tr v-for="row in state.derived.patrol_todos" :key="row.巡查编号">
            <td>{{ row.巡查编号 }}</td><td>{{ row.巡查路段 }}</td><td>{{ row.巡查日期 }}</td>
            <td>{{ row.巡查人员 }}</td><td><span class="tag ok">{{ row.引用状态 }}</span></td><td>{{ row.发布版本 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="block">
      <h4>工程清单与边界引用（悬空引用不进正式层）</h4>
      <table class="data-table">
        <thead><tr><th>工程编号</th><th>工程名称</th><th>施工路段（正式桩号）</th><th>引用状态</th><th>缺失区间</th><th>版本</th></tr></thead>
        <tbody>
          <tr v-for="row in state.derived.project_refs" :key="row.工程编号" :class="{ dangling: row.引用状态 !== '正式层内' }">
            <td>{{ row.工程编号 }}</td><td>{{ row.工程名称 }}</td><td>{{ row.施工路段 }}</td>
            <td>
              <span :class="['tag', row.引用状态 === '正式层内' ? 'ok' : 'danger']">{{ row.引用状态 }}</span>
            </td>
            <td>{{ row.缺失区间 || '—' }}</td>
            <td>{{ row.发布版本 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="block">
      <h4>发布历史</h4>
      <table class="data-table">
        <thead><tr><th>版本</th><th>来源文件</th><th>发布时间</th><th>新增路段</th><th>裁定改线</th><th>台账</th><th>巡查待办</th><th>正式工程边界</th></tr></thead>
        <tbody>
          <tr v-for="v in state.versions" :key="v.version">
            <td>{{ v.version }}</td><td>{{ v.filename }}</td><td>{{ v.published_at }}</td>
            <td>{{ v.added_segments }}</td><td>{{ v.trimmed_segments }}</td>
            <td>{{ v.ledger_count }}</td><td>{{ v.patrol_count }}</td><td>{{ v.formal_bounds }}</td>
          </tr>
          <tr v-if="!state.versions.length"><td colspan="8" class="empty-state">尚未发布过新版本</td></tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

const API = '/api/clipping'

type Piece = {
  id: number
  route_code: string
  kind: 'passable' | 'conflict'
  kind_label: string
  stake_start: string
  stake_end: string
  checked: boolean
  decision: string | null
  decision_label: string
  published: boolean
  overlap_with: Array<{ id: number; code: string; name: string }>
  points?: string
  label_xy?: [number, number]
}

type Candidate = {
  id: number
  raw_line: number
  route_code: string
  name: string
  historical_name: string
  stake_start: string
  stake_end: string
  errors: string[]
  duplicate_of: string | null
}

type Draft = {
  exists: boolean
  id: number
  filename: string
  stage: string
  stage_index: number
  stages: string[]
  interrupted: boolean
  version: string | null
  candidates: Candidate[]
  pieces: Piece[]
  counts: Record<string, number>
  can_publish: boolean
}

const stages = ['解析', '分段核对', '冲突裁定', '正式发布']
const state = ref<any>({ current_version: '', versions: [], derived: { ledger: [], patrol_todos: [], project_refs: [] }, stats: {} })
const mapData = ref<any>({ routes: [], draft_pieces: [] })
const draft = computed<Draft>(() => state.value.draft ?? { exists: false })
const message = ref('')
const messageKind = ref('ok')
const selectedPiece = ref<number | null>(null)

function notify(text: string, kind: 'ok' | 'err' = 'ok') {
  message.value = text
  messageKind.value = kind
}

async function refresh() {
  const [s, m] = await Promise.all([
    request(`${API}/state`).then((r) => r.json()),
    request(`${API}/map`).then((r) => r.json()),
  ])
  state.value = s
  mapData.value = m
}

async function call(path: string, init?: RequestInit) {
  const response = await request(`${API}${path}`, init)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload.detail || `操作被拦下（${response.status}）`)
  }
  return payload
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const form = new FormData()
  form.append('file', file)
  try {
    const payload = await call('/import', { method: 'POST', body: form })
    await refresh()
    const errors = payload.parse_errors || []
    notify(
      `解析完成：${payload.batch.counts.passable} 条可通行线段、${payload.batch.counts.conflict} 个待裁切块`
      + (errors.length ? `；${errors.length} 行解析失败已在表中标注` : ''),
    )
  } catch (error) {
    await refresh()
    notify(error instanceof Error ? error.message : '导入失败', 'err')
  } finally {
    input.value = ''
  }
}

async function loadSample() {
  try {
    await call('/import/sample', { method: 'POST' })
    await refresh()
    notify('演示样例已解析，进入分段核对')
  } catch (error) {
    await refresh()
    notify(error instanceof Error ? error.message : '载入失败', 'err')
  }
}

function canCorrect(c: Candidate) {
  if (!draft.value.exists) return false
  return draft.value.stage === '分段核对' && !c.errors.length && !c.duplicate_of
}

async function correct(c: Candidate) {
  const start = window.prompt(`第 ${c.raw_line} 行正式起点桩号（如 K1+200）`, c.stake_start)
  if (!start) return
  const end = window.prompt(`第 ${c.raw_line} 行正式终点桩号（如 K3+000）`, c.stake_end)
  if (!end) return
  try {
    await call(`/candidates/${c.id}/correct`, {
      method: 'POST',
      body: JSON.stringify({ candidate_id: c.id, start_stake: start, end_stake: end }),
    })
    await refresh()
    notify('已按正式桩号重新裁剪')
  } catch (error) {
    notify(error instanceof Error ? error.message : '纠正失败', 'err')
  }
}

function checkEnabled(p: Piece) {
  if (!draft.value.exists || p.published) return false
  return draft.value.stage === '分段核对' || draft.value.stage === '冲突裁定'
}

function decideEnabled(p: Piece, decision: string) {
  if (!checkEnabled(p)) return false
  // 按正式桩号裁入时，同候选相邻可通行段必须已核对
  if (decision === 'use_formal') {
    const ordered = [...draft.value.pieces].sort((a, b) => a.id - b.id)
    const idx = ordered.findIndex((x) => x.id === p.id)
    let cur = p
    for (let i = idx + 1; i < ordered.length; i += 1) {
      const n = ordered[i]
      if (n.route_code === cur.route_code && n.stake_start === cur.stake_end && n.kind === 'passable') {
        if (!n.checked) return false
        cur = n
      } else {
        break
      }
    }
  }
  return p.checked
}

async function toggleCheck(p: Piece) {
  try {
    await call(`/pieces/${p.id}/check`, { method: 'POST', body: JSON.stringify({ checked: !p.checked }) })
    await refresh()
  } catch (error) {
    notify(error instanceof Error ? error.message : '核对失败', 'err')
  }
}

async function checkAll() {
  try {
    await call('/pieces/check-all', { method: 'POST' })
    await refresh()
    notify('全部线段已核对')
  } catch (error) {
    notify(error instanceof Error ? error.message : '操作失败', 'err')
  }
}

async function decide(p: Piece, decision: string) {
  try {
    await call(`/pieces/${p.id}/decide`, { method: 'POST', body: JSON.stringify({ decision }) })
    await refresh()
    notify(decision === 'use_formal' ? '已按正式桩号裁定（发布时裁入并保留原线余量）' : '已裁定：保留既有边界，原线不动')
  } catch (error) {
    notify(error instanceof Error ? error.message : '裁定失败', 'err')
  }
}

async function publish(simulateInterrupt: boolean) {
  try {
    const payload = await call('/publish', {
      method: 'POST',
      body: JSON.stringify({ fail_after: simulateInterrupt ? 1 : null }),
    })
    await refresh()
    notify(payload.message, payload.interrupted ? 'err' : 'ok')
  } catch (error) {
    await refresh()
    notify(error instanceof Error ? error.message : '发布失败', 'err')
  }
}

async function resume() {
  try {
    const payload = await call('/resume', { method: 'POST' })
    await refresh()
    notify(payload.message)
  } catch (error) {
    notify(error instanceof Error ? error.message : '恢复失败', 'err')
  }
}

async function discard() {
  if (!window.confirm('废弃当前裁剪草稿？中断期间已落入正式层的线段将回滚，正式层恢复原样。')) return
  try {
    const payload = await call('/discard', { method: 'POST' })
    await refresh()
    notify(payload.message)
  } catch (error) {
    notify(error instanceof Error ? error.message : '废弃失败', 'err')
  }
}

function stageClass(index: number) {
  if (!draft.value.exists) return index === 0 ? 'active' : 'todo'
  const current = draft.value.stage_index
  if (!draft.value.interrupted && draft.value.stage === '正式发布' && draft.value.can_publish && index === 3) return 'active'
  if (index < current) return 'done'
  if (index === current) return 'active'
  return 'todo'
}

function firstPoint(points: string) {
  const [x, y] = points.split(' ')[0].split(',').map(Number)
  return { x, y }
}

onMounted(refresh)
</script>

<style scoped>
.clipping-page { font-size: 13px; }
.hidden-file { display: none; }
.stat-value.sm { font-size: 15px; }
.stat-card.warn .stat-value { color: #d92d20; }

.stage-bar {
  list-style: none; display: flex; gap: 0; padding: 0; margin: 4px 0 14px;
  background: #fff; border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
}
.stage-item { display: flex; align-items: center; gap: 8px; padding: 12px 18px; flex: 1; position: relative; color: var(--muted); }
.stage-item + .stage-item::before { content: ''; position: absolute; left: 0; top: 20%; height: 60%; width: 1px; background: var(--border); }
.stage-no {
  width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  background: #e2e8f0; color: #475569; font-size: 12px; font-weight: 600;
}
.stage-item.active { background: #eff6ff; color: #1d4ed8; font-weight: 600; }
.stage-item.active .stage-no { background: #1f6feb; color: #fff; }
.stage-item.done { color: #047857; }
.stage-item.done .stage-no { background: #039855; color: #fff; }

.notice { border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; border: 1px solid; }
.notice.ok { background: #ecfdf3; border-color: #a6f4c5; color: #027a48; }
.notice.err { background: #fef3f2; border-color: #fecdca; color: #b42318; }

.map-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 14px; }
.map-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.map-head h3 { margin: 0; font-size: 14px; }
.legend { display: flex; gap: 14px; color: var(--muted); font-size: 12px; }
.lg { display: inline-block; width: 18px; height: 6px; border-radius: 3px; margin-right: 4px; vertical-align: middle; }
.lg.formal { background: #334155; }
.lg.passable { background: #f79009; }
.lg.conflict { background: repeating-linear-gradient(45deg, #d92d20 0 3px, #fee4e2 3px 6px); }
.map-svg { width: 100%; height: 360px; background: #f8fafc; border-radius: 6px; }
.map-svg polyline { cursor: default; }
.map-svg polyline[stroke="#f79009"], .map-svg polyline[stroke^="url"], .map-badge, .badge-text { cursor: pointer; }
.map-badge:hover { r: 11; }
.selected { filter: drop-shadow(0 0 4px rgba(31, 111, 235, 0.8)); }
.badge-text { fill: #fff; font-size: 10px; pointer-events: none; }
.route-label { font-size: 12px; font-weight: 700; fill: #334155; }

.draft-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; }
.draft-head { display: flex; justify-content: space-between; align-items: baseline; }
.draft-head h3 { margin: 0 0 8px; font-size: 15px; }
.draft-meta { color: var(--muted); font-size: 12px; }
.block { margin: 14px 0; }
.block h4 { margin: 0 0 8px; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
.btn.sm { padding: 3px 10px; font-size: 12px; }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.link:disabled { opacity: 0.4; cursor: not-allowed; }
.muted { color: var(--muted); }
.hint { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
.check-cell { display: inline-flex; gap: 4px; align-items: center; white-space: nowrap; }
.picked { background: #eff6ff; }
.dangling { background: #fef3f2; }
.danger-text { color: #b42318; }

.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; background: #e2e8f0; color: #334155; margin-right: 4px; font-style: normal; }
.tag.ok { background: #d1fadf; color: #027a48; }
.tag.danger { background: #fee4e2; color: #b42318; }
.tag.passable { background: #fef0c7; color: #b54708; }
.tag.conflict { background: #fee4e2; color: #b42318; }
.tag.keep_existing { background: #e0e7ff; color: #3730a3; }
.tag.use_formal { background: #d1fadf; color: #027a48; }

.publish-bar { display: flex; gap: 10px; flex-wrap: wrap; padding-top: 8px; border-top: 1px dashed var(--border); }
.empty-block { background: #fff; border: 1px dashed var(--border); border-radius: 8px; padding: 28px; text-align: center; color: var(--muted); margin-bottom: 16px; }
.version-head { display: flex; align-items: baseline; gap: 12px; margin: 18px 0 6px; }
.version-head h3 { margin: 0; font-size: 15px; }
</style>
