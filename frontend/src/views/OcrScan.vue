<template>
  <div class="ocr-view">
    <header class="ocr-heading">
      <div>
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="home">整仓扫描</el-breadcrumb-item>
          <el-breadcrumb-item v-if="section !== 'workbench'">{{ titles[section] }}</el-breadcrumb-item>
        </el-breadcrumb>
        <h1>{{ titles[section] }}</h1>
      </div>
      <div class="ocr-actions" v-if="section === 'workbench'">
        <el-button type="primary" @click="go('new')">新建扫描</el-button>
        <el-button @click="go('new', { schedule: '1' })">新建定时计划</el-button>
        <el-button v-if="isPlatformAdmin" @click="go('models')">模型配置</el-button>
      </div>
      <el-button v-else @click="$router.push(home)">返回工作台</el-button>
    </header>

    <template v-if="section === 'workbench'">
      <ul class="ocr-metrics" aria-label="扫描任务统计" role="list">
        <li v-for="metric in workbenchMetrics" :key="metric.label" class="ocr-metric">
          <span class="ocr-metric-icon" :class="metric.tone" aria-hidden="true"><component :is="metric.icon" :size="23" /></span>
          <div class="ocr-metric-copy">
            <span>{{ metric.label }}</span>
            <strong>{{ usageNumber.format(metric.count) }}</strong>
          </div>
        </li>
      </ul>
      <section class="glass-card ocr-card ocr-workbench">
        <el-tabs v-model="listTab">
          <el-tab-pane label="扫描任务" name="tasks">
            <div class="ocr-task-toolbar">
              <el-input v-model="search" class="ocr-task-search" clearable placeholder="搜索仓库、分支或任务编号" aria-label="搜索任务">
                <template #prefix><Search :size="17" aria-hidden="true" /></template>
              </el-input>
              <el-select v-model="stateFilter" class="ocr-task-filter" placeholder="全部状态" aria-label="筛选状态">
                <el-option label="全部状态" value="" />
                <el-option v-for="(label, value) in statuses" :key="value" :label="label" :value="value" />
              </el-select>
              <el-button class="ocr-refresh" :disabled="refreshingWorkbench" :aria-busy="refreshingWorkbench" @click="refreshWorkbench">
                <RefreshCw :size="16" :class="{ 'ocr-refresh-spinning': refreshingWorkbench }" aria-hidden="true" />
                <span>刷新</span>
              </el-button>
            </div>
            <el-table :data="visibleTasks" row-key="id" empty-text="暂无扫描任务" class="ocr-table"
                      scrollbar-always-on :scrollbar-tabindex="0">
              <el-table-column prop="repository_name" label="仓库" min-width="180" class-name="ocr-repository-cell" />
              <el-table-column prop="branch" label="分支" min-width="110" />
              <el-table-column label="状态" min-width="108"><template #default="{ row }"><el-tag :type="badge(row.state)" round disable-transitions>{{ statuses[row.state] }}</el-tag></template></el-table-column>
              <el-table-column label="已保存进度" min-width="155"><template #default="{ row }">
                <span>{{ coverage(row) }}</span><el-progress v-if="row.total" :percentage="percent(row)" :show-text="false" />
              </template></el-table-column>
              <el-table-column label="已用 Token" min-width="104" class-name="ocr-numeric-cell"><template #default="{ row }">{{ usageNumber.format(row.tokens) }}</template></el-table-column>
              <el-table-column label="最近更新" min-width="158" class-name="ocr-numeric-cell"><template #default="{ row }">{{ date(row.updated) }}</template></el-table-column>
              <el-table-column label="操作" width="174" fixed="right"><template #default="{ row }">
                <el-button link type="primary" @click="go('tasks/' + row.id)">查看进度</el-button>
                <el-button v-if="row.state === 'complete'" link type="primary" @click="go('tasks/' + row.id + '/report')">查看报告</el-button>
              </template></el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="定时计划" name="plans">
            <el-table :data="plans" empty-text="暂无定时计划">
              <el-table-column label="目标" min-width="200"><template #default="{ row }">
                <div v-for="target in row.specification.targets" :key="target.repository + target.branch">
                  {{ repos.find(item => item.id === target.repository)?.repo_name || target.repository }} · {{ target.branch }}
                </div>
              </template></el-table-column>
              <el-table-column label="执行时间" min-width="210"><template #default="{ row }">
                {{ row.recurrence.frequency === 'daily' ? '每天' : weekdays[row.recurrence.weekday] }}
                {{ row.recurrence.time }} · {{ row.recurrence.timezone }}
              </template></el-table-column>
              <el-table-column label="下次执行" min-width="140"><template #default="{ row }">{{ row.enabled ? date(row.next_runs[0], row.recurrence.timezone) : '已停用' }}</template></el-table-column>
              <el-table-column label="启用" width="90"><template #default="{ row }">
                <el-switch :model-value="row.enabled" :aria-label="'启用计划 ' + row.id.slice(0, 8)" @change="setPlan(row, $event)" />
              </template></el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </section>
    </template>

    <template v-else-if="section === 'new'">
      <div class="ocr-columns">
        <section class="glass-card ocr-card">
          <h2>仓库与分支</h2>
          <el-form label-position="top">
            <el-form-item label="Gitea 实例">
              <el-select v-model="instance" placeholder="选择 Gitea 实例" aria-label="选择 Gitea 实例" @change="selected = []; invalidate()">
                <el-option v-for="row in servers" :key="row.id" :label="row.name" :value="row.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="仓库">
              <el-select v-model="selected" multiple filterable placeholder="选择已启用的仓库" aria-label="选择仓库" @change="loadBranches">
                <el-option v-for="row in repos.filter(item => item.server_id === instance && item.enabled)" :key="row.id" :label="row.repo_name" :value="row.id" />
              </el-select>
            </el-form-item>
            <el-form-item v-for="id in selected" :key="id" :label="repos.find(row => row.id === id)?.repo_name + ' 的分支'">
              <el-select v-model="branches[id]" multiple filterable aria-label="选择分支" placeholder="选择分支" @change="invalidate">
                <el-option v-for="name in branchOptions[id] || []" :key="name" :label="name" :value="name" />
              </el-select>
            </el-form-item>
          </el-form>
        </section>
        <section class="glass-card ocr-card ocr-config-card">
          <h2>模型与导出</h2>
          <el-form label-position="top" class="ocr-config-form" @submit.prevent>
            <el-form-item label="模型">
              <el-select v-model="mode" aria-label="扫描模型" class="ocr-model-picker"
                         popper-class="ocr-model-popper" @change="invalidate">
                <template #prefix>
                  <span class="ocr-model-icon"><Cpu :size="20" aria-hidden="true" /></span>
                </template>
                <template #label="{ value }">
                  <span class="ocr-model-copy">
                    <strong :title="scanModelName(modelSources[value])">{{ scanModelName(modelSources[value]) }}</strong>
                    <small>{{ modelSources[value].label }}</small>
                  </span>
                </template>
                <el-option v-for="(source, value) in modelSources" :key="value" :value="value"
                           :label="source.label + profileLabel(source.profile)"
                           :aria-label="source.label + profileLabel(source.profile)">
                  <span class="ocr-model-option">
                    <span class="ocr-model-copy">
                      <strong>{{ scanModelName(source) }}</strong>
                      <small>{{ source.label }}</small>
                    </span>
                    <Check v-if="mode === value" :size="16" aria-hidden="true" />
                  </span>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="导出格式">
              <div class="ocr-format-options" role="group" aria-label="导出格式">
                <button v-for="(name, value) in labels" :key="value" type="button" role="checkbox"
                        :aria-checked="formats.includes(value)" class="ocr-format-option" @click="toggleFormat(value)">
                  <component :is="formatIcons[value]" class="ocr-format-icon" :size="17" aria-hidden="true" />
                  <span>{{ name }}</span>
                  <Check v-if="formats.includes(value)" class="ocr-format-check" :size="15" aria-hidden="true" />
                </button>
              </div>
            </el-form-item>
            <el-form-item label="执行频率">
              <el-segmented v-model="rule.frequency" :options="frequencies" class="ocr-frequency"
                            aria-label="执行频率" block />
            </el-form-item>
            <template v-if="rule.frequency !== 'manual'">
              <el-form-item v-if="rule.frequency === 'weekly'" label="星期">
                <el-select v-model="rule.weekday" aria-label="星期"><el-option v-for="(name, index) in weekdays" :key="name" :label="name" :value="index" /></el-select>
              </el-form-item>
              <el-form-item label="本地时间"><input v-model="rule.time" type="time" class="ocr-input" aria-label="定时执行时间"></el-form-item>
              <el-form-item label="时区"><el-input v-model="rule.timezone" aria-label="时区" /></el-form-item>
              <el-checkbox v-model="rule.force">即使无变化也重新扫描</el-checkbox>
              <ul v-if="nextRuns.length" class="ocr-times"><li v-for="value in nextRuns" :key="value">{{ date(value, rule.timezone) }}</li></ul>
            </template>
          </el-form>
        </section>
      </div>
      <section class="glass-card ocr-card" aria-labelledby="ocr-scope-heading">
        <div class="ocr-row ocr-scope-heading">
          <h2 id="ocr-scope-heading">文件范围预览</h2>
          <div class="ocr-actions">
            <el-button :disabled="!canSubmit" :loading="busy" @click="preview">预览范围</el-button>
            <el-button v-if="rule.frequency === 'manual'" type="primary" :disabled="!previewReady" :loading="busy" @click="start">立即扫描</el-button>
            <el-button v-else type="primary" :disabled="!canSubmit || !nextRuns.length" :loading="busy" @click="savePlan">保存计划</el-button>
          </div>
        </div>
        <el-empty v-if="!previewItems.length" description="先选择仓库和分支，再预览范围" :image-size="48" />
        <details v-for="row in previewItems" :key="row.id" open class="ocr-scope-target">
          <summary>
            <span class="ocr-scope-summary">
              <strong>{{ row.repository_name }}</strong><el-tag size="small" effect="plain">{{ row.branch }}</el-tag>
              <el-tag size="small" :type="badge(row.state)">{{ statuses[row.state] }}</el-tag>
              <el-tag v-if="row.total !== null" size="small" type="success">{{ row.total }} 个候选</el-tag>
              <el-tag v-if="row.total !== null" size="small" type="info">{{ row.excluded }} 个排除</el-tag>
            </span>
          </summary>
          <div class="ocr-scope-body">
            <div v-if="row.sha" class="ocr-scope-sha"><span>固定提交</span><code>{{ row.sha }}</code></div>
            <div v-if="row.max_input_tokens" class="ocr-scope-sha">
              <span>OCR 输入上限</span><strong>{{ usageNumber.format(row.max_input_tokens) }} Token</strong>
            </div>
            <el-alert v-if="row.error" :title="row.error" type="warning" :closable="false" />
            <OcrFileScope v-if="row.scope?.length" :files="row.scope" />
          </div>
        </details>
      </section>
    </template>

    <template v-else-if="section === 'models'">
      <div class="ocr-columns">
        <section class="glass-card ocr-card">
          <h2>连接配置</h2>
          <el-form label-position="top" @submit.prevent>
            <el-form-item label="配置方式">
              <el-radio-group v-model="mode" @change="tested = null"><el-radio value="inherit">继承 AI Review</el-radio><el-radio value="custom">OCR 专用</el-radio></el-radio-group>
            </el-form-item>
            <p v-if="mode === 'inherit'">当前模型{{ profileLabel('inherited') || '尚未配置' }}</p>
            <template v-else>
              <el-form-item label="API 端点"><el-input v-model="modelForm.api_base_url" aria-label="API 端点" placeholder="https://api.example.com/v1" /></el-form-item>
              <el-form-item label="模型 ID"><el-input v-model="modelForm.model" aria-label="模型 ID" /></el-form-item>
              <el-form-item label="API Key"><el-input v-model="modelForm.api_key" type="password" autocomplete="new-password" aria-label="API Key" :placeholder="profileData.custom?.configured ? '已保存，留空保留' : '输入密钥'" /></el-form-item>
            </template>
            <el-form-item label="OCR 输入上限（Token）">
              <el-input-number v-model="modelLimits[mode]" :min="1" :max="Number.MAX_SAFE_INTEGER"
                               :precision="0" :controls="false" aria-label="OCR 输入上限" />
              <span class="ocr-input-limit-help">按模型上下文填写。OCR 用此值的 80% 筛选单文件。</span>
            </el-form-item>
            <div class="ocr-actions">
              <el-button :loading="busy" @click="testModel">测试连接</el-button>
              <el-button type="primary" :loading="busy" :disabled="!validModelLimit" @click="saveModel">保存配置</el-button>
            </div>
          </el-form>
        </section>
        <section class="glass-card ocr-card">
          <h2>连接与能力验证</h2>
          <el-alert v-if="tested" title="模型工具调用验证通过" type="success" :closable="false" />
          <p v-if="tested">{{ tested.model }} · {{ tested.milliseconds }} 毫秒</p>
          <p v-else>测试会向选中的模型发送一次工具调用请求。</p>
        </section>
      </div>
    </template>

    <template v-else-if="section === 'progress' && task">
      <section class="glass-card ocr-card ocr-progress-summary" aria-label="扫描概览">
        <div class="ocr-progress-header">
          <div class="ocr-progress-identity">
            <div class="ocr-progress-repository">
              <h2>{{ task.repository_name }}</h2><span class="ocr-progress-branch">{{ task.branch }}</span>
            </div>
            <div class="ocr-progress-commit">
              <span>固定提交</span><code>{{ task.sha || '正在固定提交' }}</code>
            </div>
          </div>
          <div class="ocr-actions ocr-progress-controls">
            <el-tag :type="badge(task.state)" round disable-transitions>{{ statuses[task.state] }}</el-tag>
            <el-button v-if="task.state === 'complete'" type="primary" @click="go('tasks/' + task.id + '/report')">查看报告</el-button>
            <el-button v-if="task.reused" @click="go('tasks/' + task.reused)">查看已有任务</el-button>
            <el-button v-if="stoppable.includes(task.state)" type="danger" plain @click="confirmStop = true">终止扫描</el-button>
            <el-button v-if="task.state === 'attention' && task.total !== null" @click="resume">继续扫描</el-button>
          </div>
        </div>
        <div class="ocr-progress">
          <div class="ocr-progress-line">
            <h3>文件进度</h3>
            <div class="ocr-progress-count">
              <span>{{ task.total === null ? '待枚举文件' : fileRatio(task) + ' 个文件' }}</span>
              <strong v-if="task.total">{{ percent(task) }}%</strong>
            </div>
          </div>
          <el-progress v-if="task.total" :percentage="percent(task)" :show-text="false" :stroke-width="8" aria-label="文件进度" />
        </div>
        <ul class="ocr-metrics ocr-progress-metrics" aria-label="扫描文件及用量统计" role="list">
          <li v-for="metric in progressMetrics" :key="metric.label" class="ocr-metric" :class="metric.className">
            <span class="ocr-metric-icon" :class="metric.tone" aria-hidden="true"><component :is="metric.icon" :size="20" /></span>
            <div class="ocr-metric-copy"><span>{{ metric.label }}</span><strong>{{ usageNumber.format(metric.count) }}</strong></div>
          </li>
        </ul>
        <section v-if="scanActive && task.retry_at" class="ocr-retry-card" aria-label="扫描重试">
          <div class="ocr-activity-main">
            <span class="ocr-activity-icon ocr-retry-icon" aria-hidden="true"><CircleAlert :size="21" /></span>
            <div class="ocr-activity-copy">
              <div class="ocr-retry-heading">
                <h3 aria-live="polite">{{ retrySeconds ? '等待模型恢复' : '正在重新连接' }}</h3>
                <span v-if="task.http" class="ocr-retry-http">HTTP {{ task.http }}</span>
              </div>
              <div class="ocr-retry-meta">
                <span>已保存的扫描进度会保留</span>
                <span v-if="task.retries">第 {{ task.retries }} 次重试</span>
              </div>
            </div>
          </div>
          <div v-if="retrySeconds" class="ocr-retry-clock">
            <Clock3 :size="17" aria-hidden="true" />
            <span>下次重连</span><strong role="timer">{{ retryClock }}</strong>
          </div>
        </section>
        <el-alert v-else-if="task.error" :title="task.error" type="warning" :closable="false" />
        <section v-if="currentActivity" class="ocr-current-card ocr-activity-main" aria-label="当前扫描对象">
          <span class="ocr-activity-icon ocr-current-icon" aria-hidden="true">
            <FileText v-if="currentActivity.stage" :size="20" /><FileCode v-else :size="20" />
          </span>
          <div class="ocr-activity-copy">
            <span class="ocr-current-label">{{ currentActivity.stage ? '当前阶段' : '当前文件' }}</span>
            <strong v-if="currentActivity.stage" class="ocr-current-stage">{{ currentActivity.stage }}</strong>
            <code v-else class="ocr-current-path"><span>{{ currentActivity.directory }}</span><wbr><strong>{{ currentActivity.filename }}</strong></code>
          </div>
        </section>
        <p v-if="task.state === 'stopped'" class="ocr-progress-note">终止人 {{ task.stop_actor || '管理员' }} · {{ date(task.stopped_at) }}</p>
      </section>
      <div class="ocr-columns">
        <section class="glass-card ocr-card ocr-batch" aria-labelledby="ocr-batch-heading">
          <div class="ocr-row ocr-batch-heading">
            <h2 id="ocr-batch-heading">批次目标</h2><span>{{ batchStatus.done }} / {{ siblings.length }} 个目标完成</span>
          </div>
          <div v-if="batchStatus.total" class="ocr-batch-progress">
            <span>已保存 {{ fileRatio(batchStatus) }} 个文件</span>
            <el-progress :percentage="percent(batchStatus)" :show-text="false" aria-label="批次文件保存进度" />
          </div>
          <el-table :data="siblings" row-key="id" empty-text="暂无批次目标" class="ocr-batch-table"
                    scrollbar-always-on :scrollbar-tabindex="0">
            <el-table-column prop="repository_name" label="仓库" min-width="112" />
            <el-table-column prop="branch" label="分支" min-width="72" />
            <el-table-column label="状态与进度" min-width="198"><template #default="{ row }">
              <div class="ocr-batch-state">
                <el-tag :type="badge(row.state)" size="small" round disable-transitions>{{ statuses[row.state] }}</el-tag>
                <span>{{ row.total === null ? '待枚举文件' : fileRatio(row) }}</span>
              </div>
            </template></el-table-column>
            <el-table-column label="操作" width="66" fixed="right"><template #default="{ row }">
              <el-button link @click="go('tasks/' + row.id)">查看</el-button>
            </template></el-table-column>
          </el-table>
        </section>
        <section class="glass-card ocr-card ocr-timeline" aria-labelledby="ocr-timeline-heading">
          <h2 id="ocr-timeline-heading">事件时间线</h2>
          <OcrEventTimeline :events="timeline" />
        </section>
      </div>
      <el-dialog v-model="confirmStop" title="确认终止扫描" width="min(92vw, 500px)">
        <p>{{ task.repository_name }} · {{ task.branch }}</p>
        <p>本次扫描进度、发现和报告将被清除，下次需要从头扫描。</p>
        <template #footer><div class="ocr-actions ocr-end">
          <el-button @click="confirmStop = false">返回</el-button><el-button type="danger" :loading="busy" @click="stop">确认终止</el-button>
        </div></template>
      </el-dialog>
    </template>

    <OcrScanReport v-else-if="section === 'report' && report" :key="task.id" :report="report"
                   :export-formats="reportExports" :busy="busy" @download="download" />
    <el-alert v-if="connection" :title="connection" type="warning" :closable="false" />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { CalendarDays, Check, CircleAlert, CircleX, Clock3, Coins, Cpu, FileBraces, FileCode, FileText, Layers, Play, RefreshCw, Search, ShieldCheck } from '@lucide/vue'
