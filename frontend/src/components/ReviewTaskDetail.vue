<template>
  <section ref="panel" class="review-detail" :class="{ 'is-fullscreen': isFullscreen }" aria-label="审查任务详情">
    <header class="detail-bar">
      <div class="detail-title"><el-button text :icon="ArrowLeft" @click="$emit('close')">返回完整列表</el-button><h2>审查任务详情</h2></div>
      <div class="detail-controls">
        <span v-if="hasNativeEvents" class="connection"><i :class="connectionClass" />{{ connectionLabel }}</span>
        <el-button text :icon="FullScreen" @click="toggleFullscreen">{{ isFullscreen ? '退出全屏' : '全屏' }}</el-button>
      </div>
    </header>

    <el-skeleton v-if="loading && !task" :rows="8" animated />
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <template v-if="task">
      <div class="identity-row">
        <div class="identity-copy"><strong>{{ instance || '历史实例未记录' }} · {{ repository }} #{{ task.number }}</strong><span>{{ modelName }} · 任务 {{ task.id }}</span></div>
        <div class="identity-actions"><slot name="actions" /><el-button v-if="canCancel && ['queued', 'running', 'awaiting_cost', 'budget_paused', 'incomplete'].includes(task.status)" type="danger" plain :loading="cancelling" :disabled="cancelling" @click="$emit('cancel')">强制取消</el-button><el-tag :type="statusType(task.status)" size="large">{{ task.progress?.label || statusLabel(task.status) }}</el-tag><el-button v-if="prUrl" type="primary" :icon="TopRight" tag="a" :href="prUrl" target="_blank" rel="noopener">查看 PR</el-button></div>
      </div>
      <el-alert v-if="task.cancel_requested_at" :title="task.status === 'cancelling' ? '正在强制取消；确认执行停止后显示已取消' : '本次审查已人工取消，当前版本不会自动重跑'" :description="[task.cancel_requested_by, task.cancel_error].filter(Boolean).join(' · ')" type="info" show-icon :closable="false" />
      <details v-if="isPlatformAdmin && task.error" class="failure-card" aria-label="任务失败详情"><summary>失败原因：{{ errorSummary }} · 展开诊断</summary>
        <div class="failure-heading"><WarningFilled /><div><strong>{{ errorSummary }}</strong><details v-if="String(task.error).length > 100" class="full-error"><summary>查看完整错误信息</summary><pre>{{ task.error }}</pre></details><p>{{ diagnosticSummary }}</p></div></div>
        <dl v-if="primaryDiagnosticRows.length" class="failure-fields"><template v-for="row in primaryDiagnosticRows" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></template></dl>
        <p v-else class="legacy-diagnostic">此历史任务未记录结构化诊断，无法补推失败层级、调用标识和响应位置。</p>
        <details v-if="moreDiagnosticRows.length"><summary>更多定位信息（{{ moreDiagnosticRows.length }} 项）</summary><dl class="failure-fields more-fields"><template v-for="row in moreDiagnosticRows" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></template></dl></details>
        <p v-if="safeDiagnostic?.secondary_exception_type" class="secondary-note">次级传输异常 {{ safeDiagnostic.secondary_exception_type }} 发生在上述首因之后，不替代首因。</p>
        <details v-if="secondaryDiagnostics.length" class="secondary-diagnostics"><summary>次级原生错误（{{ secondaryDiagnostics.length }} 项）</summary><p>以下错误发生在首因之后，仅用于补充定位，不替代上方首因。</p><pre v-for="(item, index) in secondaryDiagnostics" :key="index">{{ toDiagnosticText(item) }}</pre></details>
        <details v-if="safeDiagnostic"><summary>查看完整脱敏诊断</summary><pre data-testid="safe-diagnostic">{{ diagnosticText }}</pre><el-button type="primary" plain size="small" @click="copyDiagnostic">{{ diagnosticCopied ? '已复制' : '复制脱敏诊断' }}</el-button></details>
      </details>
      <details v-if="isPlatformAdmin && (rawDiagnostic || task.error)" class="raw-diagnostics" aria-label="原始诊断包"><summary>原始诊断包 · 按需查看与下载</summary>
        <div><strong>原始诊断包</strong><p>{{ rawDiagnosticStatus }}</p></div>
        <dl v-if="rawDiagnostic" class="raw-meta"><dt>记录大小</dt><dd>{{ byteSize(rawDiagnostic.bytes) }}</dd><dt>保留期限</dt><dd>{{ rawRetention }}</dd><template v-if="Number.isInteger(rawDiagnostic.collected_files)"><dt>已收集文件</dt><dd>{{ rawDiagnostic.collected_files }} 个</dd></template><template v-if="Number.isInteger(rawDiagnostic.omitted_files)"><dt>省略文件</dt><dd>{{ rawDiagnostic.omitted_files }} 个</dd></template></dl>
        <el-alert v-if="rawDiagnostic?.truncated" :title="rawTruncationMessage" type="warning" :closable="false" />
        <p class="raw-warning">原始事件和响应可能包含业务源码，仅保存在部署机并供管理员下载定位。采集不包含模型 Key、认证头或运行时环境变量。</p>
        <el-alert v-if="rawDownloadError" :title="rawDownloadError" type="error" :closable="false" />
        <el-button v-if="rawDiagnostic?.available && !rawExpired" type="primary" plain :icon="Download" :loading="rawDownloading" @click="downloadRawDiagnostics">下载原始诊断包</el-button>
      </details>

      <ReviewFailureChecks :task="task" />
      <ReviewBatchPlan v-if="task.batch_protocol" :task="task" @refresh="$emit('refresh')" />
      <details class="execution-summary" :open="continuing"><summary>{{ continuing ? '执行进度与资源用量' : '执行摘要 · 查看耗时与资源用量' }}</summary>
      <section class="action-card">
        <div><span>当前动作</span><h3>{{ currentAction }}</h3><p>{{ actionContext }}</p></div>
        <div class="action-state"><el-icon v-if="continuing" class="is-loading"><Loading /></el-icon><el-icon v-else-if="task.status === 'succeeded'"><CircleCheckFilled /></el-icon><el-icon v-else><WarningFilled /></el-icon><strong>{{ continuing ? '执行中' : statusLabel(task.status) }}</strong></div>
      </section>
      <el-alert v-if="compacting" title="模型正在压缩上下文" description="正在整理会话历史，压缩完成后继续审查；执行器心跳仅表示会话存活。" type="info" :closable="false" show-icon data-testid="compaction-status" />
      <div class="metrics">
        <div><span>已耗时</span><strong>{{ duration(elapsedSeconds) }}</strong><small>{{ targetText }}</small></div>
        <div><span>剩余预算</span><strong>{{ remainingBudget }}</strong><small>{{ task.progress_stage === 'issue_checks' ? '逐问题使用独立请求超时' : '主审查会话预算' }}</small></div>
        <div><span>模型请求</span><strong>{{ counters.model_calls ?? task.model_calls ?? 0 }} 次</strong><small>{{ modelWaitLabel }}</small></div>
        <div><span>工具调用</span><strong>{{ toolCount }} 次</strong><small>{{ eventCount }} 条原生记录</small></div>
      </div>

      <ol class="stage-track">
        <li v-for="(stage, index) in stages" :key="stage.key" :class="stage.state"><span><el-icon v-if="stage.state === 'done'"><Check /></el-icon><template v-else>{{ index + 1 }}</template></span><div><strong>{{ stage.label }}</strong><small>{{ stage.detail }}</small></div></li>
      </ol>

      <el-alert v-if="task.progress?.sync_error" :title="'PR 进度同步：' + task.progress.sync_error" type="warning" :closable="false" />
      </details>
      <el-tabs v-model="activeTab" class="detail-tabs">
        <el-tab-pane label="实时过程" name="process">
          <section class="process-pane">
            <div class="process-heading"><div><h3>执行记录</h3><p>公开模型说明与工具状态按发生时间升序显示。</p></div><div class="process-actions"><el-select v-if="task.batch_protocol" v-model="eventSession" placeholder="当前单元会话" clearable style="width: 240px"><el-option v-for="attempt in task.batch?.attempts || []" :key="attempt.session_id" :value="attempt.session_id" :label="attempt.unit + ' / 尝试 ' + attempt.attempt" /></el-select><el-segmented v-model="eventFilter" :options="filterOptions" /><el-input v-model="searchDraft" clearable :prefix-icon="Search" placeholder="搜索文件或记录" @keyup.enter="applySearch" @clear="applySearch" /><el-button :icon="Download" @click="downloadEvents">下载脱敏日志</el-button></div></div>
            <el-alert v-if="eventError" :title="eventError" type="error" :closable="false"><el-button link type="primary" @click="resetEvents">重新连接</el-button></el-alert>
            <el-empty v-if="legacyWithoutEvents" description="此旧任务没有记录原生执行日志；审查结果与历史检索仍可在其他页签查看。" />
            <div v-else ref="eventScroller" class="event-scroller" @scroll="onEventScroll">
              <button v-if="pendingCount" type="button" class="new-events" @click="resumeFollowing">{{ pendingCount }} 条新记录 · 回到最新</button>
              <div v-if="loadingHistory" class="history-loading">正在加载更早记录…</div>
              <div class="virtual-space">
                <div aria-hidden="true" :style="{ height: beforeSpace + 'px' }" />
                <article v-for="row in visibleEvents" :key="eventKey(row)" class="event-row" :class="[`is-${eventCategory(row)}`, { 'is-error': isEventError(row) }]" :style="{ height: row.height + 'px' }">
                  <time>{{ eventTime(row.created_at) }}</time><span class="event-dot"><el-icon><component :is="eventIcon(row)" /></el-icon></span>
                  <div class="event-main"><div class="event-title"><strong>{{ eventTitle(row) }}</strong><el-tag v-if="eventCategory(row) === 'tool'" size="small" :type="isEventError(row) ? 'danger' : row.status === 'running' || row.status === 'pending' ? 'primary' : 'info'">{{ row.status === 'running' || row.status === 'pending' ? '执行中' : isEventError(row) ? '异常' : '已完成' }}</el-tag><code v-if="row.target">{{ row.target }}</code><span v-if="eventDuration(row)">{{ eventDuration(row) }}</span><el-button v-if="eventDetails(row)" link :icon="expandedEvents.has(eventKey(row)) ? ArrowUp : ArrowDown" @click="toggleEvent(row)">{{ expandedEvents.has(eventKey(row)) ? '收起' : '展开' }}</el-button></div><p>{{ row.summary || '此记录没有公开摘要。' }}</p><pre v-if="expandedEvents.has(eventKey(row))">{{ eventDetails(row) }}</pre></div>
                </article>
                <div aria-hidden="true" :style="{ height: afterSpace + 'px' }" />
              </div>
              <footer class="stream-footer"><span><i :class="connectionClass" />{{ following ? '正在跟随最新记录' : '已暂停跟随' }}</span><span>已加载 {{ events.length }} 条</span></footer>
            </div>
          </section>
        </el-tab-pane>

        <el-tab-pane :label="diffOnly ? '审查报告' : `审查结果（${findings.length}）`" name="result">
          <el-alert v-if="diffOnly" title="仅审查 PR diff" description="仅使用 PR 标题、描述和 diff 中的变更与相邻上下文；未读取完整文件或工程，未执行构建与测试。" type="info" :closable="false" />
          <el-alert v-if="partial" title="本次审查存在未覆盖或未完成内容，请结合覆盖范围查看。" type="warning" show-icon :closable="false" />
          <ReviewIssueLedger :task="task" :open="open" />
          <template v-if="reportText">
            <div v-if="diffOnly && findings.length" data-testid="diff-inline-locations">
              <p v-if="task.result?.lifecycle">本轮问题候选（{{ findings.length }} 项；实际新增、复用和待确认状态见问题台账）</p>
              <p v-else>行内定位（{{ findings.length }} 项；{{ task.status === 'succeeded' ? '已完成发布核对' : '以最终发布结果为准' }}）</p>
              <p v-for="(f, i) in findings" :key="i">
                {{ f.anchor?.id || '未定位' }} · {{ f.title }} · {{ f.path }}
                <template v-if="f.anchor"> · {{ f.anchor.side === 'new' ? '新' : '旧' }}侧第 {{ f.anchor.line }} 行</template>
                <template v-if="f.existing_review_id"> · 已有历史意见，本次未重复发布</template>
                <template v-if="task.result?.lifecycle"> · {{ f.issue_id ? `问题 ${f.issue_id}` : '关联待确认，未发布' }}</template>
                <template v-if="!f.anchor"> · {{ f.fallback_reason || '仅保留在报告' }}</template>
              </p>
            </div>
            <pre class="diff-report" data-testid="diff-review-report">{{ reportText }}</pre>
          </template>
          <el-empty v-else-if="!findings.length" :description="diffOnly ? '尚未返回审查报告。' : task.status === 'succeeded' && !partial ? '已分析范围内未发现证据充分的问题。' : '审查尚未完整完成，不能给出无问题结论。'" />
          <el-collapse v-else v-model="expanded"><el-collapse-item v-for="(f, i) in findings" :key="f.finding_id || f.candidate_id || i" :name="String(i)"><template #title><div class="finding-title"><el-tag :type="f.severity === 'P1' ? 'danger' : 'warning'" size="small">{{ f.severity }}</el-tag><strong>{{ f.title }}</strong><span v-if="f.assessment === 'uncertain'">待确认</span></div></template><article class="finding-content"><p class="path">{{ f.path }} · {{ f.scope }}</p><p v-if="f.anchor">{{ f.anchor.side === 'new' ? '新' : '旧' }}侧第 {{ f.anchor.line }} 行<span v-if="f.existing_review_id"> · 已有历史意见，本次未重复发布</span></p><p v-else>普通意见：{{ f.fallback_reason || '没有可靠的行内位置' }}</p><pre v-if="f.quote">{{ f.quote }}</pre><dl><template v-for="field in findingFields" :key="field.key"><dt v-if="f[field.key]">{{ field.label }}</dt><dd v-if="f[field.key]">{{ String(f[field.key]).slice(0, textLimits[i + field.key] || 1200) }}<template v-if="String(f[field.key]).length > 1200"><p class="secondary">共 {{ String(f[field.key]).length }} 字符，当前显示 {{ Math.min(textLimits[i + field.key] || 1200, String(f[field.key]).length) }} 字符</p><el-button v-if="(textLimits[i + field.key] || 1200) < String(f[field.key]).length" link type="primary" @click="textLimits[i + field.key] = (textLimits[i + field.key] || 1200) + 12000">展开下一段</el-button><el-button v-if="textLimits[i + field.key] > 1200" link @click="textLimits[i + field.key] = 1200">收起</el-button></template></dd></template></dl><details v-if="f.source_evidence?.length || f.trace?.length"><summary>查看推导及源码证据</summary><ol><li v-for="(step, n) in f.trace || []" :key="n">{{ step }}</li></ol><div v-for="(source, n) in f.source_evidence || []" :key="n"><p>{{ source.version === 'before' ? '修改前' : '修改后' }} · {{ source.path }}</p><pre>{{ source.quote }}</pre></div></details><p v-if="f.finding_id" class="secondary">意见编号 {{ f.finding_id }}</p></article></el-collapse-item></el-collapse>
        </el-tab-pane>

        <el-tab-pane :label="`变更与覆盖（${groups.length} 组缺口）`" name="coverage">
          <el-alert v-if="diffOnly" :title="`已提供 ${coverage.diff_files_provided || 0} 个文件的 PR diff`" description="这里统计输入范围，不代表完整工程覆盖；二进制内容和 diff 外的依赖无法检查。" type="info" :closable="false" />
          <p class="secondary">文件读取数量与分析完成数量分别统计。重复缺口已合并，展开文件查看具体原因。</p><p v-if="coverage.change_evidence" class="evidence-count">已提供变更原文 {{ coverage.change_evidence.supplied }} / {{ coverage.change_evidence.expected }} 行。此项只统计原文供给，审查结论还需结合已完成单元和未决范围。</p><el-empty v-if="!groups.length" :description="task.status === 'succeeded' ? '本次没有记录覆盖缺口。' : '当前尚未记录缺口，任务完成前不能判断覆盖完整。'" /><el-collapse><el-collapse-item v-for="group in groups" :key="group.path" :name="group.path"><template #title><div class="coverage-title"><strong>{{ group.path }}</strong><el-tag type="warning" size="small">{{ group.reasons.length }} 类原因</el-tag></div></template><div v-for="(reason, i) in group.reasons" :key="i" class="coverage-reason"><p>{{ reason.reason }}</p><small>记录 {{ reason.count }} 次<span v-if="reason.scopes.length"> · 涉及 {{ reason.scopes.length }} 个函数</span></small><details v-if="reason.scopes.length"><summary>查看函数</summary><ul><li v-for="scope in reason.scopes" :key="scope">{{ scope }}</li></ul></details></div></el-collapse-item></el-collapse>
          <details v-if="coverage.diagnostics?.length"><summary>代码组织说明（{{ coverage.diagnostics.length }} 项）</summary><p class="secondary">语法树用于辅助组织。解析器未识别的原文仍可进入审查；实际未决内容在上方单独显示。</p><ul class="diagnostics"><li v-for="(note, i) in coverage.diagnostics" :key="i"><strong>{{ note.path }}</strong><span>{{ note.side === 'old' ? '修改前' : '修改后' }}<template v-if="note.start_line"> · 第 {{ note.start_line }} 行</template> · {{ note.reason }}</span></li></ul></details>
          <section v-if="!hasNativeEvents"><h4>历史检索记录（{{ retrieval.operations || 0 }} 条）</h4><p>读取记录包含整个任务的分页和复用记录，不代表模型分析次数，也不会作为完整对话历史逐轮累加。</p><p class="secondary">模型补查 {{ retrieval.model_rounds ?? retrieval.rounds ?? 0 }} 轮 · 程序补查 {{ retrieval.runtime_rounds || 0 }} 轮 · 独立请求 {{ retrieval.unique_requests ?? retrieval.operations ?? 0 }} 项 · 读取记录 {{ retrieval.operations || 0 }} 条 · {{ retrieval.failed || 0 }} 条读取失败</p><el-alert v-if="readError" :title="readError" type="error" :closable="false" /><el-empty v-if="!reads.length" description="本次尚无主动检索记录，模型可以直接使用已提供的上下文。" /><el-collapse><el-collapse-item v-for="(read, i) in reads" :key="`${read.unit}:${read.request_id}:${i}`" :name="`read-${i}`"><template #title><div class="coverage-title"><el-tag :type="read.status === 'error' || read.complete === false ? 'warning' : 'info'" size="small">{{ read.request?.version === 'before' ? '修改前' : '修改后' }}</el-tag><strong>{{ operations[read.request?.op] || '读取' }} · {{ read.request?.path || read.request?.pattern || '*' }}</strong><span v-if="read.source">第 {{ read.source.start }}–{{ read.source.end }} 行</span><span v-if="read.request?.query">{{ read.request.query }}</span></div></template><p v-if="read.status === 'error'" class="read-warning">{{ read.reason }}</p><template v-if="read.source"><p>固定提交 {{ read.source.commit }} · 第 {{ read.source.start }}–{{ read.source.end }} 行</p><pre>{{ read.source.code }}</pre></template></el-collapse-item></el-collapse><el-button v-if="reads.length < readTotal" :loading="reading" @click="loadReads">加载更多记录（{{ reads.length }} / {{ readTotal }}）</el-button></section>
        </el-tab-pane>

        <el-tab-pane v-if="isPlatformAdmin" label="运行信息" name="runtime">
          <div class="runtime-health"><div><span>日志连接</span><strong>{{ connectionLabel }}</strong></div><div><span>执行器心跳</span><strong>{{ heartbeatLabel }}</strong></div><div><span>模型等待</span><strong>{{ modelWaitLabel }}</strong></div></div>
          <section v-if="!hasNativeEvents"><h4>历史阶段耗时</h4><p class="secondary">准备、分析、发布为不同阶段。模型传输、执行器及检索耗时属于分析阶段内的明细，不能重复相加；进行中的阶段在结束后记录。</p><dl class="runtime" data-testid="performance-stages"><template v-for="[key, label] in performanceStages" :key="key"><dt>{{ label }}</dt><dd>{{ seconds(task.performance?.stages_seconds?.[key]) }}</dd></template><dt>排队等待</dt><dd>{{ seconds(task.progress?.queue_seconds) }}</dd></dl><details v-if="task.performance"><summary>历史分析明细与执行器调用（{{ task.performance.recorded_calls }} 次）</summary><dl class="runtime"><dt>检索与保存</dt><dd>{{ seconds(task.performance.analysis_detail_seconds?.retrieval) }}</dd><dt>等待执行器</dt><dd>{{ seconds(task.performance.analysis_detail_seconds?.call) }}</dd><dt>其中模型传输</dt><dd>{{ seconds(task.performance.analysis_detail_seconds?.model) }}</dd><dt>其余调用开销</dt><dd>{{ seconds(task.performance.analysis_detail_seconds?.overhead) }}</dd></dl><p class="secondary">其余调用开销含进程启动、协议处理及内部传输。只统计实际发起的上游请求；有调用缺失模型耗时记录时不推算该项；中断前未落盘的阶段不补造时间。</p><div v-for="call in (task.performance.calls || []).slice(0, callLimit)" :key="call.call_id" class="call-timing"><code>{{ call.call_id }}</code><span>调用 {{ seconds(call.call_seconds) }} · 执行器 {{ seconds(call.executor_seconds) }} · 模型 {{ seconds(call.model_seconds) }}</span></div><el-button v-if="(task.performance.calls || []).length > callLimit" link @click="callLimit += 50">显示后续 50 次调用</el-button></details></section>
          <section v-if="task.diagnostic?.call_id"><h4>当前模型调用</h4><p>{{ ({waiting:'等待模型响应', thinking:'模型正在思考', answering:'模型正在生成正文', tool_call:'模型正在请求工具'})[task.diagnostic.model_phase] || '查看下方传输状态' }}</p><dl class="runtime"><template v-for="row in diagnosticRows" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></template></dl><p class="secondary">字符数不是 Token 数；请求参数被接受不代表服务实际执行。心跳不会延长总期限。</p></section>
          <h4>执行配置</h4><dl class="runtime"><dt>任务编号</dt><dd>{{ task.id }}</dd><dt>审查提交</dt><dd>{{ task.progress?.head || task.snapshot?.head || '未读取' }}</dd><dt>比较基点</dt><dd>{{ task.snapshot?.merge_base || '未读取' }}</dd><dt>执行模型</dt><dd>{{ modelName }}</dd><dt>执行引擎</dt><dd>{{ task.configuration?.engine || '历史任务未记录' }}</dd><dt>整次审查上限</dt><dd>{{ task.configuration?.review_timeout_seconds ?? task.configuration?.task_timeout_seconds ?? '历史任务未记录' }} 秒</dd><dt>模型上下文窗口</dt><dd>{{ task.configuration?.model_context_tokens ?? '历史任务未配置' }} Token</dd><dt>上下文压缩阈值</dt><dd>{{ task.configuration?.compaction_threshold_tokens ?? '历史任务使用原生默认' }} Token</dd><dt>单次模型请求超时</dt><dd>{{ task.configuration?.model_request_timeout_seconds ?? '历史任务未记录' }} 秒</dd><template v-if="!hasNativeEvents"><dt>已处理／失败单元</dt><dd>{{ task.result?.units_processed ?? task.progress?.units_completed ?? 0 }} / {{ task.result?.units_failed ?? 0 }}</dd></template><dt>配置版本</dt><dd>{{ task.config_revision }}</dd><dt>更新时间</dt><dd>{{ fmt(task.updated) }}</dd></dl>
        </el-tab-pane>
      </el-tabs>
    </template>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowDown, ArrowUp, ArrowLeft, Check, CircleCheckFilled, Close, Download, Document, FullScreen, Loading, Search, Setting, TopRight, WarningFilled } from '@element-plus/icons-vue'
