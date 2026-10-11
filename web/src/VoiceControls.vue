<script setup lang="ts">
import { ref } from 'vue'
import AudioOptionHint from './AudioOptionHint.vue'
import { Mic, MicOff, Headphones, VolumeX, DoorOpen, Settings2 } from 'lucide-vue-next'
defineProps<{
  echoCancellationEnabled: boolean; autoGainEnabled: boolean; inputGainEnabled: boolean;
  rawMicrophone: boolean; micMuted: boolean; micBusy: boolean; micVolume: number; listenMuted: boolean; listenVolume: number;
  micDevices: MediaDeviceInfo[]; speakerDevices: MediaDeviceInfo[]; micDevice: string; speakerDevice: string;
  status: string; devicesBusy: boolean; speakerSupported: boolean
  thresholdEnabled: boolean; thresholdValue: number; thresholdBusy: boolean; thresholdLevel: number | null; thresholdOpen: boolean
  nativeDenoiseEnabled: boolean; nativeDenoiseStatus: string; rnnoiseEnabled: boolean; denoiseBusy: boolean
}>()
const emit = defineEmits<{
  echoCancellationEnabled: [boolean]; autoGainEnabled: [boolean]; inputGainEnabled: [boolean];
  rawMicrophone: [boolean]; toggleMic: []; toggleListen: []; diagnostics: []; micVolume: [number]; listenVolume: [number];
  micDevice: [string]; speakerDevice: [string]; leave: []
  thresholdEnabled: [boolean]; thresholdValue: [number]; settingsVisibility: [boolean]
  nativeDenoiseEnabled: [boolean]; rnnoiseEnabled: [boolean]
}>()
const valueOf = (event: Event) => (event.target as HTMLInputElement).value
const settingsOpen = ref(false)
function toggleSettings() {
  settingsOpen.value = !settingsOpen.value
  emit('settingsVisibility', settingsOpen.value)
}
</script>

