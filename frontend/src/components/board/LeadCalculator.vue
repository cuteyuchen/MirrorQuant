<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/utils/errors";
import PositionCalculatorCard from "@/components/PositionCalculatorCard.vue";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";

const props = defineProps<{ leadId: number; name?: string | null }>();
const cache = useDataCacheStore();
const { t } = useI18n();
const loading = ref(false);
const error = ref("");
const entry = computed(() => cache.leadSnapshots[props.leadId]);
let timer: ReturnType<typeof setInterval> | undefined;
// 看板里就地测算：只有展开时才取这位带单员的快照，之后每 15 秒跟着仓位刷新一次。
async function load(force = false) {
  loading.value = true;
  try { await cache.fetchLeadSnapshot(props.leadId, force); error.value = ""; }
  catch (cause: unknown) { if (!isCancelled(cause)) error.value = errorMessage(cause); }
  finally { loading.value = false; }
}
function start() {
  if (timer) return;
  void load();
  timer = setInterval(() => { void load(true); }, 15000);
}
function stop() { clearInterval(timer); timer = undefined; }
// 看板在 KeepAlive 里：切到别的页面时停掉轮询，回来再续上。
onMounted(start);
onActivated(start);
onDeactivated(stop);
onUnmounted(stop);
watch(() => props.leadId, () => { stop(); start(); });
</script>
<template>
  <div class="min-w-0 space-y-3">
    <ErrorState v-if="error" :message="error" :busy="loading" @retry="load(true)" />
    <LoadingSkeleton v-if="loading && !entry" />
    <PositionCalculatorCard v-else-if="entry?.data" :snapshot="entry.data" :display-name="name" compact />
    <EmptyState v-else-if="entry" class="tile" :title="t('leads.noSnapshot')" />
  </div>
</template>
