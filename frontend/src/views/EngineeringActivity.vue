<template>
  <div class="engineering-dashboard" aria-label="研发上库看板">
    <header class="page-head">
      <div>
        <h1>研发上库</h1>
        <p>按北京时间查看代码变更、PR 和缺陷修复。</p>
      </div>
      <div v-if="server" class="actions">
        <el-button @click="openSettings">采集设置</el-button>
        <el-button type="primary" :loading="queuing" @click="queue()">{{ collection.status === 'disabled' ? '开始采集' : '立即采集' }}</el-button>
      </div>
    </header>

    <section class="surface filters" aria-label="统计筛选">
      <label>实例<el-select v-model="server" placeholder="选择实例" aria-label="实例">
        <el-option v-for="item in servers" :key="item.id" :label="item.name" :value="item.id" />
      </el-select></label>
      <label>仓库<el-select v-model="repository" clearable filterable placeholder="全部仓库" aria-label="仓库">
        <el-option v-for="item in options.repositories" :key="item.id" :label="item.name" :value="item.id" />
      </el-select></label>
      <label>研发团队<el-select v-model="team" clearable filterable placeholder="全部贡献者" aria-label="研发团队">
        <el-option v-for="item in configuredTeams" :key="item.id" :label="teamName(item)" :value="item.id" />
      </el-select></label>
      <label>人员<el-select v-model="person" clearable filterable placeholder="全部人员" aria-label="人员">
        <el-option v-for="item in dashboard?.people || []" :key="item.id" :label="personLabel(item)" :value="item.id" />
      </el-select></label>
      <label>分支范围<el-select v-model="view" aria-label="分支范围">
        <el-option label="全部开发分支" value="development" /><el-option label="进入主线" value="main" />
      </el-select></label>
      <div class="date-filters">
        <div class="presets" aria-label="日期快捷筛选">
          <button v-for="item in presets" :key="item.key" :aria-pressed="preset === item.key"
            :class="{ selected: preset === item.key }" type="button" @click="choosePreset(item.key)">{{ item.label }}</button>
        </div>
        <el-date-picker v-model="dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期"
          end-placeholder="结束日期" range-separator="至" :clearable="false" :editable="false" aria-label="自定义日期"
          :disabled-date="date => dateKey(date) > chinaToday()" @change="preset = ''" />
      </div>
    </section>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-empty v-if="!servers.length && !loading && !error" description="请先添加 Gitea 实例" />
    <div v-if="server" class="sync-line" role="status">
      <span class="sync-state" :class="{ partial: dashboard?.coverage.partial }">{{ statusLabel }}</span>
      <span v-if="collection.finished_at">最近同步 {{ localTime(collection.finished_at) }}</span>
      <span v-if="dashboard">{{ dashboard.coverage.synced }} / {{ dashboard.coverage.repositories }} 个仓库已同步</span>
      <span v-if="collection.progress?.error">{{ collection.progress.error }}</span>
      <span v-if="dashboard?.coverage.partial">存在数据缺口，当前数值仅含已核实记录</span>
    </div>

    <details v-if="dashboard?.coverage.gaps?.length" class="surface coverage-notes">
      <summary>查看仓库采集覆盖情况</summary>
      <ul><li v-for="gap in dashboard.coverage.gaps" :key="gap.name">
        <strong>{{ gap.name }}</strong>
        <span v-if="gap.error">{{ gap.error }}</span>
        <span v-if="gap.history_before">截至 {{ gap.history_before }} 的代码事件可能不完整</span>
        <span v-if="gap.private_activity">当前凭据可能看不到私有活动</span>
        <span v-if="gap.days.length">代码采集缺口 {{ gap.days.join('、') }}</span>
      </li></ul>
    </details>
    <div v-if="dashboard" class="report" v-loading="loading">
      <div class="metrics">
        <article v-for="item in metrics" :key="item.key" class="surface metric">
          <span>{{ item.label }}</span><strong>{{ dashboard.coverage.synced ? number(dashboard.summary[item.key]) : '—' }}</strong>
          <small v-if="item.key === 'changes'">+{{ number(dashboard.summary.additions) }} / −{{ number(dashboard.summary.deletions) }}</small>
        </article>
      </div>

      <div class="overview-grid">
        <section class="surface panel">
          <header class="panel-head"><h2>每日趋势</h2>
            <el-select v-model="chartMetric" aria-label="趋势指标" class="metric-select">
              <el-option v-for="item in chartOptions" :key="item.key" :label="item.label" :value="item.key" />
            </el-select>
          </header>
          <EngineeringTrend :series="dashboard.series" :labels="chartLabels" @select="openDetails" />
        </section>
        <section class="surface panel">
          <header class="panel-head"><h2>研发 Top 10</h2>
            <el-select v-model="metric" aria-label="排行指标" class="metric-select">
              <el-option v-for="item in rankingOptions" :key="item.key" :label="item.label" :value="item.key" />
            </el-select>
          </header>
          <ol v-if="dashboard.ranking.length" class="ranking">
            <li v-for="(row, index) in dashboard.ranking" :key="row.person">
              <span class="rank-number">{{ index + 1 }}</span>
              <button type="button" class="text-button" :title="personLabel(row)" @click="person = row.person">{{ shortName(row) }}</button>
              <span class="rank-bar" :style="{ '--bar': (100 * row[metric] / rankMax) + '%' }"></span>
              <strong>{{ number(row[metric]) }}</strong>
            </li>
          </ol>
          <p v-else class="empty-note">所选指标暂无可排名的记录</p>
        </section>
      </div>

      <section class="surface panel">
        <header class="panel-head"><h2>交付效率</h2></header>
        <dl class="delivery">
          <div><dt>PR 合并耗时中位数</dt><dd>{{ mergeTime }}</dd></div>
          <div><dt>当前待合并 PR</dt><dd>{{ number(dashboard.summary.waiting) }}</dd></div>
          <div><dt>关闭未合并 PR</dt><dd>{{ number(dashboard.summary.pr_close) }}</dd></div>
          <div><dt>重开 Bug</dt><dd>{{ number(dashboard.summary.bug_reopen) }}</dd></div>
          <div><dt>Bug 待归属</dt><dd>{{ number(dashboard.summary.unassigned_bugs) }}</dd></div>
          <div><dt>普通合并提交</dt><dd>{{ number(dashboard.summary.merge_commits) }}</dd></div>
        </dl>
      </section>

      <section class="surface panel">
        <header class="panel-head"><h2>全员日报</h2></header>
        <el-table :data="dashboard.daily" style="width: 100%" empty-text="所选范围暂无已核实的活动">
          <el-table-column prop="day" label="日期" width="112" />
          <el-table-column label="研发" min-width="120">
            <template #default="{ row }"><button type="button" class="text-button" @click="openDetails(row.day, row.person)">{{ shortName(row) }}</button></template>
          </el-table-column>
          <el-table-column v-for="item in columns" :key="item.key" :label="item.label" :min-width="item.width || 86" align="right">
            <template #default="{ row }">{{ displayCell(row, item.key) }}</template>
          </el-table-column>
          <el-table-column label="涉及仓库" min-width="165" show-overflow-tooltip>
            <template #default="{ row }">{{ row.repositories.join('、') || '—' }}</template>
          </el-table-column>
          <el-table-column label="明细" width="78">
            <template #default="{ row }"><el-button link type="primary" @click="openDetails(row.day, row.person)">查看</el-button></template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="dashboard.total_rows > dashboard.page_size" v-model:current-page="page"
          background layout="prev, pager, next" :pager-count="5" :page-size="dashboard.page_size" :total="dashboard.total_rows" />
      </section>
    </div>

    <el-drawer v-model="drawer" :title="'当日明细 · ' + detailDay" size="min(760px, 100vw)" destroy-on-close>
      <div class="detail-heading">
        <span>{{ detailPerson ? personLabel(dashboard?.people.find(item => item.id === detailPerson)) : '全部人员' }}</span>
        <el-select v-model="detailKind" clearable placeholder="全部活动" aria-label="明细类型">
          <el-option label="Commit" :value="view === 'main' ? 'main_commit' : 'commit'" />
          <el-option label="新建 PR" value="pr_open" /><el-option label="已合并 PR" value="pr_merge" />
          <el-option label="关闭未合并 PR" value="pr_close" /><el-option label="已解决 Bug" value="bug" />
          <el-option label="Bug 待归属" value="bug_unassigned" /><el-option label="重开 Bug" value="bug_reopen" />
        </el-select>
      </div>
      <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" />
      <div v-loading="detailLoading" class="detail-list">
        <el-empty v-if="!details.items.length && !detailLoading" description="当天暂无已核实的匹配记录" />
        <article v-for="event in details.items" :key="event.id">
          <div class="event-title">
            <span class="kind">{{ event.merge ? '合并提交' : kindName(event.kind) }}</span>
            <a :href="event.url" target="_blank" rel="noopener noreferrer">{{ event.title || '查看 Gitea 记录' }}</a>
          </div>
          <p>{{ event.person }} · {{ event.repository }} · {{ localTime(event.time) }}</p>
          <p v-if="event.branch || event.pusher">{{ [event.branch, event.pusher && '推送人 ' + event.pusher].filter(Boolean).join(' · ') }}</p>
          <p v-if="event.lines">{{ lineSummary(event.lines) }}</p>
          <p v-if="event.reason">{{ event.reason }}<template v-if="event.pull"> · PR #{{ event.pull }}</template></p>
          <p v-else-if="event.pull">关联 PR #{{ event.pull }}</p>
          <details v-if="event.history?.length > 1" class="event-history">
            <summary>查看关闭和重开记录</summary>
            <ul><li v-for="(entry, index) in event.history" :key="index">
              {{ entry.type === 'close' ? '关闭' : '重开' }} · {{ localTime(entry.time) }} · {{ entry.person }}
            </li></ul>
          </details>
        </article>
      </div>
      <el-pagination v-if="details.total > details.page_size" v-model:current-page="detailPage"
        layout="prev, pager, next" :pager-count="5" :page-size="details.page_size" :total="details.total" />
    </el-drawer>

    <el-dialog v-model="settingsOpen" title="采集设置" width="min(590px, 96vw)">
      <div class="settings-grid">
        <label>缺陷标签<el-input v-model="settingsLabels" placeholder="bug, 缺陷" aria-label="缺陷标签" /></label>
        <label>研发团队<el-select v-model="settingsTeams" multiple filterable clearable placeholder="选择 Gitea 团队" aria-label="配置研发团队">
          <el-option v-for="item in options.teams" :key="item.id" :label="teamName(item)" :value="item.id" />
        </el-select></label>
        <label v-if="isPlatformAdmin">Manager 回调地址
          <el-input v-model="settingsCallback" aria-label="Manager 回调地址" placeholder="Gitea 可访问的 Manager 根地址" />
        </label>
        <p v-if="isPlatformAdmin" class="empty-note">设置后由 Gitea 发送缺陷关闭通知，供核对实际触发关闭的修复 PR。</p>
        <label>仓库目录规则<el-select v-model="settingsRepo" clearable filterable placeholder="选择仓库后设置" aria-label="仓库目录规则">
          <el-option v-for="item in options.repositories" :key="item.id" :label="item.name" :value="item.id" />
        </el-select></label>
        <template v-if="settingsRepo">
          <label>生成物目录（每行一个）<el-input v-model="generatedPaths" type="textarea" :rows="3" /></label>
          <label>第三方代码目录（每行一个）<el-input v-model="thirdPartyPaths" type="textarea" :rows="3" /></label>
        </template>
      </div>
      <template #footer><el-button @click="settingsOpen = false">取消</el-button>
        <el-button type="primary" :loading="queuing" @click="saveSettings">保存并重新采集</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { isPlatformAdmin, scopedServers } from '../composables/accessState'
