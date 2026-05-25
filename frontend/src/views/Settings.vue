<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">系统设置</h3>
    </div>

    <div class="glass-card" style="padding:28px;max-width:600px;">
      <div class="card-section-title" style="margin-bottom:20px;">本机 IP 配置</div>
      <el-alert type="warning" :closable="false" show-icon
        title="添加服务器前必须先配置本机 IP，用于区分本地和远程服务器。" style="margin-bottom:20px" />
      <el-form label-width="100px">
        <el-form-item label="本机 IP">
          <el-input v-model="hostIp" placeholder="例: 10.10.5.21" style="width:300px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="save" :loading="saving">保存</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

export default {
  setup() {
    const hostIp = ref('')
    const saving = ref(false)

    function load() {
      api.get('/settings').then(res => {
        hostIp.value = res.data.host_ip || ''
      })
    }

    function save() {
      saving.value = true
      api.post('/settings', { host_ip: hostIp.value }).then(() => {
        ElMessage.success('保存成功')
      }).finally(() => { saving.value = false })
    }

    onMounted(load)
    return { hostIp, saving, save }
  },
}
</script>

<style scoped>
.section-header { margin-bottom: 18px; }
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.card-section-title { font-weight: 600; font-size: 15px; color: #1a1a2e; }
</style>
