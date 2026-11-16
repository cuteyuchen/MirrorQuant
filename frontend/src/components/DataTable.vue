<script setup lang="ts" generic="T">
import EmptyState from "./EmptyState.vue";
import ErrorState from "./ErrorState.vue";
import LoadingSkeleton from "./LoadingSkeleton.vue";
import type { TableColumn } from "@/types/ui";
// 同一份单元格插槽同时服务桌面表格与手机卡片，不引入横向滚动容器。
withDefaults(defineProps<{
  rows: T[]; columns: TableColumn[]; rowKey: (row: T) => string | number;
  rowTone?: (row: T) => string; loading?: boolean; error?: string;
  empty?: string; label: string; selectable?: boolean; expanded?: string | number | null;
}>(), { loading: false, error: "", selectable: false });
const emit = defineEmits<{ retry: []; select: [row: T] }>();
function select(event: Event, row: T) {
  if ((event.target as HTMLElement).closest("button,a,input,summary,select")) return;
  emit("select", row);
}
</script>
<template>
  <section class="min-w-0 space-y-3" :aria-label="label" :aria-busy="loading">
    <ErrorState v-if="error" :message="error" :busy="loading" @retry="emit('retry')" />
    <LoadingSkeleton v-if="loading && !rows.length" />
    <div v-else-if="!rows.length && !error" class="card"><EmptyState :title="empty" /></div>
    <template v-if="rows.length">
      <!-- 桌面表格放进卡片；卡片不裁剪溢出，行内“更多”菜单可以伸出表格。 -->
      <div class="card hidden p-1.5 md:block">
        <table class="data-table">
          <caption class="sr-only">{{ label }}</caption>
          <colgroup><col v-for="column in columns" :key="column.key" :style="column.width ? { width: column.width } : {}" /></colgroup>
          <thead><tr><th v-for="column in columns" :key="column.key" scope="col" :class="column.numeric ? 'number' : 'text-left'">{{ column.label }}</th></tr></thead>
          <tbody>
            <template v-for="row in rows" :key="rowKey(row)">
              <tr :class="[rowTone?.(row), selectable ? 'cursor-pointer hover:bg-surface2/70' : 'hover:bg-surface2/40']"
                :tabindex="selectable ? 0 : undefined" :aria-expanded="expanded == null ? undefined : expanded === rowKey(row)"
                @click="selectable && select($event, row)" @keydown.enter.self.prevent="selectable && emit('select', row)"
                @keydown.space.self.prevent="selectable && emit('select', row)">
                <td v-for="column in columns" :key="column.key" :class="column.numeric ? 'number' : ''"><slot :name="`cell-${column.key}`" :row="row" /></td>
              </tr>
              <tr v-if="expanded === rowKey(row)"><td :colspan="columns.length" class="bg-background/50"><slot name="details" :row="row" /></td></tr>
            </template>
          </tbody>
        </table>
      </div>
      <div class="space-y-3 md:hidden">
        <article v-for="row in rows" :key="rowKey(row)" class="min-w-0 rounded-2xl border border-border/60 bg-surface p-3.5 shadow-card" :class="rowTone?.(row)">
          <div class="grid min-w-0 grid-cols-2 gap-x-3 gap-y-3">
            <!-- 长名称与极端金额允许在窄轨道内换行，不撑大手机卡片。 -->
            <div v-for="column in columns" :key="column.key" class="min-w-0 [overflow-wrap:anywhere]" :class="[column.mobileWide ? 'col-span-2' : '', column.numeric ? 'number' : '']">
              <p class="mb-1 font-sans text-xs font-medium text-muted">{{ column.label }}</p>
              <slot :name="`cell-${column.key}`" :row="row" />
            </div>
          </div>
          <button v-if="selectable" class="button-secondary mt-3 w-full" :aria-expanded="expanded === rowKey(row)" @click="emit('select', row)"><slot name="select-label" /></button>
          <div v-if="expanded === rowKey(row)" class="mt-3 border-t border-border/50 pt-3"><slot name="details" :row="row" /></div>
        </article>
      </div>
    </template>
  </section>
</template>
