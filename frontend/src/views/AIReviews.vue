<template>
  <div class="review-page">
    <div v-if="tab !== 'repositories'" class="section-header review-toolbar">
      <div><h3 class="section-title">{{ sectionTitle }}</h3><p class="muted">{{ tab === 'tasks' ? '从审查结果到问题反馈，一处查看。' : 'AI Code Review · 管理与配置' }}</p></div>
      <div class="review-actions"><el-tag :type="config.enabled ? 'success' : 'info'">{{ config.enabled ? '已启用' : '全局关闭' }}</el-tag><el-button :icon="Refresh" @click="refresh">刷新</el-button><el-button v-if="tab === 'tasks'" type="primary" :icon="Plus" :disabled="!config.enabled || !availableRepos.length" @click="startManual">发起审查</el-button></div>
    </div>
    <el-alert v-if="error || tasksError" :title="error || tasksError" type="error" show-icon closable @close="error = ''; tasksError = ''" />
    <el-alert v-if="isPlatformAdmin && workerStale" title="Review 工作进程尚未就绪或心跳已过期，自动审查可能暂停。" type="warning" show-icon :closable="false" />
    <el-alert v-if="isPlatformAdmin && progressStale" title="PR 进度同步进程尚未就绪或心跳已过期，审查仍可执行，但 Gitea 页面状态可能延迟。" type="warning" show-icon :closable="false" />
    <el-alert v-if="isPlatformAdmin && feedbackStale" title="人工反馈同步进程尚未就绪或心跳已过期，样本来源超过有效期后将暂停参考；自动审查仍可执行。" type="warning" show-icon :closable="false" />
    <section v-if="tab === 'tasks'">
      <div class="review-filters glass-card">
        <el-select v-model="filterRepo" clearable filterable placeholder="全部仓库" aria-label="筛选仓库" class="selector" @change="changeFilters"><el-option v-for="r in repositories" :key="r.id" :value="r.id" :label="`${r.server_name} / ${r.repo_name}${r.access_archived ? '（已归档）' : ''}`" /></el-select>
        <el-select v-model="preset" aria-label="统计时间范围" class="period-selector" @change="changePeriod"><el-option label="近 7 天" value="7" /><el-option label="近 30 天" value="30" /><el-option label="近 90 天" value="90" /><el-option label="自定义日期" value="custom" /></el-select>
        <el-date-picker v-if="preset === 'custom'" v-model="dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" :clearable="false" @change="changeFilters" />
        <span class="muted">{{ timezone }} · 按任务创建日</span>
      </div>
      <ReviewStatistics :data="statistics" :loading="statisticsLoading" :error="statisticsError" :collapsed="chartsCollapsed" @toggle="chartsCollapsed = !chartsCollapsed" />
      <div v-if="!repositories.length" class="glass-card empty-guide"><h4>开始你的第一次审查</h4><p>接入仓库后，创建或更新 PR 即可自动审查。</p><el-button v-if="isPlatformAdmin" type="primary" @click="router.push('/ai-reviews/repositories')">接入仓库</el-button><p v-else>请联系平台管理员接入仓库。</p></div>
      <div ref="workspace" class="review-workspace" :class="{ 'with-detail': detailVisible }">
        <aside v-if="detailVisible" class="task-rail glass-card" aria-label="审查任务列表">
          <div class="rail-heading"><strong>审查任务</strong><span class="muted">{{ total }} 次</span></div>
          <el-select v-model="filterStatus" clearable placeholder="全部状态" aria-label="筛选任务状态" @change="changeStatus"><el-option v-for="(label, value) in taskStates" :key="value" :value="value" :label="label" /></el-select>
          <div class="rail-rows"><button v-for="row in tasks" :key="row.id" type="button" class="task-choice" :class="{ selected: selectedTask === row.id }" :aria-current="selectedTask === row.id ? 'true' : undefined" @click="openTask(row.id)"><span class="task-choice-title">{{ repoName(row.repository) }}</span><span class="task-choice-meta"><strong>PR #{{ row.number }}</strong><el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag></span><span class="muted">{{ fmt(row.created) }}</span></button><p v-if="!tasks.length" class="muted">当前筛选没有任务；已打开的详情仍可查看。</p></div>
          <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, next, total" @current-change="changePage" />
        </aside>
        <div v-show="!detailVisible" class="glass-card panel task-table-panel">
          <div class="review-toolbar"><h4>审查任务 <span class="muted">{{ total }} 次</span></h4><el-select v-model="filterStatus" clearable placeholder="全部状态" aria-label="筛选任务状态" class="selector" @change="changeStatus"><el-option v-for="(label, value) in taskStates" :key="value" :value="value" :label="label" /></el-select></div>
          <el-table :data="tasks" :row-class-name="({row}) => row.id === lastSelected ? 'last-selected' : ''" stripe max-height="var(--table-max-height)">
            <el-table-column label="仓库 / PR" min-width="230"><template #default="{ row }">{{ repoName(row.repository) }} #{{ row.number }}</template></el-table-column>
            <el-table-column label="代码版本" min-width="135"><template #default="{ row }"><code>{{ row.progress?.head?.slice(0, 12) || '等待读取' }}</code></template></el-table-column>
            <el-table-column label="状态" min-width="135"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ row.progress?.label || statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="当前阶段 / 成本确认" min-width="330"><template #default="{ row }">
              <div>{{ row.stage }}</div><div v-if="row.progress?.units_total != null" class="muted">完成单元 {{ row.progress.units_completed }}/{{ row.progress.units_total }}</div><div v-if="row.progress?.sync_error" class="failure">状态同步：{{ row.progress.sync_error }}</div>
              <div v-if="row.status === 'awaiting_cost' && row.cost_summary" class="cost-summary" data-testid="cost-summary">
                <strong>基础调用 {{ row.cost_summary.base_calls }} 次，最多 {{ row.cost_summary.max_calls }} 次</strong>
                <span>主审 {{ row.cost_summary.primary }} 批 · 关联复核 {{ row.cost_summary.association }} 批 · 历史问题 {{ row.cost_summary.history }} 项 · 已授权 {{ row.cost_summary.authorized_calls }} 次</span>
                <span>主审及关联初始输入 {{ row.cost_summary.input_tokens }} Token／估算单位；每次输出额度 {{ row.cost_summary.output_tokens }} Token</span>
                <span>模型 {{ row.cost_summary.model || '未记录' }} · {{ row.cost_summary.basis || '保守计数' }}{{ row.cost_summary.tokenizer_model ? '（' + row.cost_summary.tokenizer_model + '）' : '' }} · 资源版本 {{ row.cost_summary.revision || '未知' }}</span>
                <span>文本变更 {{ row.cost_summary.changed_lines }} 行 · 有效执行预算 {{ Math.round(row.cost_summary.budget_seconds) }} 秒 · 未提供单价，无法估算金额</span>
                <span v-if="row.cost_summary.gap_count || row.cost_summary.unsupported_count" class="failure">覆盖缺口 {{ row.cost_summary.gap_count }} 项 · 未支持内容 {{ row.cost_summary.unsupported_count }} 项，请查看详情</span>
                <div><el-button type="primary" size="small" :loading="confirmingIds.has(row.id)" :disabled="!config.enabled || repoArchived(row.repository)" :aria-label="`确认 ${repoName(row.repository)} PR #${row.number} 的成本计划`" @click="confirmCost(row)">确认此计划（最多 {{ row.cost_summary.max_calls }} 次）</el-button></div>
              </div>
              <div v-else-if="row.status === 'awaiting_cost'" class="failure">成本摘要暂不可用，请刷新或查看详情。</div>
            </template></el-table-column>
            <el-table-column label="执行耗时 / 批次" min-width="150"><template #default="{ row }">{{ row.timing ? Math.round(row.timing.execution_seconds) + ' 秒' : '历史记录' }}<div class="muted" v-if="row.batch_protocol">{{ row.progress?.units_completed || 0 }} / {{ row.progress?.units_total ?? '预检中' }}</div></template></el-table-column>
            <el-table-column label="创建时间 ↓" min-width="195"><template #default="{ row }">{{ fmt(row.created) }}<div class="muted">更新：{{ fmt(row.updated) }}</div></template></el-table-column>
            <el-table-column label="操作" min-width="250"><template #default="{ row }"><div class="review-actions"><el-button link type="primary" @click="openTask(row.id)">详情</el-button><el-button v-if="!repoArchived(row.repository) && ['queued', 'running'].includes(row.status)" link type="danger" :loading="cancellingId === row.id" :disabled="Boolean(cancellingId)" @click="cancelTask(row)">强制取消</el-button><el-tag v-if="repoArchived(row.repository)" type="info">已归档</el-tag><template v-else-if="row.status === 'publish_unknown'"><el-button link type="warning" @click="reconcile(row)">核对发布</el-button><el-button link type="warning" @click="confirmUnpublished(row)">确认未发布</el-button></template><el-button v-else link :disabled="!config.enabled || row.status === 'cancelling'" @click="rerun(row.repository, row.number)">重新审查</el-button></div></template></el-table-column>
            <template #empty><el-empty description="启用仓库后，创建或更新 PR 即可自动审查。" /></template>
          </el-table>
          <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="changePage" />
        </div>
    <ReviewTaskDetail v-if="detailVisible" :open="detailVisible" :task="detail" :loading="detailLoading" :error="detailError" :cancelling="cancellingId === detail?.id" :can-cancel="!repoArchived(detail?.repository)" @cancel="cancelTask(detail)" @refresh="fetchDetail(selectedTask)" :repository="repositories.find(r => r.id === detail?.repository)?.repo_name || '仓库'" :instance="repositories.find(r => r.id === detail?.repository)?.server_name || ''" @close="closeDetail"><template #actions><template v-if="detail && !repoArchived(detail.repository)"><template v-if="detail.status === 'publish_unknown'"><el-button size="small" type="warning" plain @click="reconcile(detail)">核对发布</el-button><el-button size="small" @click="confirmUnpublished(detail)">确认未发布</el-button></template><el-button v-else size="small" :disabled="!config.enabled || detail.status === 'cancelling'" :loading="rerunning" @click="rerun(detail.repository, detail.number)">重新审查</el-button></template></template></ReviewTaskDetail>
      </div>
    </section>
    <ReviewRepositories v-else-if="tab === 'repositories'" :repositories="repositories" :servers="servers" :config="config" :refreshing="refreshing" @refresh="refresh" @configure="editRepo" @webhook="row => { hookRepo = row; hookVisible = true }" @review="row => { manualRepo = row; manualVisible = true }" @download="row => downloadProgress(row.server_id)" />
    <section v-else-if="tab === 'dataset'"><AIReviewDataset :repositories="repositories" /></section>
    <section v-else-if="tab === 'config' && isPlatformAdmin">
        <div class="glass-card panel">
          <el-alert v-if="configError" :title="configError" type="error" show-icon :closable="false" /><el-form label-position="top" @submit.prevent="saveConfig">
            <el-alert title="新任务仅审查 PR diff" description="直接提供 PR 标题、描述与完整 diff，关闭工程检索，以文字报告结束并发布到 PR。未读取完整工程，未验证 diff 外依赖。" type="info" :closable="false" />
            <div class="review-grid">
              <el-form-item label="全局启用"><el-switch v-model="config.enabled" aria-label="全局启用 AI Review" /></el-form-item>
              <el-form-item label="执行引擎"><el-select v-model="config.engine" aria-label="执行引擎"><el-option label="原生 OpenCode 会话" value="opencode_native" /></el-select></el-form-item>
              <el-form-item label="内部 New API 地址"><el-input v-model="config.api_base_url" placeholder="填写包含 API 前缀的内网地址" autocomplete="off" /></el-form-item>
              <el-form-item label="模型"><el-input v-model="config.model" placeholder="填写内部实际可用的模型标识" autocomplete="off" /></el-form-item>
              <el-form-item :label="config.api_key_configured ? 'API Key（已配置，留空保留）' : 'API Key（尚未配置）'"><el-input v-model="apiKey" type="password" autocomplete="new-password" placeholder="仅在新增或更换密钥时填写" /></el-form-item>
              <el-form-item label="思考控制协议"><el-select v-model="config.thinking_protocol" @change="value => { if (value === 'provider_default') config.thinking_enabled = false }"><el-option label="服务默认（未控制）" value="provider_default" /><el-option label="Qwen / vLLM（含 New API 透传）" value="qwen_vllm" /><el-option label="DeepSeek 官方协议" value="deepseek" /><el-option label="reasoning_effort 兼容协议" value="reasoning_effort" /></el-select><span class="secondary">按实际服务选择；New API 必须支持透传。服务默认不代表已关闭思考。</span></el-form-item>
              <el-form-item label="启用深度思考"><el-switch v-model="config.thinking_enabled" :disabled="config.thinking_protocol === 'provider_default'" /><span class="secondary">默认关闭；仅对新任务生效。参数被接受不代表服务实际遵守。</span></el-form-item>
              <el-form-item label="思考额度（Token）"><el-input-number v-model="config.thinking_token_budget" :min="128" :max="32768" :precision="0" :disabled="!config.thinking_enabled || config.thinking_protocol !== 'qwen_vllm'" /><span class="secondary">仅 Qwen / vLLM 独立思考预算；需要服务支持，其他协议不发送此额度。</span></el-form-item>
              <el-form-item label="连续 Push 合并窗口（秒）"><el-input-number v-model="config.debounce_seconds" :min="0" :max="120" /></el-form-item>
            </div>
            <el-collapse><el-collapse-item title="审查资源预算" name="limits"><div class="review-grid">
              <el-form-item label="Token 计数方式"><el-select v-model="config.tokenizer_profile"><el-option label="自动匹配具体型号" value="auto" /><el-option label="Qwen3.8-27B" value="qwen38" /><el-option label="DeepSeek-V4.1-Flash" value="deepseek41" /><el-option label="通用保守估算" value="conservative" /></el-select><span class="secondary">计数资源离线运行，不修改模型名或思考参数；实际资源版本在任务计划中显示。</span></el-form-item>
              <el-form-item label="单批输入目标（Token）"><el-input-number v-model="config.batch_input_tokens" :min="4096" :max="1048576" /><span class="secondary">新安装默认 65536；实际额度还会扣除输出预留和安全余量。</span></el-form-item>
              <el-form-item label="自动执行调用上限"><el-input-number v-model="config.batch_auto_calls" :min="1" :max="10000" /><span class="secondary">最大计划调用数超出时等待确认，包含反证与历史复核。</span></el-form-item>
              <el-form-item label="分批有效执行预算（秒）"><el-input-number v-model="config.batch_execution_seconds" :min="60" :max="604800" /></el-form-item>
              <el-form-item label="预处理超时（秒）"><el-input-number v-model="config.batch_preprocess_seconds" :min="10" :max="3600" /></el-form-item>
              <el-form-item label="计数进程内存上限（MiB）"><el-input-number v-model="config.batch_memory_mib" :min="256" :max="8192" /></el-form-item>
              <el-form-item label="历史单会话审查上限（秒）"><el-input-number v-model="config.review_timeout_seconds" :min="60" :max="3600" :precision="0" /><span class="secondary">首次默认 600 秒；旧配置沿用原任务预算。仅对保存后创建的新任务生效。</span></el-form-item>
              <el-form-item label="历史工程检索额度"><el-input-number v-model="config.context_retrieval_budget" :min="1" :max="200" :precision="0" disabled /><span class="secondary">仅供历史工程审查使用；新任务没有工具调用，不消耗检索额度。</span></el-form-item>
              <el-form-item label="模型上下文窗口（Token）"><el-input-number v-model="config.model_context_tokens" :min="4096" :max="1048576" :precision="0" /><span class="secondary">默认 262,144，请填写模型服务实际开放的窗口大小。</span></el-form-item>
              <el-form-item label="历史会话压缩阈值（Token）"><el-input-number v-model="config.compaction_threshold_tokens" :min="4096" :max="1048576" :precision="0" /><span class="secondary">新任务按固定 diff 分批处理；此项只用于历史会话，并需与窗口配置兼容。</span></el-form-item>
              <el-form-item label="单次模型请求超时（秒）"><el-input-number v-model="config.model_request_timeout_seconds" :min="60" :max="3600" :precision="0" /></el-form-item>
              <el-form-item label="模型输出额度（Token）"><el-input-number v-model="config.model_max_output_tokens" :min="0" :max="1048576" :precision="0" /><span class="secondary">新安装默认 32768；设为 0 时分批任务使用 8192。非零额度可能同时包含思考与正文，不能替代独立思考预算。</span></el-form-item>
              <el-form-item label="正文大小上限（MiB）"><el-input-number v-model="config.model_response_max_mib" :min="1" :max="64" :precision="0" /><span class="secondary">限制审查报告正文，不累计心跳和进度事件。</span></el-form-item>
            </div><p class="muted" data-testid="time-budget-help">初始单次等待上限：{{ effectiveModelTimeout }} 秒，即单次模型请求上限与整次审查剩余期限减去 40 秒收尾时间中的较小值。实际可用时间会随准备耗时和后续调用递减。资源配置仅对新创建的任务生效，已排队和运行中的任务沿用原预算。</p></el-collapse-item></el-collapse>
            <div class="review-actions footer"><el-button type="primary" :loading="busy" native-type="submit">保存设置</el-button><el-button :loading="testing" @click="testConnection">检查已保存的连接</el-button><el-button :loading="testing" @click="testTokens">核对 Token 计数（4 次请求）</el-button></div>
          </el-form>
          <el-alert v-if="connectionMessage" :title="connectionMessage" :type="connectionOk ? 'success' : 'warning'" :closable="false" />
          <details v-if="connectionDiagnostic" class="connection-diagnostic" data-testid="connection-diagnostic">
            <summary>查看连接测试详情（已脱敏）</summary>
            <p>诊断编号：{{ connectionDiagnostic.diagnostic_id }} · {{ connectionDiagnostic.time_utc }}</p>
            <p>失败阶段：{{ connectionDiagnostic.phase }} · 接口：{{ connectionDiagnostic.request_path }}</p>
            <p>累计耗时：{{ connectionDiagnostic.elapsed_seconds }} 秒 · 连接超时：{{ connectionDiagnostic.connect_timeout_seconds }} 秒 · 读取超时：{{ connectionDiagnostic.read_timeout_seconds }} 秒</p>
            <p>错误码：{{ connectionDiagnostic.error_code }} · HTTP：{{ connectionDiagnostic.http_status ?? '未收到响应' }} · 异常类型：{{ connectionDiagnostic.exception_type || '响应校验失败' }}</p>
            <p>响应大小：{{ connectionDiagnostic.response_bytes }} 字节；响应摘要：</p>
            <pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ connectionDiagnostic.response_summary }}</pre>
          </details>
        </div>
    </section>

    <el-dialog append-to-body v-model="repoVisible" :title="repoForm.id ? '配置审查仓库' : '添加审查仓库'" width="min(640px, 95vw)" :close-on-click-modal="false">
      <el-alert v-if="repoError" :title="repoError" type="error" show-icon :closable="false" /><el-form label-position="top">
        <el-form-item label="Gitea 实例"><el-select v-model="repoForm.server_id" :disabled="!!repoForm.id" class="full" @change="catalog = []; repoForm.repo_name = ''; searchCatalog('')"><el-option v-for="s in servers" :key="s.id" :value="s.id" :label="s.name" /></el-select></el-form-item>
        <el-form-item label="仓库"><el-select v-model="repoForm.repo_name" :disabled="!!repoForm.id" filterable remote :remote-method="searchCatalog" :loading="catalogLoading" placeholder="搜索所有者或仓库名称" class="full"><el-option v-for="r in catalog" :key="r.id" :value="r.name" :label="r.name" /><el-option v-if="repoForm.id" :label="repoForm.repo_name" :value="repoForm.repo_name" /></el-select></el-form-item>
        <el-form-item v-if="isPlatformAdmin" label="专用机器人 Token（编辑时留空保留）"><el-input v-model="repoForm.bot_token" type="password" autocomplete="new-password" /></el-form-item>
        <div v-if="isPlatformAdmin" class="permission-guide"><strong>账号与 Token 的权限分别配置</strong><dl><dt>机器人账号</dt><dd>独立非管理员；仓库协作者选择 Read／只读。组织和团队也不能授予代码写入或管理权限。</dd><dt>Token 权限</dt><dd><code>read:user</code>、<code>write:repository</code>、<code>write:issue</code>。私有仓库不能选择仅公开资源。</dd></dl><p>Token 的写权限用于发布审查和进度评论，账号自身仍保持代码只读。已有 Token 满足要求时，可以继续使用。</p></div>
        <el-form-item label="启用该仓库"><el-switch v-model="repoForm.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="repoVisible = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveRepo">保存仓库</el-button></template>
    </el-dialog>
    <el-dialog append-to-body v-model="hookVisible" title="安装或核对 PR 事件通知" width="min(600px, 95vw)">
      <el-alert v-if="hookError" :title="hookError" type="error" show-icon :closable="false" /><p>{{ hookRepo?.server_name }} / {{ hookRepo?.repo_name }}</p><el-form label-position="top"><el-form-item label="Gitea 能够访问的 Manager 地址"><el-input v-model="managerURL" placeholder="填写 Manager 完整访问地址" /></el-form-item></el-form>
      <p class="muted">将使用现有服务器管理凭据安装 Webhook，并回读验证。该地址必须在部署白名单内。</p>
      <template #footer><el-button @click="hookVisible = false">取消</el-button><el-button type="primary" :loading="busy" @click="installHook">安装并验证</el-button></template>
    </el-dialog>
    <el-dialog append-to-body v-model="manualVisible" title="人工触发 PR 审查" width="min(460px, 95vw)"><el-alert v-if="manualError" :title="manualError" type="error" show-icon :closable="false" /><el-form label-position="top"><el-form-item label="仓库"><el-select v-model="manualRepo" value-key="id" aria-label="发起审查的仓库" class="full"><el-option v-for="r in availableRepos" :key="r.id" :value="r" :label="`${r.server_name} / ${r.repo_name}`" /></el-select></el-form-item><el-form-item label="PR 编号"><el-input-number v-model="manualNumber" :min="1" /></el-form-item></el-form><template #footer><el-button @click="manualVisible = false">取消</el-button><el-button type="primary" :loading="rerunning" :disabled="rerunning || !manualRepo" @click="rerun(manualRepo?.id, manualNumber)">开始审查</el-button></template></el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { api } from '../api'