import { api } from '../api'
import ReviewIssueLedger from './ReviewIssueLedger.vue'
import ReviewBatchPlan from './ReviewBatchPlan.vue'
import ReviewFailureChecks from './ReviewFailureChecks.vue'
import { isPlatformAdmin } from '../composables/accessState.js'
import { coverageGroups, statusType } from './reviewDisplay'
import { diagnosticLocation, diagnosticRows as toDiagnosticRows, diagnosticText as toDiagnosticText, safeDiagnostic as toSafeDiagnostic } from './reviewDiagnostics'

const props = defineProps({ open: Boolean, task: Object, loading: Boolean, error: String, repository: String, instance: String, cancelling: Boolean, canCancel: Boolean })
defineEmits(['close', 'cancel', 'refresh'])
const panel = ref(), eventScroller = ref(), isFullscreen = ref(false), callLimit = ref(50)
const activeTab = ref('process'), expanded = ref(['0']), textLimits = ref({})
const events = ref([]), hasOlder = ref(false), loadingEvents = ref(false), loadingHistory = ref(false), requestingEvents = ref(false), eventError = ref(''), newestSeq = ref(0), oldestSeq = ref(0)
const eventSession = ref('')
const eventFilter = ref('all'), searchDraft = ref(''), searchQuery = ref(''), following = ref(true), pendingCount = ref(0), scrollTop = ref(0), viewportHeight = ref(520)
const expandedEvents = ref(new Set()), connectionOk = ref(false)
const reads = ref([]), readTotal = ref(0), readPage = ref(1), reading = ref(false), readError = ref('')
const diagnosticCopied = ref(false)
const rawDownloading = ref(false), rawDownloadError = ref('')
let generation = 0, eventTimer, eventController, readGeneration = 0

