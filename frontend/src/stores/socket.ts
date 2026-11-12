import { defineStore } from "pinia";
import { ref } from "vue";
import { BASE_PREFIX } from "@/api/client";

export const useSocketStore = defineStore("socket", () => {
  const connected = ref(false);
  const lastHeartbeat = ref(Date.now());
  const recentEvents = ref<any[]>([]);
  let ws: WebSocket | null = null;
  let reconnectTimer: any = null;

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const wsUrl = `${protocol}//${host}${BASE_PREFIX}/ws/stream`;

    try {
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        connected.value = true;
        lastHeartbeat.value = Date.now();
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          lastHeartbeat.value = Date.now();
          if (payload.type === "execution") {
            recentEvents.value.unshift(payload.data);
            if (recentEvents.value.length > 50) {
              recentEvents.value.pop();
            }
          }
        } catch {
          // non-json
        }
      };

      ws.onclose = () => {
        connected.value = false;
        scheduleReconnect();
      };

      ws.onerror = () => {
        connected.value = false;
        try {
          ws?.close();
        } catch {}
      };
    } catch {
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      connect();
    }, 3000);
  }

  function disconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (ws) {
      ws.close();
      ws = null;
    }
    connected.value = false;
  }

  return {
    connected,
    lastHeartbeat,
    recentEvents,
    connect,
    disconnect,
  };
});
