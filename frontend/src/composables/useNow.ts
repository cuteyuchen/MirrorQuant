import { onScopeDispose, readonly, ref } from "vue";

// 所有相对时间共用一个秒级时钟，最后一个使用者离开后停止计时。
const now = ref(Date.now());
let consumers = 0;
let timer: ReturnType<typeof setInterval> | undefined;
export function useNow() {
  if (consumers++ === 0) {
    now.value = Date.now();
    timer = setInterval(() => { now.value = Date.now(); }, 1000);
  }
  onScopeDispose(() => {
    if (--consumers === 0) { clearInterval(timer); timer = undefined; }
  });
  return readonly(now);
}