const continuing = computed(() => ['queued','running','cancelling','publishing','recovering','publish_unknown'].includes(props.task?.status))
const shouldPollEvents = computed(() => hasNativeEvents.value && props.task?.status !== 'cancelled' && props.task?.log_sync?.complete !== true)
const findings = computed(() => Array.isArray(props.task?.result?.findings) ? props.task.result.findings : [])
const coverage = computed(() => props.task?.result?.coverage || props.task?.snapshot?.coverage || {})
const diffOnly = computed(() => coverage.value.scope === 'diff')
const reportText = computed(() => props.task?.result?.report_markdown || '')
const groups = computed(() => coverageGroups(coverage.value))
const retrieval = computed(() => props.task?.result?.context_retrieval || {})
const counters = computed(() => props.task?.counters || {})
const modelName = computed(() => props.task?.configuration?.model || '历史模型未记录')
const compacting = computed(() => continuing.value && props.task?.counters?.compaction_active === true)
const currentAction = computed(() => ['cancelling', 'cancelled'].includes(props.task?.status) ? statusLabel(props.task.status) : compacting.value ? '模型正在压缩上下文' : props.task?.current_action || props.task?.stage || statusLabel(props.task?.status))
const actionContext = computed(() => { const a = props.task?.progress?.action || props.task?.action || {}; const version = a.version === 'before' ? '修改前' : a.version === 'after' ? '修改后' : ''; return [version, a.path, a.start_line && a.end_line ? `${a.start_line}–${a.end_line} 行` : ''].filter(Boolean).join(' · ') || props.task?.progress?.detail || '等待下一条原生执行记录' })
const elapsedSeconds = computed(() => props.task?.timing?.execution_seconds ?? props.task?.progress?.elapsed_seconds ?? (props.task?.progress?.started_at ? Math.max(0, Date.now()/1000-props.task.progress.started_at) : null))
const reviewBudget = computed(() => props.task?.batch?.budget_seconds ?? props.task?.configuration?.review_timeout_seconds ?? props.task?.progress?.target_seconds ?? props.task?.configuration?.task_timeout_seconds ?? 600)
const remainingBudget = computed(() => props.task?.progress_stage === 'issue_checks' ? '逐项复核' : elapsedSeconds.value == null ? '未开始' : duration(Math.max(0, reviewBudget.value - elapsedSeconds.value)))
const targetText = computed(() => props.task?.progress_stage === 'issue_checks' ? '旧问题逐项复核，无总调用次数上限' : props.task?.progress?.target_exceeded ? (continuing.value ? '已超过目标时间，继续完整审查' : '本次执行超过目标时间') : `整次审查上限 ${duration(reviewBudget.value)}`)
const eventCount = computed(() => counters.value.events ?? props.task?.log_sync?.last_seq ?? events.value.length)
const toolCount = computed(() => counters.value.tool_calls ?? counters.value.tools ?? counters.value.tool_events ?? events.value.filter(e => eventCategory(e) === 'tool').length)
const modelWaitLabel = computed(() => { if (props.task?.status === 'cancelled') return '请求已终止'; if (props.task?.status === 'cancelling') return '正在确认请求停止'; const m = props.task?.progress?.model_request; if (!m) return '当前未等待'; if (m.status === 'waiting' || m.status === 'running') return `等待 ${duration(m.elapsed_seconds || 0)} / 上限 ${duration(m.timeout_seconds || 0)}`; return m.status === 'failed' ? `${m.elapsed_seconds || 0} 秒 / 有效上限 ${m.timeout_seconds || 0} 秒 · 请求异常` : '最近请求已完成' })
const heartbeatLabel = computed(() => !props.task?.last_heartbeat ? '历史任务未记录' : Date.now()/1000-props.task.last_heartbeat < 90 ? '正常' : '已超过 90 秒')
const connectionLabel = computed(() => eventError.value ? '日志连接异常' : connectionOk.value ? '日志已连接' : '正在连接日志')
const connectionClass = computed(() => eventError.value ? 'dot error' : connectionOk.value ? 'dot online' : 'dot waiting')
const safeDiagnostic = computed(() => toSafeDiagnostic(props.task?.diagnostic))
const diagnosticRows = computed(() => { const rows = toDiagnosticRows(props.task?.diagnostic); if (props.task?.session?.id && !rows.some(row => row.key === 'session_id')) rows.push({ key: 'session_id', label: '会话标识', value: props.task.session.id }); return rows })
const primaryDiagnosticKeys = new Set(['source', 'phase', 'reason', 'upstream_http_status', 'upstream_error_type', 'call_id', 'upstream_request_id', 'upstream_trace_id', 'session_id', 'native_session_id', 'json_line', 'json_column', 'json_offset'])
const primaryDiagnosticRows = computed(() => diagnosticRows.value.filter(row => primaryDiagnosticKeys.has(row.key)).slice(0, 8))
const moreDiagnosticRows = computed(() => { const shown = new Set(primaryDiagnosticRows.value.map(row => row.key)); return diagnosticRows.value.filter(row => !shown.has(row.key)) })
const diagnosticText = computed(() => toDiagnosticText({ ...(props.task?.diagnostic || {}), ...(props.task?.session?.id && !props.task?.diagnostic?.session_id ? { session_id: props.task.session.id } : {}) }))
const secondaryDiagnostics = computed(() => safeDiagnostic.value?.secondary || [])
const errorSummary = computed(() => { const value = String(props.task?.error || ''); return value.length > 100 ? value.slice(0, 100) + '…' : value })
const diagnosticSummary = computed(() => {
  const value = safeDiagnostic.value
  if (!value) return '详细定位信息未记录'
  const where = diagnosticLocation(value)
  return [where && `失败位置：${where}`, value.error_code && `错误代码：${value.error_code}`].filter(Boolean).join(' · ') || '失败位置未记录；可用下方标识在容器日志中定位'
})
const rawDiagnostic = computed(() => props.task?.raw_diagnostics && typeof props.task.raw_diagnostics === 'object' ? props.task.raw_diagnostics : null)
const rawRetention = computed(() => rawDiagnostic.value?.retention_expires_at ? fmt(rawDiagnostic.value.retention_expires_at) : '未记录')
const rawExpired = computed(() => rawDiagnostic.value?.status === 'expired' || Boolean(rawDiagnostic.value?.retention_expires_at && Number(rawDiagnostic.value.retention_expires_at) < Date.now()/1000))
const rawTruncationMessage = computed(() => `原始诊断未完整保存，仅包含已成功采集的内容；缺失内容无法从页面补回${rawDiagnostic.value?.partial_reason || rawDiagnostic.value?.error ? `（原因代码：${rawDiagnostic.value.partial_reason || rawDiagnostic.value.error}）` : ''}。`)
const rawDiagnosticStatus = computed(() => {
  const value = rawDiagnostic.value
  if (!value) return '此旧任务未记录原始诊断，现已无法补回。'
  if (rawExpired.value) return '原始诊断已超过保留期限并清理，无法补回。'
  if (value.available) return value.truncated ? '已有部分原始诊断可下载。' : '原始诊断已保存，可在保留期限内下载。'
  if (value.error || value.status === 'error') return `原始诊断采集失败${value.error ? `（错误代码：${value.error}）` : ''}。`
  if (value.status === 'capturing') return '原始诊断正在采集，完成后才能下载。'
  if (value.status === 'ready' || value.status === 'archiving') return '原始诊断正在整理，完成后才能下载。'
  if (value.status === 'unavailable') return '本次原始诊断未成功保存，无法下载。'
  return '本任务没有可下载的原始诊断记录。'
})
const prUrl = computed(() => props.task?.pr_url || props.task?.url || props.task?.progress?.pr_url || '')
const filterOptions = [{ label:'全部', value:'all' },{ label:'工具', value:'tool' },{ label:'异常', value:'error' }]
const legacyWithoutEvents = computed(() => !loadingEvents.value && !eventError.value && !events.value.length && !hasNativeEvents.value)
const hasNativeEvents = computed(() => Boolean(String(props.task?.session?.id || '').trim()) || props.task?.native_session === true)
const partial = computed(() => coverage.value.complete === false || (!hasNativeEvents.value && props.task?.progress?.units_total != null && props.task.progress.units_completed < props.task.progress.units_total))
const performanceStages = [['preparation','准备代码'],['analysis','分析与反证'],['publication','发布意见']]
const stages = computed(() => {
  const status=props.task?.status, phase=`${props.task?.diagnostic?.phase||''} ${currentAction.value}`
  if (['cancelling', 'cancelled'].includes(status)) {
    const prepared = Boolean(props.task?.session_submitted_at)
    return [{key:'prepare',label:'准备仓库',state:prepared?'done':'pending',detail:prepared?'已完成':statusLabel(status)},
      {key:'review',label:'自主审查',state:status==='cancelling'?'active':'pending',detail:statusLabel(status)},
      {key:'publish',label:'发布结果',state:'pending',detail:'未发布'}]
  }
  let index=status==='queued'?0:status==='running'?1:2
  if(status==='failed') index=/准备|仓库|快照/.test(phase)?0:/发布|核对/.test(phase)?2:1
  const success=status==='succeeded'
  return [{key:'prepare',label:'准备仓库'},{key:'review',label:'自主审查'},{key:'publish',label:'发布结果'}].map((s,i)=>({...s,state:success||i<index?'done':status==='failed'&&i===index?'error':i===index&&continuing.value?'active':'pending',detail:success||i<index?'已完成':status==='failed'&&i===index?'执行失败':i===index&&continuing.value?'正在进行':'等待执行'}))
})
const rowHeights = computed(() => events.value.map(e => expandedEvents.value.has(eventKey(e)) ? 154 : 82))
const offsets = computed(() => { let top=0; return rowHeights.value.map(height => { const row={top,height}; top+=height; return row }) })
const virtualHeight = computed(() => rowHeights.value.reduce((sum,value)=>sum+value,0))
const visibleEvents = computed(() => {
  const rows=offsets.value,startY=Math.max(0,scrollTop.value-500),endY=scrollTop.value+viewportHeight.value+500
  let low=0,high=rows.length
  while(low<high){const middle=(low+high)>>1;if(rows[middle].top+rows[middle].height<startY)low=middle+1;else high=middle}
  const start=low;high=rows.length
  while(low<high){const middle=(low+high)>>1;if(rows[middle].top<=endY)low=middle+1;else high=middle}
  return events.value.slice(start,low).map((event,index)=>({...event,...rows[start+index],index:start+index}))
})
const beforeSpace = computed(() => visibleEvents.value[0]?.top || 0)
const afterSpace = computed(() => visibleEvents.value.length ? Math.max(0,virtualHeight.value-visibleEvents.value.at(-1).top-visibleEvents.value.at(-1).height) : virtualHeight.value)

