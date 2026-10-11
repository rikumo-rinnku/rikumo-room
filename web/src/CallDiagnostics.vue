<script setup lang="ts">
import type { DiagnosticSnapshot } from './diagnostics'
import type { ParticipantInfo } from './api'
defineProps<{ snapshot: DiagnosticSnapshot | null; eventCount: number;
  isManager: boolean; recording: boolean; recordingOwned: boolean; recordingBusy: boolean; recordingSaving: boolean;
  recordingSeconds: number; recordingBytes: number; recordingTracks: number; recordingNotice: string;
  recordingMembers: ParticipantInfo[]; recordingMember: string; recordingKind: string }>()
defineEmits<{ close: []; mark: []; download: []; recordingStart: []; recordingStop: []; recordingSave: []; recordingMark: [];
  'update:recordingMember': [string]; 'update:recordingKind': [string] }>()
const display = (value: number | null, unit: string) => value === null ? '—' : `${value.toFixed(1)}${unit}`
</script>
<template>
  <section class="diagnostic-panel" aria-label="通话诊断">
    <header><strong>通话诊断</strong><button class="text-button" @click="$emit('close')">关闭</button></header>
    <p>每 5 秒采集一次。网络往返时间不等于实际语音延迟。</p>
    <div v-if="snapshot">连接：{{ snapshot.state }} · 音频：{{ snapshot.context }} · {{ new Date(snapshot.time).toLocaleTimeString() }}</div>
    <div class="diagnostic-table"><table><thead><tr><th>成员</th><th>方向</th><th>往返</th><th>抖动</th><th>丢包</th><th>码率</th><th>丢包补偿</th></tr></thead>
      <tbody><tr v-for="(row, index) in snapshot?.rows ?? []" :key="index"><td>{{ row.member }}</td><td>{{ row.direction }}</td><td>{{ display(row.rttMs, ' ms') }}</td><td>{{ display(row.jitterMs, ' ms') }}</td><td>{{ display(row.lossPercent, '%') }}</td><td>{{ display(row.bitrateKbps, ' kbps') }}</td><td>{{ display(row.concealPercent, '%') }}</td></tr></tbody>
    </table></div>
    <p>“—”表示暂无数据；接收丢包按本次区间计算，发送丢包为远端报告。丢包补偿表示浏览器用替代声音补齐的采样比例。</p>
    <p v-if="snapshot?.microphone">麦克风：{{ snapshot.microphone.device }} · 采样率：{{ snapshot.microphone.sampleRate ?? '—' }} · 原生降噪：{{ snapshot.microphone.noiseSuppression ?? '未知' }}</p>
    <div class="diagnostic-actions"><button class="text-button" @click="$emit('mark')">标记出现电音</button><button class="text-button" @click="$emit('download')">导出诊断</button><span>已记录 {{ eventCount }} 个事件</span></div>
    <p>仅保留当前页面最近约 30 分钟和 200 个事件；导出含设备名称及成员名称，不含录音、密码、令牌或网络地址。</p>
    <section v-if="isManager" class="problem-recording-controls" aria-label="语音问题记录">
      <h3>问题记录</h3>
      <p>开启后在本机缓存各成员最近三分钟语音，房间内会显示提醒。停止或离开房间后清空缓存。</p>
      <button v-if="!recording" class="secondary-button" :disabled="recordingBusy || recordingSaving" @click="$emit('recordingStart')">开启三分钟缓存</button>
      <template v-else-if="recordingOwned">
        <p>已缓存 {{ recordingSeconds }} 秒 · {{ recordingTracks }} 条音轨 · {{ (recordingBytes / 1024 / 1024).toFixed(1) }} MB</p>
        <div class="problem-recording-fields">
          <select :value="recordingMember" aria-label="异常成员" @change="$emit('update:recordingMember', ($event.target as HTMLSelectElement).value)"><option value="">选择异常成员</option><option v-for="member in recordingMembers" :key="member.identity" :value="member.identity">{{ member.name || member.identity }}</option></select>
          <select :value="recordingKind" aria-label="异常类型" @change="$emit('update:recordingKind', ($event.target as HTMLSelectElement).value)"><option>电音</option><option>声音加速</option><option>断续</option><option>无声</option><option>其他</option></select>
          <button class="text-button" @click="$emit('recordingMark')">标记异常</button>
        </div>
        <div class="diagnostic-actions"><button class="secondary-button" :disabled="recordingBusy || recordingSaving || !recordingTracks" @click="$emit('recordingSave')">{{ recordingSaving ? '正在保存…' : '保存最近三分钟' }}</button><button class="text-button" :disabled="recordingBusy || recordingSaving" @click="$emit('recordingStop')">停止并清空</button></div>
      </template>
      <p v-else>另一位管理员正在缓存，录音由开启者保存。</p>
      <p v-if="recordingNotice" role="status">{{ recordingNotice }}</p>
    </section>
  </section>
</template>
<style scoped>
.problem-recording-controls{border-top:1px solid var(--line);margin-top:16px;padding-top:16px}.problem-recording-controls h3{font-size:13px;margin:0 0 10px}.problem-recording-fields{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}.problem-recording-fields select{padding:8px;border:1px solid var(--line);border-radius:8px;background:#151720;color:var(--soft);font:inherit}
.diagnostic-panel{position:fixed;bottom:105px;left:calc(254px + 24px);width:min(760px,calc(100vw - 300px));max-height:65vh;overflow:auto;z-index:30;padding:20px;border:1px solid var(--line);border-radius:12px;background:#1b1e27;color:var(--soft);font-size:12px;box-shadow:0 12px 40px #0006}.diagnostic-panel header,.diagnostic-actions{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.diagnostic-panel header{justify-content:space-between}.diagnostic-panel p{color:var(--muted);line-height:1.6}.diagnostic-table{overflow-x:auto}.diagnostic-panel table{width:100%;white-space:nowrap}.diagnostic-panel th,.diagnostic-panel td{text-align:left;padding:8px}.diagnostic-actions span{color:var(--muted)}@media(max-width:760px){.diagnostic-panel{left:12px;bottom:150px;width:calc(100vw - 24px);box-sizing:border-box}}
</style>
