<script lang="ts">
// 嵌套确认框共用滚动锁，不能由先关闭的父弹窗提前解除。
let modalLocks = 0;
let originalOverflow = "";
</script>
<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId, watch } from "vue";
import { X } from "lucide-vue-next";
import { useI18n } from "@/i18n";

const props = withDefaults(defineProps<{ open: boolean; title: string; busy?: boolean; wide?: boolean; drawer?: boolean; level?: number }>(), { busy: false, level: 0 });
const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();
const titleId = useId();
const panel = ref<HTMLElement | null>(null);
let previous: HTMLElement | null = null;
let listening = false;
function close() { if (!props.busy) emit("close"); }
function focusable() {
  return Array.from(panel.value?.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex="0"]',
  ) || []).filter(el => !el.closest('[hidden], [inert], [aria-hidden="true"]')
    && getComputedStyle(el).display !== "none" && getComputedStyle(el).visibility !== "hidden");
}
function overlayZIndex(el: Element | null): number {
  const z = el ? Number(getComputedStyle(el).zIndex) : NaN;
  return Number.isFinite(z) ? z : 0;
}
function isTop() {
  // 最上层按绘制顺序判定：层级高的在上，同层级后挂载的在上。
  // 只按 DOM 顺序会在页面弹窗后挂载时误判，确认框会被盖住、焦点被抢走。
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
  const mine = panel.value;
  if (!mine) return false;
  const myZ = overlayZIndex(mine.parentElement);
  const myIndex = dialogs.indexOf(mine);
  return !dialogs.some((el, index) => {
    if (el === mine) return false;
    const z = overlayZIndex(el.parentElement);
    return z > myZ || (z === myZ && index > myIndex);
  });
}
function onFocus(event: FocusEvent) {
  if (isTop() && !panel.value?.contains(event.target as Node)) (focusable()[0] || panel.value)?.focus();
}
function onKey(event: KeyboardEvent) {
  // 嵌套确认框只处理最上层的键盘事件。
  if (!isTop()) return;
  if (event.key === "Escape") { event.preventDefault(); close(); }
  if (event.key !== "Tab") return;
  const items = focusable();
  const first = items[0] || panel.value;
  const last = items[items.length - 1] || panel.value;
  if (event.shiftKey && (document.activeElement === first || !panel.value?.contains(document.activeElement))) {
    event.preventDefault(); last?.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !panel.value?.contains(document.activeElement))) {
    event.preventDefault(); first?.focus();
  }
}
function cleanup() {
  if (!listening) return;
  document.removeEventListener("keydown", onKey);
  document.removeEventListener("focusin", onFocus);
  if (--modalLocks === 0) document.body.style.overflow = originalOverflow;
  const target = previous;
  void nextTick(() => { if (target?.isConnected) target.focus(); });
  listening = false;
}
watch(() => props.open, async open => {
  if (!open) { cleanup(); return; }
  previous = document.activeElement as HTMLElement | null;
  if (modalLocks++ === 0) originalOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  document.addEventListener("keydown", onKey);
  document.addEventListener("focusin", onFocus);
  listening = true;
  await nextTick();
  if (props.open && isTop()) (focusable()[0] || panel.value)?.focus();
}, { immediate: true });
onBeforeUnmount(cleanup);
</script>

<template>
  <Teleport to="body">
    <!-- 详情抽屉沿用相同焦点与关闭规则，避免另一套弹层实现。 -->
    <!-- 遮罩使用独立的 scrim 色：亮色主题下也是深色半透明，弹层始终从页面中浮出来。 -->
    <div v-if="open" class="fixed inset-0 flex bg-scrim/70 p-3 backdrop-blur-sm sm:p-6" :style="{ zIndex: 50 + level }" :class="drawer ? 'items-start justify-end' : 'items-center justify-center'" @click.self="close">
      <section ref="panel" role="dialog" aria-modal="true" :aria-labelledby="titleId" :aria-busy="busy"
        tabindex="-1" class="max-h-[90vh] w-full overflow-y-auto rounded-2xl border border-border/70 bg-surface p-4 shadow-pop sm:p-6"
        :class="drawer ? 'h-[90vh] max-w-2xl' : wide ? 'max-w-6xl' : 'max-w-xl'">
        <header class="mb-4 flex items-center justify-between gap-3">
          <h2 :id="titleId" class="text-lg font-bold tracking-tight">{{ title }}</h2>
          <button type="button" class="icon-button shrink-0" :aria-label="t('common.close')" :title="t('common.close')" :disabled="busy" @click="close"><X :size="20" /></button>
        </header>
        <slot />
      </section>
    </div>
  </Teleport>
</template>