const statusLabel = s => ({cancelling:'取消中',cancelled:'已取消',queued:'等待执行',running:'分析中',publishing:'发布与核对',recovering:'核对发布结果',publish_unknown:'发布待核对',succeeded:'已完成',superseded:'版本已过期',failed:'执行失败'}[s]||s||'状态未记录')
const seconds = value => Number.isFinite(value) ? `${value} 秒` : '未记录'
const duration = value => Number.isFinite(Number(value)) ? `${String(Math.floor(Number(value)/60)).padStart(2,'0')}:${String(Math.floor(Number(value)%60)).padStart(2,'0')}` : '—'
const fmt = value => value ? new Date(Number(value)>1e12?Number(value):Number(value)*1000).toLocaleString('zh-CN') : '尚无'
const byteSize = value => Number.isFinite(Number(value)) ? `${Number(value).toLocaleString('zh-CN')} 字节` : '未记录'
const eventTime = value => value ? new Date(Number(value)>1e12?Number(value):Number(value)*1000).toLocaleTimeString('zh-CN',{hour12:false}) : '—'
const eventKey = e => e._display_id || e.event_id || e.id || e.seq
const rawEventKey = e => e.event_id || e.id || e.seq
const eventCategory = e => String(e.type || '').split('.',1)[0] || 'system'
const operationKey = e => { const category=eventCategory(e), id=category==='tool'?(e.payload?.tool_call_id||e.payload?.part_id||e.payload?.call_id):category==='model'?(e.payload?.message_id||e.payload?.call_id):''; return id?`${category}:${id}`:'' }
const isEventError = e => eventCategory(e)==='error'||e.type?.endsWith('.error')||['error','failed'].includes(e.status)
const eventIcon = e => eventCategory(e)==='tool'?Setting:isEventError(e)?WarningFilled:eventCategory(e)==='result'||e.type==='session.completed'?CircleCheckFilled:Document
const eventTitle = e => e.type === 'session.compaction' ? '上下文压缩' : ({session:'会话状态',action:'执行说明',model:'模型请求',tool:'工具调用',result:'审查结果',error:'执行异常',heartbeat:'执行器心跳',system:'系统记录',text:'模型说明'}[eventCategory(e)]||e.type||'执行记录')
const eventDuration = e => Number.isFinite(e.payload?.duration_ms)?`${(e.payload.duration_ms/1000).toFixed(1)} 秒`:Number.isFinite(e.payload?.duration_seconds)?`${e.payload.duration_seconds} 秒`:''
const eventDetails = e => e.payload&&Object.keys(e.payload).length?JSON.stringify(e.payload,null,2):''
const findingFields = [{key:'trigger',label:'触发条件'},{key:'explanation',label:'原因'},{key:'impact',label:'影响'},{key:'recommendation',label:'修改建议'},{key:'verification',label:'反证结果'},{key:'before_behavior',label:'修改前'},{key:'after_behavior',label:'修改后'}]
const operations = {find_files:'查找文件',search_code:'搜索代码',read_file:'读取源码'}