import { useRoute, useRouter } from 'vue-router'
import ReviewStatistics from '../components/ReviewStatistics.vue'
import { isPlatformAdmin, scopedServers } from '../composables/accessState.js'
import AIReviewDataset from '../components/AIReviewDataset.vue'
import ReviewRepositories from '../components/ReviewRepositories.vue'
import ReviewTaskDetail from '../components/ReviewTaskDetail.vue'
import { errorMessage, statusType } from '../components/reviewDisplay'

const route = useRoute(), router = useRouter()
const tab = computed(() => route.meta.reviewSection || 'tasks')
const sectionTitle = computed(() => ({ tasks:'AI Code Review', repositories:'审查仓库', dataset:'审查经验数据集', config:'模型与设置' })[tab.value])
const refreshing = ref(false)
const error = ref(''), tasksError = ref(''), busy = ref(false), testing = ref(false), csrf = ref('')
const config = ref({ enabled: false }), apiKey = ref(''), health = ref([]), servers = ref([]), repositories = ref([])
const tasks = ref([]), total = ref(0), page = ref(Math.max(1, Number(route.query.page) || 1)), filterRepo = ref(Number(route.query.repository) || null), detail = ref(null), detailVisible = ref(false)
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
const dateString = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`
function recentDates(days) { const end = new Date(), start = new Date(); start.setDate(end.getDate() - days + 1); return [dateString(start), dateString(end)] }
const dates = ref(route.query.start && route.query.end ? [String(route.query.start), String(route.query.end)] : recentDates(30))
const preset = ref(String(route.query.period || (route.query.start ? 'custom' : '30'))), filterStatus = ref(String(route.query.status || ''))
const statistics = ref(null), statisticsError = ref(''), statisticsLoading = ref(false), chartsCollapsed = ref(Boolean(route.query.task)), workspace = ref()
const lastSelected = ref(String(route.query.last_task || route.query.task || ''))
let listChartsCollapsed = false
const selectedTask = computed(() => String(route.query.task || ''))
const availableRepos = computed(() => repositories.value.filter(r => r.enabled && !r.access_archived))
const taskStates = { awaiting_cost:'等待成本确认', budget_paused:'预算暂停', incomplete:'审查未完成', queued:'等待执行', running:'分析中', publishing:'发布中', recovering:'核对中', succeeded:'已完成', failed:'执行失败', cancelled:'已取消', cancelling:'取消中', superseded:'版本已过期', publish_unknown:'发布待核对' }
let statisticsController, tasksController, statisticsGeneration = 0, lastStatisticsPoll = 0
function scopeParams() { return { start: dates.value?.[0], end: dates.value?.[1], timezone, repository: filterRepo.value || undefined } }
function listQuery() { return { ...scopeParams(), period: preset.value, last_task: lastSelected.value || undefined, page: String(page.value), status: filterStatus.value || undefined } }
async function loadStatistics(silent = false) {
  const generation = ++statisticsGeneration
  statisticsController?.abort(); statisticsController = new AbortController(); statisticsLoading.value = true
  try { const r = await api.get('/ai-review/statistics', { params: scopeParams(), signal: statisticsController.signal, notifyError: !silent }); if (!disposed && generation === statisticsGeneration) { statistics.value = r.data; statisticsError.value = '' } }
  catch (e) { if (!disposed && generation === statisticsGeneration && e.code !== 'ERR_CANCELED') { statistics.value = null; statisticsError.value = errorMessage(e) } }
  finally { if (generation === statisticsGeneration) statisticsLoading.value = false }
  lastStatisticsPoll = Date.now()
}
function changePeriod() { if (preset.value !== 'custom') { dates.value = recentDates(Number(preset.value)); changeFilters() } }
async function changeFilters() { page.value = 1; statistics.value = null; tasks.value = []; await router.replace({ query: { ...route.query, ...listQuery() } }) }
async function changeStatus() { page.value = 1; await changePage() }
async function changePage() { await router.replace({ query: { ...route.query, ...listQuery() } }) }
watch(() => [route.query.start, route.query.end, route.query.repository, route.query.page, route.query.status, route.query.period], async (values, old) => {
  dates.value = values[0] && values[1] ? [String(values[0]), String(values[1])] : recentDates(30)
  filterRepo.value = Number(values[2]) || null; page.value = Math.max(1, Number(values[3]) || 1)
  filterStatus.value = String(values[4] || ''); preset.value = String(values[5] || (values[0] ? 'custom' : '30'))
  if (tab.value !== 'tasks') return
  if (values.slice(0,3).some((v,i) => v !== old[i])) { statistics.value = null; loadStatistics() }
  if (values.some((v,i) => v !== old[i])) { tasks.value = []; await loadTasks() }
})
function startManual() { manualRepo.value = availableRepos.value.find(r => r.id === filterRepo.value) || availableRepos.value[0]; manualVisible.value = true }
const repoVisible = ref(false), repoForm = ref({}), catalog = ref([]), catalogLoading = ref(false)
const hookVisible = ref(false), hookRepo = ref(null), managerURL = ref(window.location.origin)
const manualVisible = ref(false), manualRepo = ref(null), manualNumber = ref(1)
const connectionMessage = ref(''), connectionOk = ref(false), connectionDiagnostic = ref(null)
const repoError = ref(''), hookError = ref(''), manualError = ref(''), configError = ref('')
const detailError = ref(''), detailLoading = ref(false), rerunning = ref(false), cancellingId = ref('')
const confirmingIds = ref(new Set())
let taskGeneration = 0, detailGeneration = 0, repoGeneration = 0, listScrollTop = 0, detailController
watch(repoVisible, visible => { if (!visible) { repoGeneration++; catalogGeneration++; repoForm.value.bot_token = ''; repoError.value = '' } })
let hookGeneration = 0
watch(hookVisible, () => { hookGeneration++; hookError.value = '' })
watch(manualVisible, () => { manualError.value = '' })
let timer, catalogGeneration = 0, disposed = false, lastHealthPoll = 0, lastTasksPoll = 0
const workerStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'worker') || Date.now()/1000-health.value.find(h => h.name === 'worker').updated > 90))
const feedbackStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'feedback') || Date.now()/1000-health.value.find(h => h.name === 'feedback').updated > 90))
const progressStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'progress') || Date.now()/1000-health.value.find(h => h.name === 'progress').updated > 90))
const fmt = value => value ? new Date(value*1000).toLocaleString('zh-CN') : '尚无'
const repoName = id => { const r = repositories.value.find(r => r.id === id); return r ? `${r.server_name} / ${r.repo_name}${r.access_archived ? '（已归档）' : ''}` : `仓库 ${id ?? ''}` }
const repoArchived = id => Boolean(repositories.value.find(row => row.id === id)?.access_archived)
const statusLabel = s => ({ awaiting_cost: '等待成本确认', budget_paused: '预算暂停', incomplete: '审查未完成', cancelling: '取消中', cancelled: '已取消', queued: '等待执行', running: '分析中', publishing: '发布与核对', recovering: '核对发布结果', publish_unknown: '发布待核对', succeeded: '已完成', superseded: '版本已过期', failed: '执行失败' }[s] || s)

const effectiveModelTimeout = computed(() => Math.max(0, Math.min(config.value.model_request_timeout_seconds ?? 600, (config.value.review_timeout_seconds ?? 600) - 40)))
const showError = e => { if (e.code === 'ERR_CANCELED') return; error.value = errorMessage(e); if (detailVisible.value) detailError.value = error.value }
async function downloadProgress(serverId) { try { const response = await api.get(`/ai-review/servers/${serverId}/progress-assets`, { responseType: 'blob' }); const url = URL.createObjectURL(response.data); const a = document.createElement('a'); a.href = url; a.download = `gitea-review-progress-${serverId}.zip`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); ElMessage.success('组件已下载；解压后按安装说明接入已有 Gitea 容器') } catch (e) { showError(e) } }
let csrfPromise
async function write(method, path, data) {
  error.value = ''; detailError.value = ''
  if (!csrf.value) {
    if (!csrfPromise) csrfPromise = api.get('/ai-review/csrf').then(response => { csrf.value = response.data.token }).finally(() => { csrfPromise = null })
    await csrfPromise
  }
  return api.request({ method, url: '/ai-review'+path, data, headers: { 'X-Review-CSRF': csrf.value } })
}
async function loadTasks(silent = false) { lastTasksPoll = Date.now(); const current = ++taskGeneration; tasksController?.abort(); tasksController = new AbortController(); try { const r = await api.get('/ai-review/tasks', { params: { ...scopeParams(), page: page.value, status: filterStatus.value || undefined }, signal: tasksController.signal, notifyError: !silent }); if (!disposed && current === taskGeneration) { tasks.value = r.data.items; total.value = r.data.total; tasksError.value = '' } } catch (e) { if (!disposed && current === taskGeneration && e.code !== 'ERR_CANCELED') tasksError.value = errorMessage(e) } }
async function refresh() { if (refreshing.value) return; refreshing.value = true; try { const [c, r, s] = await Promise.all([api.get('/ai-review/config'), api.get('/ai-review/repositories'), api.get('/ai-review/servers')]); config.value = c.data?.config || config.value; health.value = Array.isArray(c.data?.health) ? c.data.health : []; repositories.value = Array.isArray(r.data) ? r.data : repositories.value; servers.value = Array.isArray(s.data) ? scopedServers(s.data) : servers.value; if (tab.value === 'tasks') await Promise.all([loadTasks(), loadStatistics()]) } catch (e) { showError(e) } finally { refreshing.value = false } }
async function saveConfig() { if (busy.value) return; configError.value = ''; busy.value = true; try { const { api_key_configured, ...payload } = config.value; payload.engine = 'opencode_native'; if (apiKey.value) payload.api_key = apiKey.value; config.value = (await write('put', '/config', payload)).data; apiKey.value = ''; ElMessage.success('设置已保存；资源预算与压缩阈值仅对后续新任务生效') } catch (e) { configError.value = errorMessage(e) } finally { busy.value = false } }
async function testTokens() { testing.value = true; connectionMessage.value = ''; try { const r = await write('post', '/connection-test', { token_samples: 4 }); connectionMessage.value = r.data.calibration.message + ' ' + r.data.calibration.samples.map((v, i) => `样本 ${i+1}：本地 ${v.local} / 服务 ${v.server ?? '未提供'}，差值 ${v.difference ?? '未知'}`).join('；'); connectionOk.value = r.data.calibration.samples.every(v => v.difference === 0) } catch(e) { connectionOk.value = false; connectionMessage.value = errorMessage(e) } finally { testing.value = false } }
async function testConnection() { testing.value = true; connectionMessage.value = ''; connectionDiagnostic.value = null; try { const r = await write('post', '/connection-test', {}); connectionMessage.value = r.data.message; connectionOk.value = r.data.ok } catch (e) { connectionOk.value = false; connectionMessage.value = errorMessage(e); connectionDiagnostic.value = e.response?.data?.diagnostic || null } finally { testing.value = false } }
function editRepo(row) { repoGeneration++; repoError.value = ''; repoForm.value = row ? { ...row, bot_token: '' } : { server_id: servers.value[0]?.id, repo_name: '', bot_token: '', enabled: false }; catalog.value = []; repoVisible.value = true; if (!row && repoForm.value.server_id) searchCatalog('') }
async function searchCatalog(q) { const generation = ++catalogGeneration; if (!repoForm.value.server_id) return; catalogLoading.value = true; try { const r = await api.get('/ai-review/catalog', { params: { server_id: repoForm.value.server_id, q } }); if (!disposed && generation === catalogGeneration) catalog.value = r.data.items } catch (e) { if (!disposed && generation === catalogGeneration) repoError.value = errorMessage(e) } finally { if (generation === catalogGeneration) catalogLoading.value = false } }
async function reloadRepositories() { try { repositories.value = (await api.get('/ai-review/repositories')).data } catch { error.value = '配置已保存，但仓库列表刷新失败；请刷新核对。' } }
async function saveRepo() {
  if (busy.value) return
  busy.value = true; repoError.value = ''; const current = repoGeneration
  try { const f = repoForm.value; await write(f.id ? 'put' : 'post', '/repositories'+(f.id ? '/'+f.id : ''), isPlatformAdmin.value ? { server_id: f.server_id, repo_name: f.repo_name, bot_token: f.bot_token, enabled: f.enabled, revision: f.revision } : { enabled: f.enabled, revision: f.revision });
    if (disposed || current !== repoGeneration) return
    repoVisible.value = false; repoForm.value.bot_token = ''; ElMessage.success('仓库配置已保存'); await reloadRepositories()
  } catch (e) { if (!disposed && current === repoGeneration) repoError.value = errorMessage(e) } finally { busy.value = false }
}
async function installHook() { if (busy.value) return; busy.value = true; hookError.value = ''; const request = hookGeneration; const target = hookRepo.value.id; try { await write('post', `/repositories/${target}/webhook`, { manager_url: managerURL.value }); if (request !== hookGeneration) return; hookVisible.value = false; ElMessage.success('事件通知已安装并验证'); await reloadRepositories() } catch (e) { if (request === hookGeneration) hookError.value = errorMessage(e) } finally { busy.value = false } }
async function rerun(repository, number) { if (rerunning.value) return; rerunning.value = true; manualError.value = ''; try { await write('post', `/repositories/${repository}/rerun`, { number }); manualVisible.value = false; page.value = 1; if (tab.value !== 'tasks') await router.push({ path: '/ai-reviews', query: listQuery() }); else await loadTasks(); ElMessage.success('审查任务已排队') } catch (e) { if (manualVisible.value) manualError.value = errorMessage(e); else showError(e) } finally { rerunning.value = false } }

async function confirmCost(row) {
  if (!row?.cost_summary?.plan_hash || confirmingIds.value.has(row.id)) return
  const id = row.id, planHash = row.cost_summary.plan_hash
  confirmingIds.value = new Set([...confirmingIds.value, id])
  try {
    const response = await write('post', `/tasks/${id}/confirm-plan`, { plan_hash: planHash })
    tasks.value = tasks.value.map(item => item.id === id ? { ...item, ...response.data, cost_summary: null } : item)
    ElMessage.success('成本计划确认请求已处理，任务状态已更新')
  } catch (e) {
    showError(e)
  } finally {
    await loadTasks(true)
    const next = new Set(confirmingIds.value); next.delete(id); confirmingIds.value = next
  }
}

async function cancelTask(row) {
  if (!row || cancellingId.value) return
  cancellingId.value = row.id
  try {
    await ElMessageBox.confirm(`取消 ${repoName(row.repository)} #${row.number} 的本次审查？当前版本不会自动重跑，已有日志和诊断将保留。`, '强制取消审查', { confirmButtonText: '强制取消', cancelButtonText: '继续审查', type: 'warning' })
    const response = await write('post', `/tasks/${row.id}/cancel`, {})
    taskGeneration++; detailGeneration++; detailController?.abort(); detailLoading.value = false
    if (detail.value?.id === row.id) detail.value = response.data.task
    tasks.value = tasks.value.map(item => item.id === row.id ? response.data.task : item)
    ElMessage.success(response.data.task.status === 'cancelled' ? '任务已取消' : '已接受取消，正在确认执行停止')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') {
      if (detailVisible.value) detailError.value = errorMessage(e)
      else showError(e)
      if (e.response?.data?.task) {
        if (detail.value?.id === row.id) detail.value = e.response.data.task
        tasks.value = tasks.value.map(item => item.id === row.id ? e.response.data.task : item)
      }
    }
  } finally { cancellingId.value = '' }
}

