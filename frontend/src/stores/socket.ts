import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { BASE_PREFIX, notifyUnauthorized } from "@/api/client";
import { MOCK_MODE } from "@/api/mode";
import type { ExecutionRecord, SocketMessage } from "@/types/api";

export const HEARTBEAT_TIMEOUT_MS = 20000;
export const useSocketStore = defineStore("socket", () => {
  const state = ref<"disconnected" | "connecting" | "connected" | "reconnecting" | "mock">("disconnected");
  const connected = computed(() => state.value === "connected");
  const lastHeartbeat = ref<number | null>(null);
  const recentEvents = ref<ExecutionRecord[]>([]);
  const eventSequence = ref(0);
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let heartbeatTimer: ReturnType<typeof setInterval> | undefined;
  let desired = false;
  let attempts = 0;
  function clearTimers() { clearTimeout(reconnectTimer); clearInterval(heartbeatTimer); reconnectTimer = undefined; heartbeatTimer = undefined; }
  function scheduleReconnect() {
    if (!desired || reconnectTimer) return;
    clearInterval(heartbeatTimer);
    state.value = "reconnecting";
    const delay = Math.min(30000, 1000 * 2 ** Math.min(attempts++, 5)) * (0.75 + Math.random() * 0.5);
    reconnectTimer = setTimeout(() => { reconnectTimer = undefined; open(); }, delay);
  }
  function open() {
    if (!desired) return;
    if (socket && socket.readyState < WebSocket.CLOSING) return;
    state.value = attempts ? "reconnecting" : "connecting";
    try {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const current = new WebSocket(`${protocol}//${window.location.host}${BASE_PREFIX}/ws/stream`);
      socket = current;
      current.onopen = () => {
        if (!desired || socket !== current) { current.close(); return; }
        state.value = "connected"; attempts = 0; lastHeartbeat.value = Date.now();
        clearInterval(heartbeatTimer);
        heartbeatTimer = setInterval(() => {
          if (socket !== current || !desired) return;
          if (Date.now() - (lastHeartbeat.value ?? 0) > HEARTBEAT_TIMEOUT_MS) {
            // 心跳超时立即失效当前连接，不等待浏览器无限期完成关闭握手。
            socket = null; current.close(); scheduleReconnect();
          } else if (current.readyState === WebSocket.OPEN) current.send("ping");
        }, 5000);
      };
      current.onmessage = event => {
        if (!desired || socket !== current) return;
        try {
          const payload = JSON.parse(String(event.data)) as SocketMessage;
          lastHeartbeat.value = Date.now();
          if (payload.type === "execution") {
            if (payload.data) recentEvents.value = [payload.data, ...recentEvents.value].slice(0, 50);
            eventSequence.value++;
          }
        } catch { /* 非 JSON 消息不延长可信心跳。 */ }
      };
      current.onclose = event => {
        if (socket !== current) return;
        socket = null;
        clearInterval(heartbeatTimer);
        if (event.code === 1008) { disconnect(); notifyUnauthorized(); return; }
        if (desired) scheduleReconnect();
        else state.value = "disconnected";
      };
      current.onerror = () => {
        if (socket !== current) return;
        socket = null; current.close(); scheduleReconnect();
      };
    } catch { socket = null; scheduleReconnect(); }
  }
  function connect() {
    if (MOCK_MODE) { state.value = "mock"; return; }
    desired = true; open();
  }
  function disconnect() {
    // 先关闭重连意图并分离旧回调，再关闭连接。
    desired = false; clearTimers(); attempts = 0;
    const old = socket; socket = null; old?.close();
    state.value = "disconnected"; lastHeartbeat.value = null;
    recentEvents.value = []; eventSequence.value = 0;
  }
  return { connected, state, lastHeartbeat, recentEvents, eventSequence, connect, disconnect };
});