function eventParams(extra={}){const types=eventFilter.value==='tool'?'tool.running,tool.completed,tool.error':eventFilter.value==='error'?'error,session.error,session.failed,session.event_error,tool.error,model.error':undefined;return{limit:200,session_id:eventSession.value || undefined,type:types,q:searchQuery.value||undefined,...extra}}
function mergeEvents(incoming,countAsNew=false){
  const oldRaw=new Set(events.value.map(rawEventKey)),rows=[...events.value],operations=new Map();let added=0
  rows.forEach((row,index)=>{const key=operationKey(row);if(key)operations.set(key,index)})
  for(const item of incoming){const op=operationKey(item),index=op&&operations.has(op)?operations.get(op):-1;if(index>=0){if((item.seq??0)>(rows[index].seq??0))rows[index]={...rows[index],...item,created_at:rows[index].created_at??item.created_at,_display_id:eventKey(rows[index]),_sort_seq:rows[index]._sort_seq??rows[index].seq}}else if(!oldRaw.has(rawEventKey(item))){rows.push({...item,_sort_seq:item.seq});oldRaw.add(rawEventKey(item));added++;if(op)operations.set(op,rows.length-1)}}
  events.value=rows.sort((a,b)=>(a._sort_seq??a.seq??0)-(b._sort_seq??b.seq??0))
  if(incoming.length){newestSeq.value=Math.max(newestSeq.value,...incoming.map(e=>e.seq||0));const positive=incoming.map(e=>e.seq||0).filter(Boolean);if(positive.length)oldestSeq.value=oldestSeq.value?Math.min(oldestSeq.value,...positive):Math.min(...positive)}
  if(countAsNew&&!following.value)pendingCount.value+=added
}
async function fetchEvents(mode='initial'){
  if(!props.open||!props.task?.id||requestingEvents.value)return
  const current=generation,taskId=props.task.id,scroller=eventScroller.value,oldHeight=scroller?.scrollHeight||0
  requestingEvents.value=true;if(mode==='initial')loadingEvents.value=true;if(mode==='history')loadingHistory.value=true
  eventController?.abort();eventController=new AbortController()
  try{const cursor=mode==='poll'&&newestSeq.value?{after:newestSeq.value}:mode==='history'&&oldestSeq.value?{before:oldestSeq.value}:{};const r=await api.get(`/ai-review/tasks/${taskId}/events`,{params:eventParams(cursor),signal:eventController.signal,notifyError:mode!=='poll'});if(current!==generation)return;mergeEvents(Array.isArray(r.data.items)?r.data.items:[],mode==='poll');if(mode!=='poll')hasOlder.value=!!r.data.has_more;connectionOk.value=true;eventError.value='';await nextTick();if(mode==='history'&&scroller)scroller.scrollTop+=scroller.scrollHeight-oldHeight;else if(following.value)scrollToLatest(false)}
  catch(e){if(e?.name!=='CanceledError'&&e?.code!=='ERR_CANCELED'&&current===generation){connectionOk.value=false;eventError.value=e.response?.status===404&&!props.task?.session?'此旧任务没有记录原生执行日志。':e.response?.data?.error||'执行记录暂时无法连接'}}
  finally{if(current===generation){requestingEvents.value=false;loadingEvents.value=false;loadingHistory.value=false}}
}
function schedulePoll(){clearTimeout(eventTimer);if(!props.open||!shouldPollEvents.value)return;eventTimer=setTimeout(async()=>{await fetchEvents('poll');schedulePoll()},document.hidden?5000:1000)}
function resetEvents(){generation++;eventController?.abort();requestingEvents.value=false;loadingEvents.value=false;loadingHistory.value=false;events.value=[];newestSeq.value=0;oldestSeq.value=0;hasOlder.value=false;eventError.value='';connectionOk.value=false;fetchEvents().then(schedulePoll)}
function applySearch(){searchQuery.value=searchDraft.value.trim();resetEvents()}
function toggleEvent(e){const key=eventKey(e),next=new Set(expandedEvents.value);next.has(key)?next.delete(key):next.add(key);expandedEvents.value=next}
function onEventScroll(){const el=eventScroller.value;if(!el)return;scrollTop.value=el.scrollTop;viewportHeight.value=el.clientHeight;following.value=el.scrollHeight-el.scrollTop-el.clientHeight<48;if(following.value)pendingCount.value=0;if(el.scrollTop<100&&hasOlder.value&&!loadingHistory.value)fetchEvents('history')}
function scrollToLatest(smooth=true){nextTick(()=>{const el=eventScroller.value;if(el){if(typeof el.scrollTo==='function')el.scrollTo({top:el.scrollHeight,behavior:smooth?'smooth':'auto'});else el.scrollTop=el.scrollHeight}following.value=true;pendingCount.value=0})}
function resumeFollowing(){scrollToLatest()}
async function downloadEvents(){try{const r=await api.get(`/ai-review/tasks/${props.task.id}/events/export`,{params:eventParams(),responseType:'blob'});const url=URL.createObjectURL(r.data),a=document.createElement('a');a.href=url;a.download=`ai-review-events-${props.task.id}.jsonl`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){eventError.value=e.response?.data?.error||'脱敏日志下载失败'}}
async function copyDiagnostic(){try{await navigator.clipboard.writeText(diagnosticText.value);diagnosticCopied.value=true;setTimeout(()=>diagnosticCopied.value=false,1500)}catch{diagnosticCopied.value=false}}
async function downloadRawDiagnostics(){if(rawDownloading.value||!rawDiagnostic.value?.available||rawExpired.value)return;const taskId=props.task.id,url=`/api/ai-review/tasks/${taskId}/raw-diagnostics`;rawDownloading.value=true;rawDownloadError.value='';try{await api.head(`/ai-review/tasks/${taskId}/raw-diagnostics`);if(props.task?.id!==taskId)return;const a=document.createElement('a');a.href=url;a.download=`ai-review-raw-diagnostics-${taskId}.zip`;a.click()}catch(e){if(props.task?.id!==taskId)return;const data=e?.response?.data;rawDownloadError.value=(typeof data?.error==='string'?data.error:'')||'原始诊断包下载失败，请在当前任务中重试或查看后台容器日志。'}finally{if(props.task?.id===taskId)rawDownloading.value=false}}
async function toggleFullscreen(){try{if(!document.fullscreenElement)await panel.value?.requestFullscreen();else await document.exitFullscreen()}catch{isFullscreen.value=!isFullscreen.value}}
function syncFullscreen(){isFullscreen.value=document.fullscreenElement===panel.value}
async function loadReads(){if(reading.value||!props.open||!props.task?.id)return;const current=readGeneration,id=props.task.id;reading.value=true;readError.value='';try{const r=await api.get(`/ai-review/tasks/${id}/context-reads`,{params:{page:readPage.value,start:reads.value.length+1}});if(current!==readGeneration)return;reads.value.push(...r.data.items);readTotal.value=r.data.total;readPage.value++}catch(e){if(current===readGeneration)readError.value=e.response?.data?.error||'读取记录暂时无法加载'}finally{if(current===readGeneration)reading.value=false}}