async function reconcile(row) { try { await write('post', `/tasks/${row.id}/reconcile`, {}); ElMessage.success('已请求核对，不会盲目重发评论') } catch (e) { showError(e) } }
async function closeDetail() {
  await router.push({ path: '/ai-reviews', query: listQuery() })
}
function openTask(id) {
  if (!detailVisible.value) { listScrollTop = document.querySelector('.app-main')?.scrollTop || 0; listChartsCollapsed = chartsCollapsed.value }
  lastSelected.value = String(id)
  router.push({ query: { ...route.query, ...listQuery(), task: String(id) } })
}
async function fetchDetail(id, polling = false) {
  const current = ++detailGeneration
  detailController?.abort(); detailController = new AbortController()
  if (!polling) { detail.value = null; detailVisible.value = true; detailError.value = ''; chartsCollapsed.value = true }
  detailLoading.value = true
  try { const r = await api.get('/ai-review/tasks/'+id, { params: { evidence: '0' }, signal: detailController.signal, notifyError: !polling }); if (!disposed && current === detailGeneration && selectedTask.value === id) detail.value = r.data }
  catch (e) { if (e.code !== 'ERR_CANCELED' && !disposed && current === detailGeneration) detailError.value = errorMessage(e) }
  finally { if (current === detailGeneration) detailLoading.value = false }
}
watch(selectedTask, async id => {
  if (id) { lastSelected.value = id; await fetchDetail(id); await nextTick(); workspace.value?.scrollIntoView({ block: 'start' }) }
  else { detailGeneration++; detailController?.abort(); detail.value = null; detailVisible.value = false; detailLoading.value = false; detailError.value = ''; chartsCollapsed.value = listChartsCollapsed; await nextTick(); const main = document.querySelector('.app-main'); if (main) main.scrollTop = listScrollTop }
})
async function poll() {
  if (disposed) return
  if (detailVisible.value && detail.value) {
    const active = ['queued', 'running', 'cancelling', 'publishing', 'recovering', 'publish_unknown'].includes(detail.value.status)
    if (active || (detail.value.status !== 'cancelled' && detail.value.log_sync?.complete === false)) await fetchDetail(detail.value.id, true)
  }
  if (tab.value === 'tasks' && Date.now() - lastTasksPoll >= 5000) await loadTasks(true)
  if (tab.value === 'tasks' && Date.now() - lastStatisticsPoll >= 30000) await loadStatistics(true)
  if (isPlatformAdmin.value && Date.now() - lastHealthPoll >= 5000) { lastHealthPoll = Date.now(); try { health.value = (await api.get('/ai-review/config', { notifyError: false })).data.health } catch {} }
  if (!disposed) timer = setTimeout(poll, detailVisible.value ? (document.hidden ? 5000 : 1000) : 5000)
}

