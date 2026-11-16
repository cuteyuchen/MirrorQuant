<script setup lang="ts">
import { computed, ref } from "vue";
import { PieChart } from "lucide-vue-next";
import type { LeadSnapshot } from "@/types/api";
import { useI18n } from "@/i18n";
import { finiteNumber, formatDateTime, formatPct } from "@/utils/format";
import EmptyState from "@/components/EmptyState.vue";

const props = defineProps<{ allocation?: LeadSnapshot["allocation"] }>();
const { t } = useI18n();
// 分类色只有 5 个：排不进前 5 的资产并入“其他”并用中性色，绝不循环复用颜色。
const SERIES = 5;
const OTHER = "__other__";
type Slice = { key: string; label: string; pct: number | null; color: string };
// 资产 → 色位的对应关系在组件存活期间保持不变：名次变了颜色也跟着资产走，不跟着名次走。
const slots = new Map<string, number>();
function claimSlots(names: string[]) {
  for (const name of [...slots.keys()]) if (!names.includes(name)) slots.delete(name);
  const taken = new Set(slots.values());
  const free = Array.from({ length: SERIES }, (_, index) => index + 1).filter(slot => !taken.has(slot));
  for (const name of names) if (!slots.has(name)) slots.set(name, free.shift()!);
}
const slices = computed<Slice[]>(() => {
  const items = (props.allocation?.items ?? []).map((item, index) => ({
    key: item.asset?.trim() || `#${index}`, label: item.asset?.trim() || t("common.unknown"), volume: finiteNumber(item.volume),
  }));
  if (!items.length) return [];
  // 任一成交量未知或为负就算不出占比：只列出资产，占比显示破折号，也不画占比条。
  const total = items.some(item => item.volume == null || item.volume < 0) ? null : items.reduce((sum, item) => sum + item.volume!, 0);
  const known = total != null && total > 0;
  const ranked = known ? [...items].sort((a, b) => b.volume! - a.volume!) : items;
  const head = ranked.slice(0, SERIES);
  const tail = ranked.slice(SERIES);
  claimSlots(head.map(item => item.key));
  const rows: Slice[] = head.map(item => ({
    key: item.key, label: item.label, pct: known ? item.volume! / total * 100 : null, color: `rgb(var(--c-chart-${slots.get(item.key)}))`,
  }));
  if (tail.length) rows.push({
    key: OTHER, label: t("alloc.otherCount", { n: tail.length }), color: "rgb(var(--c-faint))",
    pct: known ? tail.reduce((sum, item) => sum + item.volume!, 0) / total * 100 : null,
  });
  return rows;
});
// 占比为零的资产只留在图例里，不在条上占一条细缝。
const segments = computed(() => slices.value.filter(slice => slice.pct != null && slice.pct > 0));
// 悬停条上的一段或图例里的一行时，两边联动高亮同一项。
const active = ref<string | null>(null);
</script>
<template>
  <section class="card flex flex-col">
    <header class="card-header">
      <h3 class="section-title flex min-w-0 items-center gap-2.5"><span class="section-icon" aria-hidden="true"><PieChart :size="18" /></span>{{ t("alloc.title") }}</h3>
      <span v-if="allocation?.updateTime != null" class="font-mono text-xs tabular-nums text-muted">{{ t("alloc.updated", { time: formatDateTime(allocation.updateTime, { short: true, withSeconds: false }) }) }}</span>
    </header>
    <div class="card-body">
      <EmptyState v-if="!slices.length" :title="t('alloc.noData')" />
      <template v-else>
        <!-- 堆叠占比条只是概览，段间留 2px 间隙；下面的图例逐项写出数值，是它的文本等价视图。 -->
        <div v-if="segments.length" class="mb-3 flex h-3 w-full gap-0.5" data-testid="alloc-bar" aria-hidden="true">
          <span v-for="slice in segments" :key="slice.key" class="min-w-1 rounded-sm transition-opacity first:rounded-l-full last:rounded-r-full"
            :class="active != null && active !== slice.key ? 'opacity-30' : ''" :style="{ flex: `${slice.pct} 1 0%`, backgroundColor: slice.color }"
            :title="t('alloc.shareLabel', { asset: slice.label, pct: formatPct(slice.pct, 1, false) })"
            @mouseenter="active = slice.key" @mouseleave="active = null" />
        </div>
        <ul class="-mx-2 space-y-0.5" data-testid="alloc-legend">
          <li v-for="slice in slices" :key="slice.key" class="flex min-w-0 items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm transition-colors"
            :class="active === slice.key ? 'bg-surface2' : ''" @mouseenter="active = slice.key" @mouseleave="active = null">
            <span v-if="segments.length" class="h-2.5 w-2.5 shrink-0 rounded-sm" :style="{ backgroundColor: slice.color }" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate font-semibold">{{ slice.label }}</span>
            <span class="shrink-0 font-mono font-semibold tabular-nums text-muted">{{ formatPct(slice.pct, 1, false) }}</span>
          </li>
        </ul>
      </template>
    </div>
  </section>
</template>