import { api } from '../api'
import OcrEventTimeline from '../components/OcrEventTimeline.vue'
import OcrFileScope from '../components/OcrFileScope.vue'
import OcrScanReport from '../components/OcrScanReport.vue'
import { isPlatformAdmin } from '../composables/accessState.js'

const home = '/ai-reviews/ocr'
const route = useRoute(), router = useRouter()
const urls = '/ai-review/ocr'
const section = computed(() => route.meta.ocrSection || 'workbench')
const titles = { workbench: '整仓扫描', new: '扫描配置', models: '模型配置', progress: '扫描进度', report: '扫描报告' }
const statuses = { previewing: '预览中', previewed: '待启动', queued: '排队中', running: '扫描中', retry: '等待模型', exporting: '生成报告', complete: '已完成', attention: '待处理', stopping: '正在终止', stopped: '已终止', skipped: '已跳过' }
const labels = { html: 'HTML', md: 'Markdown', json: 'JSON', txt: 'TXT', sarif: 'SARIF', native: 'OCR 原生 HTML' }
const formatIcons = { html: FileCode, md: FileText, json: FileBraces, txt: FileText, sarif: ShieldCheck, native: FileCode }
const modelSources = {
  inherit: { label: '继承 AI Review', profile: 'inherited' },
  custom: { label: 'OCR 专用', profile: 'custom' },
}
const frequencies = [
  { label: '手动', value: 'manual' },
  { label: '每天', value: 'daily' },
  { label: '每周', value: 'weekly' },
]
const usageNumber = new Intl.NumberFormat('zh-CN')
const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const activeStates = ['previewing', 'queued', 'running', 'retry', 'exporting', 'stopping']
const stoppable = [...activeStates.filter(value => value !== 'stopping'), 'attention', 'previewed']
const connection = ref(''), busy = ref(false), serverClockOffset = ref(0), seconds = ref(Date.now() / 1000)
const tasks = ref([]), plans = ref([]), repos = ref([]), servers = ref([])
const listTab = ref('tasks'), search = ref(''), stateFilter = ref('')
const refreshingWorkbench = ref(false)
const selected = ref([]), instance = ref(null), branches = reactive({}), branchOptions = reactive({})
const mode = ref('inherit'), formats = ref(['html'])
const rule = reactive({ frequency: 'manual', weekday: 0, time: '09:00', timezone: 'Asia/Shanghai', force: false })
const nextRuns = ref([]), profileData = ref({}), modelForm = reactive({ api_base_url: '', model: '', api_key: '' })
const modelLimits = reactive({ inherit: null, custom: null })
const validModelLimit = computed(() => Number.isSafeInteger(modelLimits[mode.value]) && modelLimits[mode.value] > 0)
const tested = ref(null), previewBatch = ref(''), previewItems = ref([])
const task = ref(null), report = ref(null), siblings = ref([]), timeline = ref([]), confirmStop = ref(false)