watch(eventFilter,resetEvents)
watch(eventSession,resetEvents)
watch(()=>props.task?.session_id, (value, previous)=>{ if(value !== previous) resetEvents() })
watch(hasNativeEvents,(native,wasNative)=>{if(native&&!wasNative&&props.open&&props.task?.id)resetEvents();else if(!native&&wasNative){generation++;eventController?.abort();clearTimeout(eventTimer);requestingEvents.value=false;loadingEvents.value=false;loadingHistory.value=false;events.value=[];newestSeq.value=0;oldestSeq.value=0;connectionOk.value=false}})
watch([()=>props.task?.id,()=>props.open],([id,open],[oldId,oldOpen])=>{if(id===oldId&&open===oldOpen)return;const reloadReads=activeTab.value==='coverage'||activeTab.value==='context';generation++;readGeneration++;eventController?.abort();clearTimeout(eventTimer);events.value=[];newestSeq.value=0;oldestSeq.value=0;requestingEvents.value=false;loadingEvents.value=false;loadingHistory.value=false;reads.value=[];readTotal.value=0;readPage.value=1;reading.value=false;readError.value='';rawDownloading.value=false;rawDownloadError.value='';expandedEvents.value=new Set();following.value=true;pendingCount.value=0;activeTab.value=continuing.value?'process':'result';if(open&&id&&hasNativeEvents.value)fetchEvents().then(schedulePoll);if(open&&id&&reloadReads)loadReads()})
watch(activeTab,tab=>{if(tab==='context')activeTab.value='coverage';if((tab==='coverage'||tab==='context')&&readPage.value===1)loadReads()})
watch(()=>retrieval.value.operations,count=>{if((activeTab.value==='coverage'||activeTab.value==='context')&&count>readTotal.value){if(reads.value.length===readTotal.value)loadReads();else readTotal.value=count}})
onMounted(()=>{activeTab.value=continuing.value?'process':'result';document.addEventListener('visibilitychange',schedulePoll);document.addEventListener('fullscreenchange',syncFullscreen);if(props.open&&props.task?.id&&hasNativeEvents.value)fetchEvents().then(schedulePoll)})
onBeforeUnmount(()=>{generation++;readGeneration++;eventController?.abort();clearTimeout(eventTimer);document.removeEventListener('visibilitychange',schedulePoll);document.removeEventListener('fullscreenchange',syncFullscreen)})
</script>