import EngineeringTrend from '../components/EngineeringTrend.vue'

const columns = [
  { key: 'additions', label: '新增行' }, { key: 'deletions', label: '删除行' },
  { key: 'changes', label: '变更行' }, { key: 'commits', label: 'Commit' },
  { key: 'pr_open', label: '新建 PR' }, { key: 'pr_merge', label: '合并 PR' },
  { key: 'bug', label: '已解决 Bug', width: 98 },
]
const rankingOptions = columns.slice(2)
const metrics = [...rankingOptions, { key: 'active', label: '活跃研发' }]
const chartOptions = [{ key: 'code', label: '源码变更行' }, { key: 'commits', label: 'Commit' },
  { key: 'pr', label: 'PR' }, { key: 'bug', label: '缺陷修复' }]
const groups = { code: '源码', doc: '文档', generated: '生成物', third_party: '第三方', other: '其他', binary: '二进制' }
const presets = [{ key: 1, label: '今天' }, { key: -1, label: '昨天' }, { key: 7, label: '近 7 天' }, { key: 30, label: '近 30 天' }, { key: 90, label: '近 90 天' }]
const rangeFormat = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
const timeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short', hour12: false })
const numberFormat = new Intl.NumberFormat('zh-CN')
const server = ref(null)
const servers = ref([])
const repository = ref('')
const team = ref('')
const person = ref('')
const view = ref('development')
const chartMetric = ref('code')
const metric = ref('changes')
const preset = ref(30)
const dates = ref(rangeFor(30))
const page = ref(1)
const options = ref({ repositories: [], teams: [], settings: {} })
const dashboard = ref(null)
const loading = ref(true)
const queuing = ref(false)
const error = ref('')
const settingsOpen = ref(false)
const settingsLabels = ref('')
const settingsCallback = ref('')
const settingsTeams = ref([])
const settingsPaths = ref({})
const settingsRepo = ref('')
const drawer = ref(false)
const detailDay = ref('')
const detailPerson = ref('')
const detailPage = ref(1)
const detailKind = ref('')
const detailLoading = ref(false)
const detailError = ref('')
const details = ref({ items: [], total: 0 })
const collection = computed(() => dashboard.value?.collection || options.value.collection || { status: 'disabled' })
const configuredTeams = computed(() => options.value.teams.filter(item => options.value.settings.teams?.includes(item.id)))
const statusLabel = computed(() => ({ disabled: '尚未接入', queued: '等待同步', collecting: '正在同步', ready: '已同步', partial: '部分同步失败' }[collection.value.status] || '等待同步'))
const chartLabels = computed(() => ({ code: columns.slice(0, 2), commits: [columns[3]], pr: columns.slice(4, 6), bug: [columns[6]] }[chartMetric.value]))
const rankMax = computed(() => Math.max(1, ...dashboard.value.ranking.map(row => row[metric.value])))
const mergeTime = computed(() => {
  const seconds = dashboard.value.summary.median_merge_seconds
  if (seconds == null) return '—'
  if (seconds < 60) return number(Math.round(seconds)) + ' 秒'
  if (seconds < 3600) return number(Math.round(seconds / 60)) + ' 分钟'
  return number(Math.round(seconds / 360) / 10) + ' 小时'
})
const generatedPaths = pathEditor('generated')
const thirdPartyPaths = pathEditor('third_party')
let optionSequence = 0
let dashboardSequence = 0
let detailSequence = 0
let timer