async function confirmUnpublished(row) {
  try {
    await ElMessageBox.confirm('仅当你已确认 Gitea 不再处理原请求、没有正式 Review，并已处理机器人残留草稿时使用。此操作不删除 Gitea 评论；确认后可人工重新审查。', '确认原任务未发布', { confirmButtonText: '已核对，确认未发布', cancelButtonText: '取消', type: 'warning' })
    await write('post', `/tasks/${row.id}/reconcile`, { confirm_unpublished: true })
    await loadTasks()
    ElMessage.success('已记录未发布，可人工重新审查')
  } catch (e) { if (e !== 'cancel' && e !== 'close') showError(e) }
}
onMounted(async () => { await refresh(); if (!disposed && tab.value === 'tasks' && selectedTask.value) await fetchDetail(selectedTask.value); if (!disposed) timer = setTimeout(poll, 5000) })
onUnmounted(() => { disposed = true; statisticsGeneration++; statisticsController?.abort(); tasksController?.abort(); taskGeneration++; detailGeneration++; detailController?.abort(); repoGeneration++; catalogGeneration++; clearTimeout(timer); apiKey.value = ''; repoForm.value.bot_token = '' })
</script>

<style scoped>
.task-table-panel :deep(.last-selected td.el-table__cell) { background: #eaf2fe; }
.cost-summary { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; margin-top: 8px; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.review-page { min-width: 0; container-type: inline-size; container-name: review-page; }
.review-filters { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 14px 18px; margin: 0 0 18px; }.period-selector { width: 140px; }.review-filters :deep(.el-date-editor) { max-width: 100%; width: 280px; flex-grow: 0; }
.review-workspace { min-width: 0; scroll-margin-top: 12px; }.review-workspace.with-detail { display: grid; grid-template-columns: 320px minmax(0,1fr); gap: 18px; align-items: start; }.task-rail { padding: 16px; min-width: 0; position: sticky; top: 0; }.rail-heading { display: flex; justify-content: space-between; margin-bottom: 14px; }.task-rail>.el-select { width: 100%; margin-bottom: 12px; }.rail-rows { max-height: 62vh; overflow-y: auto; }.task-choice { width: 100%; display: flex; flex-direction: column; gap: 8px; padding: 14px 12px; text-align: left; background: transparent; border: 1px solid transparent; border-bottom-color: #e8edf3; border-radius: 10px; cursor: pointer; font: inherit; color: inherit; }.task-choice:hover { background: #f1f6fc; }.task-choice.selected { background: #eaf2fe; border-color: #a6c4ef; }.task-choice:focus-visible { outline: 2px solid #3976c4; outline-offset: -2px; }.task-choice-title { font-size: 12px; overflow-wrap: anywhere; color: #52637b; }.task-choice-meta { display: flex; gap: 8px; align-items: center; justify-content: space-between; flex-wrap: wrap; }.task-rail .el-pagination { margin-top: 14px; }.task-table-panel h4 { margin: 0; }.empty-guide { padding: 24px; margin-bottom: 16px; }
@container review-page (max-width: 960px) { .review-workspace.with-detail { grid-template-columns: minmax(0,1fr); }.with-detail .task-rail { display: none; } }

.review-toolbar, .review-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.review-toolbar { justify-content: space-between; margin-bottom: 18px; }
.panel { padding: 24px; }
.muted { color: var(--text-secondary, #64748b); font-size: 13px; line-height: 1.7; }
.review-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 24px; }
.selector { width: 280px; max-width: 100%; }
.full { width: 100%; }
.footer { margin-top: 22px; margin-bottom: 16px; }
.finding { margin-top: 20px; padding: 20px; border: 1px solid #dfe6ed; border-radius: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.finding pre { padding: 12px; overflow: auto; background: #f3f6fa; border-radius: 8px; white-space: pre-wrap; }
.wrap { overflow-wrap: anywhere; }
.permission-guide { padding: 14px; margin-bottom: 18px; border-radius: 10px; background: #f4f7fb; font-size: 13px; line-height: 1.75; }
.permission-guide dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 8px; }
.permission-guide dd { margin: 0; overflow-wrap: anywhere; }
.failure { color: #b45309; font-size: 12px; }
.el-pagination { margin-top: 20px; flex-wrap: wrap; }
.el-alert { margin-bottom: 16px; }
@media (max-width: 720px) { .review-grid { grid-template-columns: minmax(0, 1fr); } .panel { padding: 14px; } }
</style>