<style scoped>
.diff-report { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; padding: 16px; }
.review-detail{min-width:0;overflow-wrap:anywhere;background:rgba(255,255,255,.82);border:1px solid rgba(148,163,184,.2);border-radius:16px;box-shadow:0 12px 36px rgba(15,23,42,.08);padding:0 20px 22px}
.review-detail.is-fullscreen{overflow:auto;border-radius:0;padding:0 28px 24px;background:#f8fafc}
.detail-bar,.identity-row,.detail-controls,.identity-actions,.process-heading,.process-actions,.event-title,.stream-footer{display:flex;align-items:center;gap:10px;flex-wrap:wrap;min-width:0}
.detail-bar{position:sticky;top:0;z-index:2;background:rgba(255,255,255,.96);justify-content:space-between;border-bottom:1px solid #e5eaf1;padding:12px 0}
.detail-bar h2{font-size:20px;margin:2px 0}
.execution-summary{margin:12px 0}.execution-summary>summary{cursor:pointer;color:#52637b;font-size:13px;padding:8px 0}.execution-summary[open]>summary{margin-bottom:8px}
.detail-title{display:flex;align-items:center;gap:12px;flex-wrap:wrap;min-width:0}
.failure-card>summary,.raw-diagnostics>summary{cursor:pointer;line-height:1.6}
@container review-page (min-width: 961px) { .identity-copy strong{font-size:17px}.metrics>div{padding:4px 8px}.metrics strong{font-size:19px}.detail-bar h2{font-size:16px}.stage-track{font-size:12px}.stage-track li{gap:5px}.stage-track li::after{margin:0 4px}.action-card h3{font-size:18px} }
.failure-card{margin:12px 0;padding:14px;border:1px solid #f4b8b8;border-radius:10px;background:#fff7f7;color:#8f1d1d;min-width:0}
.failure-heading{display:flex;align-items:flex-start;gap:10px;min-width:0}
.failure-heading>svg{width:20px;height:20px;flex:0 0 20px;margin-top:2px}
.failure-heading strong,.failure-heading p{overflow-wrap:anywhere;white-space:pre-wrap}
.failure-heading p{margin:5px 0 0;color:#9f3a38}
.failure-heading>div{min-width:0}.full-error{margin-top:6px}.full-error pre{max-height:240px;overflow:auto}
.failure-fields{margin:14px 0 4px;padding-top:12px;border-top:1px solid #f2cccc}
.failure-fields dt{color:#8f5555}.failure-fields dd{overflow-wrap:anywhere}
.failure-fields.more-fields{margin-top:4px}
.legacy-diagnostic{margin:12px 0 0;color:#8f5555}
.failure-card details pre{max-height:320px;overflow:auto;color:#1f2937;background:#fff}
.raw-diagnostics{margin:12px 0;padding:14px;border:1px solid #e6c98f;border-radius:10px;background:#fffaf0;color:#1f2937;min-width:0}.raw-diagnostics>div>p{margin:5px 0;color:#7c4a03}.raw-meta{margin:10px 0}.raw-warning{color:#7c4a03;line-height:1.65;overflow-wrap:anywhere}
.detail-bar h2 span,.eyebrow,.secondary,.process-heading p,.identity-copy span,.event-main p,.event-title span,.event-row time{color:#64748b}
.eyebrow{font-size:12px;margin:0}
.connection,.stream-footer span{display:inline-flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;flex:0 0 8px}
.dot.online{background:#19a05a}
.dot.error{background:#dc2626}
.dot.waiting{background:#d97706}
.identity-row{justify-content:space-between;padding:12px 0 10px}
.identity-copy{display:flex;flex-direction:column;gap:5px}
.identity-copy strong{font-size:20px}
.action-card{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 18px;border-radius:10px;background:linear-gradient(110deg,#eef6ff,#f7fbff);border:1px solid #e0edff}
.action-card span{font-size:13px;color:#1765be}
.action-card h3{font-size:22px;margin:5px 0}
.action-card p{margin:0;color:#52647b}
.action-state{display:flex;align-items:center;gap:9px;color:#0b69d1;flex-shrink:0}
.action-state .el-icon{font-size:22px}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:12px 0}
.metrics>div{display:flex;flex-direction:column;gap:5px;padding:4px 22px;border-right:1px solid #dfe5ed}
.metrics>div:last-child{border-right:0}
.metrics span,.metrics small{color:#64748b;font-size:12px}
.metrics strong{font-size:22px}
.stage-track{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));list-style:none;margin:8px 0 14px;padding:0}
.stage-track li{display:flex;align-items:center;gap:10px;min-width:0}
.stage-track li::after{content:'';height:3px;background:#dfe5ed;flex:1;order:2;margin:0 10px}
.stage-track li:last-child::after{display:none}
.stage-track li>span{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:#e3e8ef;color:#4b5b72;flex:0 0 26px}
.stage-track li>div{display:flex;flex-direction:column;min-width:max-content}
.stage-track small{color:#64748b}
.stage-track .done>span{background:#26935a;color:#fff}
.stage-track .done::after,.stage-track .active::after{background:#6cacf2}
.stage-track .active>span{background:#0b69d1;color:#fff}
.stage-track .active small{color:#0b69d1}
.stage-track .error>span{background:#dc2626;color:#fff}
.process-heading{justify-content:space-between;margin-bottom:10px}
.process-heading h3{margin:0}
.process-heading p{font-size:13px;margin:5px 0}
.process-actions .el-input{width:220px}
.event-scroller{height:min(54vh,620px);min-height:360px;overflow:auto;border-block:1px solid #e8edf3;scrollbar-gutter:stable}
.new-events{position:sticky;top:8px;z-index:3;display:block;margin:8px auto;padding:8px 14px;border:1px solid #bcd8fa;border-radius:18px;background:#eef6ff;color:#075eb9;cursor:pointer}
.history-loading{text-align:center;color:#64748b;padding:8px}
.virtual-space{min-width:680px}
.event-row{display:grid;grid-template-columns:76px 30px minmax(0,1fr);gap:10px;padding:10px 8px;border-bottom:1px solid #e8edf3;overflow:hidden;box-sizing:border-box}
.event-row time{font-size:12px;padding-top:5px}
.event-dot{display:flex;align-items:flex-start;justify-content:center;color:#0b69d1;padding-top:2px}
.event-dot .el-icon{font-size:19px}
.event-main{min-width:0}
.event-title{min-height:24px}
.event-title code{background:#eef2f7;padding:3px 8px;border-radius:10px;max-width:360px;overflow:hidden;text-overflow:ellipsis}
.event-main p{font-size:13px;margin:4px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.event-main pre{max-height:76px;overflow:auto;margin:5px 0;padding:7px}
.event-row.is-error,.event-row.is-error .event-dot{color:#b91c1c;background:#fff8f8}
.event-row.is-running{background:#f3f8ff}
.stream-footer{position:sticky;bottom:0;justify-content:space-between;padding:8px;background:#f8fbff;color:#64748b;font-size:12px}
.runtime-health{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:12px 0 20px}
.runtime-health>div{display:flex;flex-direction:column;gap:5px;padding:14px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc}
.finding-title,.coverage-title{display:flex;gap:10px;align-items:center;flex-wrap:wrap;min-width:0;padding:10px 0;text-align:left}
.finding-content{padding:0 4px 12px;line-height:1.75}
.path{font-family:monospace}
.call-timing{display:flex;flex-wrap:wrap;gap:6px 16px;margin:10px 0}
dl{display:grid;grid-template-columns:120px minmax(0,1fr);gap:12px 16px}
dt{color:#64748b}
dd{margin:0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:14px;border-radius:8px;background:#f4f6f9;font-size:12px;line-height:1.65}
summary{cursor:pointer;color:#3c608b;padding:8px 0}
.el-alert{margin:12px 0}
.coverage-reason{border-bottom:1px solid #e7ebf0;padding:10px 0 16px}
.coverage-reason small{color:#64748b}
.diagnostics{padding-left:20px}
.diagnostics li{margin:12px 0}
.diagnostics strong,.diagnostics span{display:block}
.read-warning{color:#956006}
.evidence-count{line-height:1.7}
:deep(.el-collapse-item__header){height:auto;min-height:52px}
:deep(.el-tabs__item){padding:0 18px}

@media(max-width:900px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.metrics>div{border-right:0;border-bottom:1px solid #dfe5ed;padding:8px}
.process-heading{align-items:flex-start}
.process-actions{width:100%}
.stage-track{gap:8px}
.stage-track li::after{display:none}
}

@media(max-width:600px){.review-detail,.review-detail.is-fullscreen{padding:0 12px 18px;border-radius:10px}
.detail-bar,.identity-row{align-items:flex-start}
.detail-controls,.identity-actions{width:100%;justify-content:space-between}
.identity-copy strong{font-size:17px}
.action-card{align-items:flex-start}
.action-card h3{font-size:18px}
.action-state strong{display:none}
.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
.metrics strong{font-size:18px}
.stage-track{grid-template-columns:minmax(0,1fr);gap:10px}
.stage-track li>div{min-width:0}
.process-actions{display:grid;grid-template-columns:minmax(0,1fr)}
.process-actions .el-input{width:100%}
.event-scroller{min-height:420px}
.virtual-space{min-width:0}
.event-row{grid-template-columns:58px 24px minmax(0,1fr);padding-inline:2px}
.event-title code{max-width:150px}
.runtime-health{grid-template-columns:minmax(0,1fr)}
dl{grid-template-columns:minmax(0,1fr);gap:4px}
dd{margin-bottom:10px}
:deep(.el-tabs__nav-wrap.is-scrollable){padding:0}
:deep(.el-tabs__nav-prev),:deep(.el-tabs__nav-next),:deep(.el-tabs__active-bar){display:none}
:deep(.el-tabs__nav){display:flex;flex-wrap:wrap;width:100%;transform:none!important}
:deep(.el-tabs__item){flex:1 1 50%;padding:0 5px;justify-content:center}
:deep(.el-tabs__item.is-active){background:#eef4fc;border-radius:6px}
}

</style>
