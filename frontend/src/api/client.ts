import type { ErrorBody } from "@/types/api";
import { MOCK_MODE } from "./mode";

export const BASE_PREFIX = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");
export const REQUEST_TIMEOUT_MS = 15000;
let unauthorizedHandler: (() => void) | undefined;
export function setUnauthorizedHandler(handler: () => void) { unauthorizedHandler = handler; }
export function notifyUnauthorized() { unauthorizedHandler?.(); }

export class ApiError extends Error {
  constructor(public status: number, public detail: string, public data: ErrorBody | null = null) {
    super(detail);
    this.name = "ApiError";
  }
}
export class CancelledRequestError extends Error {}
export function isCancelled(error: unknown) { return error instanceof CancelledRequestError; }

class ApiClient {
  resolveUrl(endpoint: string) { return `${BASE_PREFIX}${endpoint}`; }
  async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    if (MOCK_MODE) {
      const { mockResponse, mockWrite } = await import("@/mocks/board");
      if (options.method && options.method !== "GET") {
        // 预览模式只模拟个别无副作用的写操作，其余一律只读。
        const written = mockWrite(endpoint, options.method, options.body);
        if (written === undefined) throw new ApiError(409, "MOCK_READONLY");
        return written as T;
      }
      return mockResponse(endpoint) as T;
    }
    const controller = new AbortController();
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
    const abort = () => controller.abort();
    if (options.signal?.aborted) controller.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    const headers = new Headers(options.headers);
    if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    try {
      const response = await fetch(this.resolveUrl(endpoint), { ...options, headers, signal: controller.signal, credentials: "include" });
      if (!response.ok) {
        // 即使 401 的响应体损坏，也必须立即清理会话。
        if (response.status === 401) notifyUnauthorized();
        const body = await response.json().catch(() => null) as ErrorBody | null;
        const detail = body?.detail;
        const text = typeof detail === "string" ? detail : detail && !Array.isArray(detail) ? detail.message : body?.error;
        throw new ApiError(response.status, text || `HTTP ${response.status}`, body);
      }
      if (response.status === 204) return undefined as T;
      return await response.json() as T;
    } catch (cause: unknown) {
      if (cause instanceof ApiError) throw cause;
      if (timedOut) throw new ApiError(408, "REQUEST_TIMEOUT");
      if (controller.signal.aborted) throw new CancelledRequestError();
      throw new ApiError(0, "NETWORK_ERROR");
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abort);
    }
  }
  get<T>(endpoint: string, options: RequestInit = {}) {
    return this.request<T>(endpoint, { ...options, method: "GET" });
  }
  post<T = unknown>(endpoint: string, body?: unknown) {
    return this.request<T>(endpoint, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
  }
  put<T = unknown>(endpoint: string, body?: unknown) {
    return this.request<T>(endpoint, { method: "PUT", body: body === undefined ? undefined : JSON.stringify(body) });
  }
  delete<T = unknown>(endpoint: string) { return this.request<T>(endpoint, { method: "DELETE" }); }
}
export const api = new ApiClient();