function dateKey(date) {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-')
}
function chinaToday() { return rangeFormat.format(new Date()) }
function rangeFor(key) {
  const current = Date.parse(chinaToday() + 'T00:00:00Z')
  const dateFrom = offset => new Date(current - offset * 86400000).toISOString().slice(0, 10)
  return key === -1 ? [dateFrom(1), dateFrom(1)] : [dateFrom(key - 1), dateFrom(0)]
}
function choosePreset(key) { preset.value = key; dates.value = rangeFor(key) }
function number(value) { return value == null ? '—' : numberFormat.format(value) }
function localTime(value) { return timeFormat.format(new Date(value)) }
function teamName(item) { return item.organization + ' / ' + item.name }
function personLabel(item) {
  const id = item?.id || item?.person
  if (!id) return '研发'
  return id === 'unknown' ? '待归属' : item.name + ' · #' + id.split(':').at(-1)
}
function shortName(item) {
  return dashboard.value.people.filter(person => person.name === item.name).length > 1 ? personLabel(item) : item.name
}
function message(reason) { return reason.response?.data?.error || '读取研发上库数据失败' }
function path() { return '/engineering-activity/' + server.value }
function kindName(kind) {
  return ({ commit: 'Commit', main_commit: 'Commit', pr_open: '新建 PR', pr_merge: '已合并 PR',
    pr_close: '关闭未合并 PR', bug: '已解决 Bug', bug_reopen: '重开 Bug' })[kind]
}
function lineSummary(lines) {
  return Object.entries(groups).filter(([key]) => key === 'code' || lines[key].some(Boolean)).map(([key, label]) =>
    key === 'binary' ? label + ' ' + number(lines[key][0]) + ' 个' :
      label + ' +' + number(lines[key][0]) + ' / −' + number(lines[key][1])).join(' · ')
}
function displayCell(row, key) {
  const coverage = dashboard.value.series.find(item => item.day === row.day)?.coverage
  const incomplete = coverage?.roster || coverage?.[columns.slice(0, 4).some(item => item.key === key) ? 'code' : 'events']
  return row[key] || !incomplete ? number(row[key]) : '—'
}
function pathEditor(group) {
  return computed({
    get: () => settingsPaths.value[settingsRepo.value]?.[group]?.join('\n') || '',
    set: value => { (settingsPaths.value[settingsRepo.value] ||= {})[group] = value.split('\n') },
  })
}
function params(start, end, selectedPerson, selectedPage) {
  return { start, end, repository: repository.value || undefined, team: team.value || undefined,
    person: selectedPerson || undefined, view: view.value, metric: metric.value, page: selectedPage }
}
async function loadOptions() {
  const sequence = ++optionSequence
  const { data } = await api.get(path() + '/options')
  if (sequence === optionSequence) options.value = data
}
async function loadDashboard(background = false) {
  if (!server.value || !dates.value) return
  const sequence = ++dashboardSequence
  if (!background) loading.value = true
  error.value = ''
  try {
    const { data } = await api.get(path() + '/dashboard', { params: params(...dates.value, person.value, page.value) })
    if (sequence === dashboardSequence) dashboard.value = data
  } catch (reason) {
    if (sequence === dashboardSequence && reason.code !== 'ERR_CANCELED') error.value = message(reason)
  } finally {
    if (sequence === dashboardSequence) loading.value = false
  }
}
async function loadDetails() {
  if (!drawer.value) return
  const sequence = ++detailSequence
  detailLoading.value = true
  detailError.value = ''
  try {
    const { data } = await api.get(path() + '/details', { params: {
      ...params(detailDay.value, detailDay.value, detailPerson.value, detailPage.value), kind: detailKind.value,
    } })
    if (sequence === detailSequence) details.value = data
  } catch (reason) {
    if (sequence === detailSequence && reason.code !== 'ERR_CANCELED') detailError.value = message(reason)
  } finally {
    if (sequence === detailSequence) detailLoading.value = false
  }
}
function openDetails(day, who = person.value) {
  detailDay.value = day
  detailPerson.value = who
  detailKind.value = ''
  detailPage.value = 1
  details.value = { items: [], total: 0 }
  drawer.value = true
}
function openSettings() {
  settingsCallback.value = options.value.settings.webhook_base || ''
  settingsLabels.value = (options.value.settings.labels || ['bug', '缺陷']).join(', ')
  settingsTeams.value = [...(options.value.settings.teams || [])]
  settingsPaths.value = JSON.parse(JSON.stringify(options.value.settings.paths || {}))
  settingsRepo.value = ''
  settingsOpen.value = true
}
async function queue(settings) {
  queuing.value = true
  try {
    await api.post(path() + '/collect', settings ? { settings } : {})
    if (settings) {
      settingsOpen.value = false
      if (!settings.teams.includes(team.value)) team.value = ''
    }
    ElMessage.success('已加入采集队列')
    await loadOptions()
    await loadDashboard(true)
  } catch (reason) {
    if (reason.code !== 'ERR_CANCELED') ElMessage.error(message(reason))
  } finally { queuing.value = false }
}
function saveSettings() {
  const paths = Object.fromEntries(Object.entries(settingsPaths.value).map(([id, groups]) => [id,
    Object.fromEntries(Object.entries(groups).map(([name, roots]) => [name, roots.map(root => root.trim()).filter(Boolean)]))]))
  queue({ labels: settingsLabels.value.split(/[,，、]/).map(label => label.trim()).filter(Boolean), paths, teams: settingsTeams.value,
    webhook_base: isPlatformAdmin.value ? settingsCallback.value : '' })
}
watch(server, async (value) => {
  ++dashboardSequence
  dashboard.value = null
  options.value = { repositories: [], teams: [], settings: {} }
  repository.value = team.value = person.value = ''
  drawer.value = settingsOpen.value = false
  if (!value) return
  try { await loadOptions() } catch (reason) { if (reason.code !== 'ERR_CANCELED') error.value = message(reason) }
})
watch([repository, team], () => { person.value = ''; drawer.value = false })
watch([server, repository, team, person, view, dates, metric], () => {
  if (page.value === 1) loadDashboard()
  else page.value = 1
})
watch(page, () => loadDashboard())
watch(detailKind, () => { detailPage.value = 1 })
watch([drawer, detailDay, detailPerson, detailKind, detailPage], loadDetails)
onMounted(async () => {
  try {
    const { data } = await api.get('/servers')
    servers.value = scopedServers(data)
    server.value = servers.value[0]?.id || null
  } catch (reason) {
    if (reason.code !== 'ERR_CANCELED') error.value = message(reason)
  } finally { if (!server.value) loading.value = false }
  timer = window.setInterval(() => { if (!loading.value && !drawer.value && server.value) loadDashboard(true) }, 30000)
})
onUnmounted(() => { clearInterval(timer); ++optionSequence; ++dashboardSequence; ++detailSequence })
</script>