const go = (path, query = {}) => router.push({ path: home + '/' + path, query })
const badge = state => ({ complete: 'success', retry: 'warning', attention: 'warning', stopped: 'info' }[state] || 'primary')
const date = (value, timeZone) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false, timeZone }) : '—'
const coverage = row => row.total === null ? '待枚举文件' : `已完成 ${row.completed}/${row.total}`
const fileRatio = row => `${usageNumber.format(row.completed)} / ${usageNumber.format(row.total)}`
const percent = row => Math.min(100, Math.floor(100 * row.completed / row.total))
const profileLabel = name => profileData.value[name]?.model ? ' · ' + profileData.value[name].model : ''
const scanModelName = source => profileData.value[source.profile]?.model || '尚未配置'
const targets = computed(() => selected.value.flatMap(repository => (branches[repository] || []).map(branch => ({ repository, branch }))))
const canSubmit = computed(() => targets.value.length && targets.value.length <= 50 && formats.value.length)
const previewReady = computed(() => previewItems.value.some(row => row.state === 'previewed') && previewItems.value.every(row => ['previewed','attention'].includes(row.state)))
const visibleTasks = computed(() => tasks.value.filter(row => (!stateFilter.value || row.state === stateFilter.value) &&
  [row.repository_name, row.branch, row.id].some(value => value.toLowerCase().includes(search.value.toLowerCase()))))
