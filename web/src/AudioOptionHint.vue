<script setup lang="ts">
import { getCurrentInstance, nextTick, onBeforeUnmount, ref } from 'vue'

defineProps<{ text: string; warning?: boolean }>()
const id = `audio-option-hint-${getCurrentInstance()!.uid}`
const anchor = ref<HTMLElement>()
const bubble = ref<HTMLElement>()
const visible = ref(false)
const position = ref({ left: '0px', top: '0px', width: '300px' })

async function show() {
  const element = anchor.value
  if (!element) return
  const rect = element.getBoundingClientRect()
  const width = Math.min(300, window.innerWidth - 24)
  const right = rect.right + 12
  const left = right + width <= window.innerWidth - 12 ? right
    : rect.left - width - 12 >= 12 ? rect.left - width - 12
    : Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))
  position.value = { left: `${left}px`, top: `${rect.top}px`, width: `${width}px` }
  visible.value = true
  await nextTick()
  if (!visible.value || !bubble.value) return
  const height = bubble.value.getBoundingClientRect().height
  const overlaps = left < rect.right && left + width > rect.left
  const desiredTop = overlaps ? rect.top - height - 10 : rect.top
  const top = Math.max(12, Math.min(desiredTop, window.innerHeight - height - 12))
  position.value.top = `${top}px`
}
function hide() { visible.value = false }
// Hide on viewport movement so a hint cannot remain detached from its option.
window.addEventListener('scroll', hide, true)
window.addEventListener('resize', hide)
onBeforeUnmount(() => {
  window.removeEventListener('scroll', hide, true)
  window.removeEventListener('resize', hide)
})
</script>

<template>
  <div ref="anchor" class="audio-option-hint-anchor" @mouseenter="show" @mouseleave="hide" @focusin="show" @focusout="hide" @keydown.esc="hide">
    <slot :description-id="id" />
    <Teleport to="body">
      <div v-if="visible" :id="id" ref="bubble" role="tooltip" class="audio-option-hint" :class="{ 'audio-option-hint-warning': warning }" :style="position">{{ text }}</div>
    </Teleport>
  </div>
</template>

<style>
.audio-option-hint-anchor{min-width:0}
.audio-option-hint{position:fixed;z-index:1000;box-sizing:border-box;padding:11px 13px;border:1px solid var(--line);border-radius:10px;background:#1b1e27;color:var(--soft);font-family:inherit;font-size:12px;line-height:1.7;box-shadow:0 8px 24px #0005;pointer-events:none;overflow-wrap:anywhere;max-height:calc(100dvh - 24px);overflow:hidden}
.audio-option-hint-warning{border-color:#80643c;color:#ffc078}
</style>
