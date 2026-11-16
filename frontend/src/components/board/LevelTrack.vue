<script setup lang="ts">
import { computed } from "vue";
import { finiteNumber, type NumericValue } from "@/utils/format";

// 止损 → 止盈位置条：左端是止损价，右端是止盈价，圆点是现价，竖线是均价。
// 多头与空头共用同一条公式（空头的止损价高于止盈价，比例自然反向）；
// 止盈、止损或现价任一未知时不画，避免用猜测的位置误导判断。
const props = defineProps<{ mark?: NumericValue; entry?: NumericValue; takeProfit?: NumericValue; stopLoss?: NumericValue }>();

function ratio(value: NumericValue): number | null {
  const price = finiteNumber(value);
  const stop = finiteNumber(props.stopLoss);
  const target = finiteNumber(props.takeProfit);
  if (price == null || stop == null || target == null || stop === target) return null;
  return Math.min(1, Math.max(0, (price - stop) / (target - stop)));
}
const markRatio = computed(() => ratio(props.mark));
const entryRatio = computed(() => ratio(props.entry));
</script>

<template>
  <div v-if="markRatio != null" class="level-track mx-1.5" aria-hidden="true">
    <!-- 均价到现价之间的色段：朝止盈方向走是盈利色，朝止损方向走是亏损色。 -->
    <span
      v-if="entryRatio != null"
      class="absolute inset-y-0 rounded-full"
      :class="markRatio >= entryRatio ? 'bg-success/80' : 'bg-danger/80'"
      :style="{ left: `${Math.min(markRatio, entryRatio) * 100}%`, width: `${Math.abs(markRatio - entryRatio) * 100}%` }"
    />
    <span v-if="entryRatio != null" class="absolute top-1/2 h-3 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded bg-muted" :style="{ left: `${entryRatio * 100}%` }" />
    <span class="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface bg-foreground shadow" :style="{ left: `${markRatio * 100}%` }" />
  </div>
</template>