const workbenchMetrics = computed(() => [
  { label: '运行或等待', count: tasks.value.filter(row => activeStates.includes(row.state)).length, icon: Play, tone: 'is-running' },
  { label: '已完成', count: tasks.value.filter(row => row.state === 'complete').length, icon: Check, tone: 'is-complete' },
  { label: '已启用计划', count: plans.value.filter(row => row.enabled).length, icon: CalendarDays, tone: 'is-scheduled' },
])
const progressMetrics = computed(() => task.value ? [
  { label: '已排除', count: task.value.excluded, icon: Layers, tone: 'is-scheduled' },
  { label: '待处理', count: Math.max(0, (task.value.total || 0) - task.value.completed), icon: Clock3, tone: 'is-running' },
  { label: '文件失败', count: task.value.failures, icon: CircleX, tone: 'is-failure' },
  { label: '已用 Token', count: task.value.tokens, icon: Coins, tone: 'is-complete', className: 'ocr-usage-metric' },
] : [])
const batchStatus = computed(() => ({
  done: siblings.value.filter(row => ['complete', 'skipped'].includes(row.state)).length,
  total: siblings.value.reduce((total, row) => total + (row.total || 0), 0),
  completed: siblings.value.reduce((total, row) => total + row.completed, 0),
}))
const scanActive = computed(() => ['running', 'retry'].includes(task.value?.state))
const retrySeconds = computed(() => Math.max(0, Math.ceil((task.value?.retry_at || 0) - seconds.value - serverClockOffset.value)))
const retryClock = computed(() => {
  const left = retrySeconds.value
  if (left < 60) return `${left} 秒`
  const minutes = Math.floor(left / 60)
  const hours = minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ` : ''
  return `${hours}${minutes % 60} 分 ${String(left % 60).padStart(2, '0')} 秒`
})
const currentActivity = computed(() => {
  const path = task.value?.current
  if (!scanActive.value || !path) return null
  if (path === '__scan_project_summary__') return { stage: '正在生成 OCR 汇总' }
  if (/^__scan_dedup_batch_\d+__$/.test(path)) return { stage: '正在合并重复发现' }
  const filenameStart = path.lastIndexOf('/') + 1
  return { directory: path.slice(0, filenameStart), filename: path.slice(filenameStart) }
})
const reportExports = computed(() => ['md', 'json', 'txt', 'sarif', 'native'].map(value => ({
  value, label: labels[value], icon: formatIcons[value],
  available: ['md', 'json'].includes(value) || (report.value?.total > 0 && task.value?.formats.includes(value)),
})))
const configuration = () => ({ targets: targets.value, mode: mode.value, formats: formats.value })
function invalidate() { previewBatch.value = ''; previewItems.value = [] }
function toggleFormat(value) {
  formats.value = formats.value.includes(value)
    ? formats.value.filter(format => format !== value)
    : [...formats.value, value]
  invalidate()
}
async function loadBranches() {
  invalidate()
  for (const id of selected.value) {
    if (branchOptions[id]) continue
    const { data } = await api.get(urls + '/repositories/' + id + '/branches')
    branchOptions[id] = data.branches
    branches[id] = data.default_branch ? [data.default_branch] : []
  }
}
async function action(fn) {
  busy.value = true
  try { await fn() } catch (error) { if (error.code !== 'ERR_CANCELED') connection.value = error.response?.data?.error || '操作失败，请重试' }
  finally { busy.value = false }
}
const preview = () => action(async () => {
  const { data } = await api.post(urls + '/previews', configuration())
  previewBatch.value = data.batch
  await refresh()
})
const start = () => action(async () => {
  await api.post(urls + '/batches/' + previewBatch.value + '/start', {})
  await go('tasks/' + previewItems.value.find(row => row.state === 'previewed').id)
})
const savePlan = () => action(async () => {
  await api.post(urls + '/plans', { ...configuration(), recurrence: rule })
  listTab.value = 'plans'
  await router.push(home)
  ElMessage.success('定时计划已保存')
})
const setPlan = (row, enabled) => action(async () => {
  await api.patch(urls + '/plans/' + row.id, { enabled })
  await refresh()
})
const saveModel = () => action(async () => {
  const source = mode.value
  const config = source === 'custom' ? { ...modelForm } : {}
  const { data } = await api.put(urls + '/profiles', {
    ...config, mode: source, max_input_tokens: modelLimits[source]
  })
  profileData.value = data
  modelLimits[source] = data[modelSources[source].profile].max_input_tokens
  if (source === 'custom') modelForm.api_key = ''
  tested.value = null
  ElMessage.success('模型配置已保存')
})
const testModel = () => action(async () => {
  const { data } = await api.post(urls + '/profiles/test', { mode: mode.value })
  tested.value = data
})
const stop = () => action(async () => {
  await api.post(urls + '/tasks/' + task.value.id + '/stop', { confirm: true })
  confirmStop.value = false
  await refresh()
})
const resume = () => action(async () => {
  await api.post(urls + '/tasks/' + task.value.id + '/resume', {})
  await refresh()
})
const download = fmt => action(async () => {
  const { id, sha } = task.value
  const response = await api.get(urls + '/tasks/' + id + '/download/' + fmt, { responseType: 'blob' })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = 'ocr-' + sha.slice(0, 12) + (fmt === 'native' ? '-native.html' : '.' + fmt)
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
})

let pendingRefresh, routeEpoch = 0, timer
function refresh() {
  pendingRefresh ??= loadPage().finally(() => { pendingRefresh = null })
  return pendingRefresh
}
async function refreshWorkbench() {
  refreshingWorkbench.value = true
  try { await refresh() } finally { refreshingWorkbench.value = false }
}
async function loadPage() {
  const epoch = routeEpoch
  const path = section.value
  const ident = route.params.task
  const batch = previewBatch.value
  const opts = { notifyError: false }
  try {
    if (path === 'workbench') {
      const [history, schedules] = await Promise.all([api.get(urls + '/tasks', opts), api.get(urls + '/plans', opts)])
      if (epoch !== routeEpoch) return
      tasks.value = history.data.items
      plans.value = schedules.data.items
    } else if (path === 'new' && batch) {
      const { data } = await api.get(urls + '/batches/' + batch, opts)
      if (epoch !== routeEpoch || previewBatch.value !== batch) return
      previewItems.value = data.items
    } else if (path === 'progress' || (path === 'report' && !report.value)) {
      const cursor = timeline.value.at(-1)?.id || 0
      const { data } = await api.get(urls + '/tasks/' + ident, { ...opts, params: { after: cursor } })
      if (epoch !== routeEpoch) return
      task.value = data.task
      if (data.task.state === 'stopped' || data.events.length) {
        timeline.value = data.task.state === 'stopped' ? [] : [...timeline.value, ...data.events]
      }
      serverClockOffset.value = data.now - Date.now() / 1000
      if (path === 'report') {
        const output = await api.get(urls + '/tasks/' + ident + '/report', opts)
        if (epoch === routeEpoch) report.value = output.data
      } else {
        const result = await api.get(urls + '/batches/' + task.value.batch, opts)
        if (epoch === routeEpoch) siblings.value = result.data.items
      }
    }
    if (epoch === routeEpoch) connection.value = ''
  } catch (error) {
    if (epoch === routeEpoch && error.code !== 'ERR_CANCELED') connection.value = error.response?.data?.error || '连接暂时中断，正在重试'
  }
}
async function setup() {
  const epoch = ++routeEpoch
  search.value = ''; task.value = null; report.value = null; timeline.value = []; siblings.value = []; confirmStop.value = false
  if (section.value === 'new') rule.frequency = route.query.schedule === '1' ? 'daily' : 'manual'
  if (['workbench', 'new', 'models'].includes(section.value)) {
    try {
      const [repositoryData, serverData, profiles] = await Promise.all([
        api.get('/ai-review/repositories'), api.get('/ai-review/servers'), api.get(urls + '/profiles')
      ])
      if (epoch !== routeEpoch) return
      repos.value = repositoryData.data
      servers.value = serverData.data
      profileData.value = profiles.data
      modelLimits.inherit = profiles.data.inherited.max_input_tokens
      modelLimits.custom = profiles.data.custom.max_input_tokens
      instance.value ??= servers.value[0]?.id
      Object.assign(modelForm, { api_base_url: profiles.data.custom.api_base_url, model: profiles.data.custom.model, api_key: '' })
    } catch (error) { if (epoch === routeEpoch) connection.value = error.response?.data?.error || '无法加载扫描配置' }
  }
  await refresh()
}
watch(() => [rule.frequency, rule.weekday, rule.time, rule.timezone, rule.force], async () => {
  nextRuns.value = []
  if (rule.frequency === 'manual') return
  const configured = { ...rule }
  try {
    const { data } = await api.post(urls + '/plans/preview', configured, { notifyError: false })
    if (Object.entries(configured).every(([key, value]) => rule[key] === value)) nextRuns.value = data.next_runs
  } catch { nextRuns.value = [] }
})
watch(() => route.fullPath, setup)
onMounted(() => { setup(); timer = setInterval(() => { seconds.value = Date.now() / 1000; refresh() }, 1000) })
onUnmounted(() => { ++routeEpoch; clearInterval(timer) })
</script>

<style scoped>
.ocr-view { display: grid; gap: 16px; min-width: 0; color: var(--el-text-color-primary); }
.ocr-heading, .ocr-row, .ocr-actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; min-width: 0; }
.ocr-heading, .ocr-row { justify-content: space-between; }
.ocr-heading h1 { font-size: 26px; margin: 10px 0 0; }
.ocr-heading .el-breadcrumb { line-height: 1.5; }
.ocr-card { padding: 18px; min-width: 0; }
.ocr-card h2 { font-size: 17px; margin: 0 0 16px; }
.ocr-actions .el-button + .el-button { margin-left: 0; }
.ocr-actions > .el-input, .ocr-actions > .el-select { width: min(100%, 250px); }
.ocr-scope-heading h2, .ocr-columns .el-form-item:last-child { margin-bottom: 0; }
.ocr-columns { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.ocr-config-card {
  --ocr-control-border: #cbd7e6;
  --ocr-control-blue: #1553aa;
  --ocr-control-active: #edf5ff;
}
.ocr-config-form { container-type: inline-size; }
.ocr-input-limit-help { flex-basis: 100%; color: #52637c; font-size: 13px; line-height: 1.5; margin-top: 6px; }
.ocr-config-form :deep(.el-form-item) { margin-bottom: 12px; }
.ocr-config-form :deep(.el-form-item:last-child) { margin-bottom: 0; }
.ocr-config-form :deep(.el-form-item__label) {
  margin-bottom: 5px;
  font-weight: 600 !important;
  color: #34465e !important;
  line-height: 1.35;
}
.ocr-model-picker :deep(.el-select__wrapper) {
  min-height: 54px;
  padding: 7px 12px;
  gap: 10px;
  border-radius: 10px;
  background: var(--ocr-control-active);
  box-shadow: inset 0 0 0 1px #9abbeb;
}
.ocr-model-picker :deep(.el-select__wrapper:hover),
.ocr-model-picker :deep(.el-select__wrapper.is-focused) { box-shadow: inset 0 0 0 1px #2b6dcc; }
.ocr-model-picker :deep(.el-select__caret) { color: var(--ocr-control-blue); }
.ocr-model-picker :deep(.el-select__placeholder) { position: static; transform: none; }
.ocr-model-icon {
  display: inline-grid;
  flex: 0 0 36px;
  width: 36px;
  height: 36px;
  place-items: center;
  border-radius: 8px;
  color: var(--ocr-control-blue);
  background: #dbeaff;
}
.ocr-model-copy { display: grid; line-height: 1.35; min-width: 0; }
.ocr-model-copy strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #142641;
  font-size: 14px;
  font-weight: 650;
}
.ocr-model-copy small { color: #465b76; font-size: 12px; }
.ocr-model-option { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 10px; }
.ocr-model-option > svg { flex: 0 0 auto; color: #1553aa; }
:global(.ocr-model-popper .el-select-dropdown__item) { display: flex; align-items: center; height: 48px; }
:global(.ocr-model-popper .el-select-dropdown__item.is-selected::after) { content: none; }
.ocr-format-option {
  display: inline-flex;
  align-items: center;
  color: #263b58;
  border: 1px solid var(--ocr-control-border);
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
  font: inherit;
}
.ocr-format-option:hover { border-color: #7da4d9; background: #f3f8ff; }
.ocr-format-option:focus-visible { outline: 2px solid #2b6dcc; outline-offset: 2px; }
.ocr-format-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
  width: 100%;
}
@container (max-width: 405px) {
  .ocr-format-options { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (max-width: 268px) {
  .ocr-format-options { grid-template-columns: minmax(0, 1fr); }
}
.ocr-format-option {
  min-height: 42px;
  min-width: 0;
  gap: 6px;
  padding: 6px 8px;
  font-size: 13px;
  font-weight: 550;
  line-height: 1.3;
  text-align: start;
}
.ocr-format-option span { min-width: 0; overflow-wrap: anywhere; }
.ocr-format-option svg { flex: 0 0 auto; }
.ocr-format-icon { color: #516887; }
.ocr-format-check { margin-inline-start: auto; }
.ocr-format-option[aria-checked="true"] { border-color: #7da8ea; color: #14447f; background: var(--ocr-control-active); }
.ocr-format-option[aria-checked="true"] svg { color: var(--ocr-control-blue); }
.ocr-frequency {
  --el-segmented-bg-color: #e7edf5;
  --el-segmented-item-selected-bg-color: #fff;
  --el-segmented-item-selected-color: var(--ocr-control-blue);
  --el-segmented-item-hover-bg-color: #dce7f5;
  --el-segmented-item-hover-color: #233c5c;
  --el-segmented-color: #465b76;
  --el-border-radius-base: 8px;
  width: 100%;
  font-weight: 600;
}
.ocr-frequency :deep(.el-segmented__item-selected) { box-shadow: 0 1px 3px #14366226; }
.ocr-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 0;
  padding: 12px 0;
  border: 1px solid #d4e0ee;
  border-radius: 14px;
  background: #f5f9fe;
  list-style: none;
}
.ocr-metric { display: flex; gap: 16px; align-items: center; min-width: 0; padding: 0 clamp(12px, 2.4vw, 32px); }
.ocr-metric + .ocr-metric { border-inline-start: 1px solid #dbe4ef; }
.ocr-metric-icon { display: grid; place-items: center; flex: 0 0 44px; height: 44px; border-radius: 12px; }
.ocr-metric-icon.is-running { color: #1557b4; background: #dceafd; }
.ocr-metric-icon.is-complete { color: #14723f; background: #dcf2e5; }
.ocr-metric-icon.is-scheduled { color: #6840b8; background: #ebe3fb; }
.ocr-metric-icon.is-failure { color: #a72541; background: #f8dfe7; }
.ocr-metric-copy { min-width: 0; }
.ocr-metric-copy span { color: #2d3e55; font-size: 14px; font-weight: 650; line-height: 1.35; }
.ocr-metric-copy strong {
  display: block;
  margin-top: 3px;
  color: #142239;
  font-size: 27px;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
  line-height: 1.06;
  overflow-wrap: anywhere;
}
.ocr-workbench { container-type: inline-size; }
.ocr-workbench :deep(.el-tabs__item) { color: #34455d; font-weight: 650; }
.ocr-workbench :deep(.el-tabs__item.is-active), .ocr-workbench :deep(.el-tabs__item:hover) { color: #1553aa; }
.ocr-workbench :deep(.el-tabs__nav-wrap::after) { height: 1px; background: #dbe3ee; }
.ocr-task-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 360px) minmax(126px, 170px) max-content;
  align-items: center;
  gap: 10px;
}
.ocr-task-search, .ocr-task-filter { width: 100%; min-width: 0; }
.ocr-task-search :deep(.el-input__wrapper), .ocr-task-filter :deep(.el-select__wrapper) {
  min-height: 38px;
  padding: 0 11px;
  border-radius: 8px !important;
  background: #fff !important;
  box-shadow: inset 0 0 0 1px #cbd8e8 !important;
}
.ocr-task-search :deep(.el-input__wrapper:hover), .ocr-task-filter :deep(.el-select__wrapper:hover) {
  box-shadow: inset 0 0 0 1px #8199b8 !important;
}
.ocr-task-search :deep(.el-input__wrapper.is-focus), .ocr-task-filter :deep(.el-select__wrapper.is-focused) {
  box-shadow: inset 0 0 0 1px #2864ba, 0 0 0 2px #dbe9fc !important;
}
.ocr-task-search :deep(.el-input__prefix-inner > svg) { flex: 0 0 auto; margin-inline-end: 8px; color: #455b77; }
.ocr-task-search :deep(.el-input__inner), .ocr-task-filter :deep(.el-select__placeholder) { color: #25364d; font-weight: 550; }
.ocr-task-search :deep(.el-input__inner::placeholder) { color: #4a5e78; font-weight: 400; }
.ocr-task-filter :deep(.el-select__caret) { color: #455b77; }
.ocr-refresh {
  min-height: 38px;
  padding: 0 12px;
  border: 1px solid #cbd8e8;
  border-radius: 8px;
  color: #25364d;
  background: #fff;
  font-weight: 650;
}
.ocr-refresh > span { display: inline-flex; align-items: center; gap: 7px; }
.ocr-refresh:not(:disabled):hover { color: #1553aa; border-color: #8199b8; background: #f1f6fe; }
.ocr-refresh:focus-visible { outline: 2px solid #2864ba; outline-offset: 2px; }
.ocr-refresh:disabled { color: #455b77; border-color: #cbd8e8; background: #f1f6fc; }
.ocr-refresh-spinning { animation: ocr-refresh-spin 850ms linear infinite; }
@keyframes ocr-refresh-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .ocr-refresh-spinning { animation: none; } }
.ocr-workbench :deep(.el-table) { color: #293c55; --el-table-row-hover-bg-color: #f1f6fd; }
.ocr-workbench :deep(.el-table th.el-table__cell) {
  color: #293c55 !important;
  background: #eaf0f7 !important;
  font-size: 13px !important;
  letter-spacing: normal !important;
  text-transform: none !important;
}
.ocr-workbench :deep(.el-table td.el-table__cell) {
  padding-block: 9px;
  background: #fff !important;
  border-color: #e7edf5 !important;
}
.ocr-workbench :deep(.el-table tr:hover > td.el-table__cell),
.ocr-workbench :deep(.el-table tr.hover-row > td.el-table__cell) { background: #f1f6fd !important; }
.ocr-workbench :deep(.el-table td.ocr-repository-cell) { color: #162841; font-weight: 700; }
.ocr-workbench :deep(.el-table td.ocr-numeric-cell) { font-variant-numeric: tabular-nums; }
.ocr-table :deep(.el-scrollbar__wrap:focus-visible) { outline: 2px solid #2864ba; outline-offset: -2px; }
.ocr-table :deep(.el-scrollbar__thumb) { --el-scrollbar-bg-color: #5d7695; --el-scrollbar-opacity: .65; }
@container (max-width: 490px) {
  .ocr-task-toolbar { grid-template-columns: minmax(0, 1fr) max-content; }
  .ocr-task-search { grid-column: 1 / -1; }
}
@media (max-width: 620px) {
  .ocr-metric { flex-direction: column; align-items: flex-start; gap: 7px; padding-inline: 10px; }
  .ocr-metric-icon { flex-basis: auto; width: 31px; height: 31px; border-radius: 8px; }
  .ocr-metric-icon svg { width: 17px; height: 17px; }
  .ocr-metric-copy span { font-size: 12px; }
  .ocr-metric-copy strong { font-size: 22px; }
}
.ocr-progress-summary { container-type: inline-size; }
.ocr-progress-header { display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 12px 20px; }
.ocr-progress-identity { flex: 1 1 300px; min-width: 0; }
.ocr-progress-repository, .ocr-progress-commit { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 10px; min-width: 0; }
.ocr-progress-repository h2 { margin: 0; color: #142239; font-size: 20px; font-weight: 700; overflow-wrap: anywhere; min-width: 0; }
.ocr-progress-branch {
  padding: 3px 9px;
  border-radius: 10px;
  color: #174e97;
  background: #e0edfe;
  font-size: 13px;
  font-weight: 650;
  overflow-wrap: anywhere;
  max-width: 100%;
}
.ocr-progress-commit { margin-top: 7px; color: #465b76; font-size: 12px; }
.ocr-progress-commit code { padding: 2px 7px; border-radius: 5px; color: #263e5e; background: #eaf1fa; }
.ocr-progress-controls { justify-content: flex-end; gap: 9px; }
.ocr-progress-controls > .el-tag { font-weight: 650; }
.ocr-progress { margin-top: 15px; padding-block-start: 13px; border-block-start: 1px solid #d8e3f0; }
.ocr-progress-line { display: flex; align-items: baseline; justify-content: space-between; gap: 5px 12px; flex-wrap: wrap; }
.ocr-progress-line h3 { margin: 0; color: #162841; font-size: 15px; }
.ocr-progress-count { display: flex; align-items: baseline; gap: 8px; color: #293c55; font-size: 13px; font-variant-numeric: tabular-nums; }
.ocr-progress-count strong { padding-inline-start: 8px; border-inline-start: 1px solid #cad9eb; color: #154c96; }
.ocr-progress-metrics {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-top: 14px;
  padding-block: 10px;
  border-radius: 11px;
  background: #edf4fd;
}
.ocr-progress-metrics .ocr-metric { gap: 10px; padding-inline: clamp(8px, 2cqi, 24px); }
.ocr-progress-metrics .ocr-metric-icon { flex-basis: 36px; height: 36px; border-radius: 9px; }
.ocr-progress-metrics .ocr-metric-copy span { font-size: 12px; }
.ocr-progress-metrics .ocr-metric-copy strong { font-size: 22px; }
.ocr-retry-card, .ocr-current-card {
  min-width: 0;
  padding: 11px 14px;
  border: 1px solid;
  border-radius: 10px;
}
.ocr-retry-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px 16px;
  margin-top: 12px;
  border-color: #ebcea6;
  border-inline-start: 3px solid #c37a21;
  color: #744012;
  background: #fff4e4;
}
.ocr-activity-main { display: flex; align-items: center; gap: 11px; min-width: 0; }
.ocr-activity-icon { display: grid; flex: 0 0 36px; height: 36px; place-items: center; border-radius: 9px; }
.ocr-retry-icon { color: #874209; background: #ffe1b3; }
.ocr-activity-copy { display: grid; min-width: 0; gap: 3px; }
.ocr-retry-heading, .ocr-retry-meta, .ocr-retry-clock { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 9px; }
.ocr-retry-heading h3 { margin: 0; color: #733808; font-size: 16px; font-weight: 700; }
.ocr-retry-http {
  padding: 2px 7px;
  border-radius: 7px;
  color: #813d07;
  background: #ffe1b3;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}
.ocr-retry-meta { font-size: 12px; font-weight: 550; }
.ocr-retry-meta span + span { padding-inline-start: 9px; border-inline-start: 1px solid #d9b78c; font-variant-numeric: tabular-nums; }
.ocr-retry-clock {
  align-items: center;
  padding-inline-start: 16px;
  border-inline-start: 1px solid #e4c298;
  color: #754417;
  font-size: 13px;
  font-weight: 600;
}
.ocr-retry-clock svg { flex: 0 0 auto; }
.ocr-retry-clock strong { margin-inline-start: 5px; color: #803b07; font-size: 24px; font-weight: 750; font-variant-numeric: tabular-nums; }
.ocr-current-card { margin-top: 9px; border-color: #d4e2f3; background: #eef5fe; }
.ocr-current-icon { color: #1a5cae; background: #dceafb; }
.ocr-current-label { color: #415873; font-size: 12px; font-weight: 600; }
.ocr-current-path, .ocr-current-stage { color: #233d5d; font-size: 14px; line-height: 1.4; overflow-wrap: anywhere; }
.ocr-current-path strong, .ocr-current-stage { color: #142942; font-weight: 700; }
@container (max-width: 500px) {
  .ocr-progress-metrics .ocr-usage-metric strong { font-size: clamp(13px, 5.2cqi, 22px); }
  .ocr-retry-card { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .ocr-retry-clock { padding: 7px 0 0; border-inline-start: 0; border-block-start: 1px solid #e4c298; }
  .ocr-retry-clock strong { font-size: 20px; }
  .ocr-retry-meta span + span { padding-inline-start: 0; border-inline-start: 0; }
}
.ocr-progress-note { margin: 10px 0 0; color: #3b5370; font-size: 13px; }
.ocr-progress-summary.ocr-card > .el-alert { margin-block: 12px 0; }
.ocr-table { margin-top: 12px; }
.ocr-progress .el-progress, .ocr-batch-progress .el-progress { margin-top: 8px; }
.ocr-batch-heading { margin-bottom: 12px; color: #293c55; font-size: 13px; font-variant-numeric: tabular-nums; }
.ocr-batch-heading h2 { margin: 0; color: #142239; }
.ocr-batch-progress { color: #465b76; font-size: 13px; margin-bottom: 12px; font-variant-numeric: tabular-nums; }
.ocr-batch-state { display: flex; align-items: center; gap: 7px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.ocr-batch-table :deep(.el-table__cell) { color: #293c55; }
.ocr-batch-table :deep(th.el-table__cell) {
  color: #293c55 !important;
  background: #eaf0f7 !important;
}
.ocr-batch-table :deep(.el-scrollbar__wrap:focus-visible) { outline: 2px solid #2864ba; outline-offset: -2px; }
.ocr-batch-table :deep(.el-scrollbar__thumb) { --el-scrollbar-bg-color: #5d7695; --el-scrollbar-opacity: .65; }
.ocr-timeline h2 { margin-bottom: 12px; }
@container (max-width: 560px) {
  .ocr-progress-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 9px; }
  .ocr-progress-metrics .ocr-metric { flex-direction: row; align-items: center; }
  .ocr-progress-metrics .ocr-metric:nth-child(odd) { border-inline-start: 0; }
  .ocr-progress-controls { justify-content: flex-start; }
}
.ocr-card .el-alert { margin-block: 12px; }
.ocr-scope-target { margin-top: 14px; border: 1px solid #dce6f4; border-radius: 10px; background: #fbfdff; min-width: 0; }
.ocr-scope-target > summary { cursor: pointer; padding: 10px 12px; color: var(--el-color-primary); }
.ocr-scope-summary { display: inline-flex; gap: 7px; align-items: center; flex-wrap: wrap; vertical-align: middle; margin-inline-start: 4px; max-width: calc(100% - 24px); }
.ocr-scope-summary strong { color: var(--el-text-color-primary); overflow-wrap: anywhere; }
.ocr-scope-body { padding: 0 12px 12px; }
.ocr-scope-sha { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; color: var(--el-text-color-secondary); font-size: 12px; padding-bottom: 10px; }
.ocr-scope-sha code { color: #304767; background: #edf3fb; padding: 2px 6px; border-radius: 4px; }
.ocr-input { border: 1px solid var(--el-border-color); border-radius: 4px; padding: 7px; }
code { white-space: pre-wrap; overflow-wrap: anywhere; }
.ocr-end { justify-content: flex-end; }
@media (max-width: 790px) { .ocr-columns { grid-template-columns: minmax(0, 1fr); } .ocr-card { padding: 12px; } }
</style>
