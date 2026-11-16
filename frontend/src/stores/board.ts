import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { useDataCacheStore } from "./dataCache";
import { isCancelled } from "@/api/client";
import { errorMessage } from "@/utils/errors";

// App 维护全局轮询，离开看板后状态条仍持续刷新。
export const useBoardStore = defineStore("board", () => {
  const cache = useDataCacheStore();
  const data = computed(() => cache.board?.data ?? null);
  const receivedAt = computed(() => cache.board?.fetchedAt ?? null);
  const loading = ref(false);
  const error = ref("");
  let sequence = 0;
  async function refresh(force = false) {
    if (loading.value && !force) return;
    const request = ++sequence;
    loading.value = true;
    try {
      await cache.fetchBoard(force);
      if (request === sequence) error.value = "";
    } catch (cause: unknown) {
      if (request === sequence && !isCancelled(cause)) error.value = errorMessage(cause);
    } finally { if (request === sequence) loading.value = false; }
  }
  function clear() { sequence++; loading.value = false; error.value = ""; }
  return { data, receivedAt, loading, error, refresh, clear };
});