<style scoped>
.engineering-dashboard { display: grid; gap: 16px; min-width: 0; color: #192b47; }
.page-head, .panel-head, .sync-line, .actions, .detail-heading, .event-title { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.page-head, .panel-head, .detail-heading { justify-content: space-between; }
.page-head h1 { margin: 0; font-size: 28px; letter-spacing: -.025em; }
.page-head p { margin: 6px 0 0; color: #526582; font-size: 13px; }
.actions :deep(.el-button + .el-button) { margin-left: 0; }
.surface { background: rgba(255, 255, 255, .9); border: 1px solid #e3ebf5; border-radius: 14px; box-shadow: 0 5px 22px #29477809; min-width: 0; }
.filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 147px), 1fr)); gap: 12px; padding: 14px 16px; }
.filters label, .settings-grid label { display: grid; min-width: 0; gap: 6px; color: #495e7c; font-size: 12px; font-weight: 600; }
.date-filters { display: flex; align-items: center; flex-wrap: wrap; gap: 9px 15px; grid-column: 1 / -1; }
.date-filters :deep(.el-date-editor) { max-width: 100%; }
.presets { display: flex; flex-wrap: wrap; gap: 3px; background: #f0f4f9; padding: 3px; border-radius: 8px; }
.presets button { border: 0; border-radius: 6px; background: transparent; color: #4b5f7a; padding: 7px 9px; cursor: pointer; }
.presets .selected { background: white; color: #174eb7; box-shadow: 0 1px 3px #1d396b22; font-weight: 600; }
.sync-line { color: #53647c; font-size: 12px; }
.sync-state, .kind { border-radius: 6px; color: #214b90; background: #edf4ff; padding: 3px 8px; }
.sync-state.partial { color: #7c4b0a; background: #fff2da; }
.coverage-notes { padding: 10px 14px; font-size: 12px; color: #53647c; }
.coverage-notes summary { cursor: pointer; }
.coverage-notes li { margin-top: 6px; }
.coverage-notes span { margin-inline-start: 8px; }
.report { min-width: 0; display: grid; gap: 16px; }
.metrics { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 126px), 1fr)); }
.metric { padding: 14px 15px; display: grid; align-content: start; gap: 6px; font-variant-numeric: tabular-nums; }
.metric span, .metric small, .delivery dt { color: #546780; font-size: 12px; }
.metric strong { font-size: clamp(20px, 2vw, 28px); line-height: 1.15; overflow-wrap: anywhere; }
.overview-grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(275px, 2fr); gap: 16px; }
.panel { padding: 16px; }
.panel-head { margin-bottom: 13px; }
.panel h2 { font-size: 16px; margin: 0; }
.metric-select { width: 140px; }
.ranking { display: grid; gap: 8px; padding: 0; margin: 0; list-style: none; }
.ranking li { display: grid; grid-template-columns: 23px minmax(60px, 1.2fr) minmax(30px, 1fr) auto; align-items: center; gap: 8px; font-size: 12px; }
.rank-number { color: #60738a; text-align: center; }
.rank-bar { height: 6px; border-radius: 4px; background: linear-gradient(to right, #5886e5 var(--bar), #eef3f9 var(--bar)); }
.text-button { padding: 0; border: 0; background: none; color: #2052a9; cursor: pointer; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: inherit; }
.text-button:hover { text-decoration: underline; }
.empty-note, .detail-list p { color: #586c86; font-size: 13px; }
.delivery { display: grid; gap: 8px 18px; grid-template-columns: repeat(auto-fit, minmax(126px, 1fr)); margin: 0; }
.delivery dd { margin: 6px 0 0; font-size: 19px; font-weight: 650; }
.detail-list article { padding: 12px 0; border-bottom: 1px solid #e4ebf5; }
.detail-list p { margin: 5px 0 0; overflow-wrap: anywhere; }
.event-history { margin-top: 6px; font-size: 12px; color: #586c86; }
.event-title a { color: #2052a9; overflow-wrap: anywhere; }
.kind { font-size: 11px; flex: 0 0 auto; }
.detail-heading :deep(.el-select) { width: 175px; }
.settings-grid { display: grid; gap: 13px; }
:deep(.el-pagination) { justify-content: flex-end; margin-top: 12px; }
@media (max-width: 800px) { .overview-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