<template>
  <div class="voice-dock" aria-label="通话音频控制">
    <section v-if="settingsOpen" class="audio-settings-panel" aria-label="音频设置">
      <header><strong>音频设置</strong><button class="text-button" @click="toggleSettings">关闭</button></header>
      <AudioOptionHint text="直接发送麦克风原始声音，暂停输入音量、降噪、阈值、回声消除和自动增益。建议使用耳机。" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="rawMicrophone" :disabled="micBusy || devicesBusy || denoiseBusy || thresholdBusy" @change="emit('rawMicrophone', ($event.target as HTMLInputElement).checked)" />原始麦克风直通</label></AudioOptionHint>
      <fieldset class="audio-processing-settings" :disabled="rawMicrophone">
      <AudioOptionHint text="注意：开启回声消除可能产生电音。如果声音失真，请关闭此项。此功能用于减少扬声器声音被麦克风再次收录。" :warning="true" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="echoCancellationEnabled" :disabled="micBusy || devicesBusy" @change="emit('echoCancellationEnabled', ($event.target as HTMLInputElement).checked)" />回声消除<span class="audio-option-warning" aria-label="开启后可能产生电音">⚠</span></label></AudioOptionHint>
      <AudioOptionHint text="自动调整麦克风收音音量，使说话声音大小更均匀。" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="autoGainEnabled" :disabled="micBusy || devicesBusy" @change="emit('autoGainEnabled', ($event.target as HTMLInputElement).checked)" />自动增益</label></AudioOptionHint>
      <AudioOptionHint text="开启后可通过麦克风音量滑块调整发送给其他人的音量；关闭后使用原音量。" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="inputGainEnabled" :disabled="micBusy || devicesBusy" @change="emit('inputGainEnabled', ($event.target as HTMLInputElement).checked)" />麦克风输入音量处理</label></AudioOptionHint>
      <hr class="audio-settings-divider" />
      <AudioOptionHint text="使用浏览器自带降噪减少风扇声和底噪；与 RNNoise 叠加可能影响人声。" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="nativeDenoiseEnabled" :disabled="denoiseBusy || thresholdBusy || micBusy" @change="emit('nativeDenoiseEnabled', ($event.target as HTMLInputElement).checked)" />原生降噪</label></AudioOptionHint>
      <p v-if="nativeDenoiseStatus">{{ nativeDenoiseStatus }}</p>
      <AudioOptionHint text="使用 RNNoise 减少背景噪声；与原生降噪叠加可能影响人声。" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="rnnoiseEnabled" :disabled="denoiseBusy || thresholdBusy || micBusy" @change="emit('rnnoiseEnabled', ($event.target as HTMLInputElement).checked)" />RNNoise 降噪</label></AudioOptionHint>
      <p v-if="denoiseBusy">正在应用降噪设置…</p>
      <hr class="audio-settings-divider" />
      <AudioOptionHint text="低于阈值的声音不发送。阈值越高，越需要大声说话；调至正常说话能收音即可。" v-slot="{ descriptionId }"><label class="threshold-toggle" :aria-describedby="descriptionId"><input type="checkbox" :checked="thresholdEnabled" :disabled="thresholdBusy || micBusy" @change="emit('thresholdEnabled', ($event.target as HTMLInputElement).checked)" />收音阈值</label></AudioOptionHint>
      <AudioOptionHint text="阈值越高，越需要大声说话。调至正常说话能收音即可。" v-slot="{ descriptionId }"><label class="threshold-slider" :aria-describedby="descriptionId">阈值 <output>{{ thresholdValue }} dBFS</output><input type="range" min="-70" max="-10" step="1" :value="thresholdValue" :disabled="!thresholdEnabled || thresholdBusy" aria-label="收音阈值" @input="emit('thresholdValue', Number(valueOf($event)))" /></label></AudioOptionHint>
      <div v-if="thresholdEnabled" class="threshold-meter" role="meter" aria-label="麦克风收音电平" aria-valuemin="-90" aria-valuemax="0" :aria-valuenow="thresholdLevel ?? undefined"><span :style="{ width: `${Math.max(0, Math.min(100, ((thresholdLevel ?? -90) + 90) / 90 * 100))}%` }"></span><i :style="{ left: `${(thresholdValue + 90) / 90 * 100}%` }"></i></div>
      <p>{{ thresholdBusy ? '正在应用…' : !thresholdEnabled ? '阈值已关闭' : thresholdLevel === null ? '开麦后显示收音电平' : `${thresholdOpen ? '正在收音' : '低于阈值'} · ${thresholdLevel.toFixed(1)} dBFS` }}</p>
      </fieldset>
    </section>
    <div class="audio-control-group">
      <button class="audio-toggle" :class="{ off: micMuted }" :disabled="micBusy" :aria-pressed="!micMuted" :aria-label="micMuted ? '开启麦克风' : '关闭麦克风'" :title="micMuted ? '开启麦克风' : '关闭麦克风'" @click="emit('toggleMic')"><MicOff v-if="micMuted" :size="20" /><Mic v-else :size="20" /></button>
      <div class="audio-control-fields">
        <label class="audio-volume-label">麦克风 <output>{{ rawMicrophone || !inputGainEnabled ? '原音量' : `${micVolume}%` }}</output><input type="range" min="0" max="200" step="1" :value="micVolume" :disabled="rawMicrophone || !inputGainEnabled" aria-label="麦克风输入音量" @input="emit('micVolume', Number(valueOf($event)))" /></label>
        <select :value="micDevice" :disabled="devicesBusy" aria-label="麦克风设备" @change="emit('micDevice', valueOf($event))"><option value="">默认麦克风</option><option v-for="(device, index) in micDevices" :key="device.deviceId" :value="device.deviceId">{{ device.label || `麦克风 ${index + 1}` }}</option></select>
      </div>
    </div>
    <div class="audio-control-group">
      <button class="audio-toggle" :class="{ off: listenMuted }" :aria-pressed="!listenMuted" :aria-label="listenMuted ? '开启收听' : '关闭收听'" :title="listenMuted ? '开启收听' : '关闭收听'" @click="emit('toggleListen')"><VolumeX v-if="listenMuted" :size="20" /><Headphones v-else :size="20" /></button>
      <div class="audio-control-fields">
        <label class="audio-volume-label">收听 <output>{{ listenVolume }}%</output><input type="range" min="0" max="200" step="1" :value="listenVolume" aria-label="总收听音量" @input="emit('listenVolume', Number(valueOf($event)))" /></label>
        <select :value="speakerDevice" :disabled="devicesBusy || !speakerSupported" :title="speakerSupported ? '选择输出设备' : '此浏览器使用系统默认输出'" aria-label="扬声器设备" @change="emit('speakerDevice', valueOf($event))"><option value="">{{ speakerSupported ? '默认扬声器' : '系统默认输出（浏览器不支持切换）' }}</option><option v-for="(device, index) in speakerDevices" :key="device.deviceId" :value="device.deviceId">{{ device.label || `扬声器 ${index + 1}` }}</option></select>
      </div>
    </div>
    <button class="text-button audio-settings-entry" :aria-expanded="settingsOpen" @click="toggleSettings"><Settings2 :size="16" />音频设置</button>
    <div class="voice-dock-status">{{ status }}</div>
    <button class="text-button" @click="emit('diagnostics')">通话诊断</button>
    <button class="audio-toggle leave" title="结束通话" aria-label="结束通话" @click="emit('leave')"><DoorOpen :size="20" /></button>
  </div>
</template>

<style>
.audio-option-warning{color:#ffc078;font-size:13px}.audio-settings-panel .threshold-toggle{cursor:help}
.audio-processing-settings{border:0;padding:0;margin:0;min-width:0}.audio-processing-settings:disabled{opacity:.45}
.audio-settings-panel{position:absolute;bottom:calc(100% + 8px);left:24px;width:330px;max-width:calc(100vw - 48px);max-height:65dvh;overflow:auto;padding:18px;box-sizing:border-box;background:#1b1e27;border:1px solid var(--line);border-radius:12px;font-size:12px;box-shadow:0 12px 40px #0006}.audio-settings-panel header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.audio-settings-panel p,.audio-settings-panel small{color:var(--muted);line-height:1.6}.audio-settings-entry{display:flex;align-items:center;gap:6px;white-space:nowrap}
.threshold-toggle{display:flex;align-items:center;gap:8px;margin-bottom:12px}.threshold-toggle input,.threshold-slider input{accent-color:var(--accent)}.threshold-slider{display:grid;grid-template-columns:1fr auto;gap:8px}.threshold-slider input{grid-column:1/-1;width:100%;margin:0}.threshold-meter{height:8px;border-radius:4px;background:#303440;position:relative;margin-top:12px;overflow:hidden}.threshold-meter span{display:block;height:100%;background:var(--accent)}.threshold-meter i{position:absolute;top:0;bottom:0;width:2px;background:#ffc078}.audio-settings-divider{border:0;border-top:1px solid var(--line);margin:16px 0}
.voice-dock{position:relative;z-index:8;display:flex;align-items:center;gap:22px;padding:15px 24px;border-top:1px solid var(--line);background:#151720;flex-shrink:0;margin-top:auto}
.audio-control-group{display:flex;align-items:center;gap:12px;min-width:0;flex:1;max-width:290px}
.audio-toggle{display:grid;place-items:center;flex-shrink:0;width:40px;height:40px;border:1px solid var(--line);border-radius:9px;background:#222630;color:var(--accent);cursor:pointer}
.audio-toggle.off,.audio-toggle.leave{color:#ff9ba5;background:#35232a}.audio-toggle:disabled{opacity:.45;cursor:wait}
.audio-control-fields{min-width:0;flex:1}.audio-volume-label{display:grid;grid-template-columns:1fr auto;gap:4px;font-size:11px;color:var(--soft)}.audio-volume-label input{grid-column:1/-1;width:100%;margin:0;accent-color:var(--accent);cursor:pointer}
.audio-control-fields select{width:100%;max-width:240px;background:transparent;color:var(--muted);border:0;font-size:10px;margin-top:5px}.audio-control-fields option{background:#151720;color:#ddd}.voice-dock-status{margin-left:auto;color:var(--muted);font-size:10px;white-space:nowrap}
.member-volume{grid-column:1/-1;display:flex;align-items:center;gap:10px;padding-top:9px;border-top:1px solid var(--line);font-size:11px;color:var(--muted);width:100%}.member-volume input{min-width:40px;flex:1;accent-color:var(--accent)}.member-volume output{width:38px;text-align:right}
.participant-card:has(.member-volume){flex-wrap:wrap;height:auto;min-height:58px;padding-top:12px;padding-bottom:12px}.participant-audio-state{color:var(--muted);flex-shrink:0}
.main-panel:has(.voice-dock)>.call-view{flex:1;min-height:0;overflow:auto}.main-panel:has(.voice-dock)>.settings-view{height:auto;flex:1;min-height:0}.main-panel:has(.voice-dock){height:100dvh;min-height:0;overflow:hidden}.main-panel:has(.voice-dock)>.topbar{flex-shrink:0}
@media(max-width:760px){.voice-dock{gap:10px;padding:10px 12px;flex-wrap:wrap}.audio-control-group{max-width:none;flex-basis:40%}.voice-dock-status{margin-left:0;flex:1}.audio-toggle{width:36px;height:36px}}
</style>
