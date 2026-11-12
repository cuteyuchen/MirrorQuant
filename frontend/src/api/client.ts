export const BASE_PREFIX = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  detail: string;
  data: any;

  constructor(status: number, detail: string, data: any = null) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.data = data;
  }
}

export interface ApiResponse<T = any> {
  data?: T;
  error?: string;
}

class ApiClient {
  private baseUrl = BASE_PREFIX;

  resolveUrl(endpoint: string): string {
    return `${this.baseUrl}${endpoint}`;
  }

  async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    const url = this.resolveUrl(endpoint);
    const headers = new Headers(options.headers || {});
    if (!headers.has("Content-Type") && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }

    const res = await fetch(url, {
      ...options,
      headers,
      credentials: "include",
    });

    if (!res.ok) {
      let errDetail = `HTTP ${res.status}`;
      let body: any = null;
      try {
        body = await res.json();
        if (typeof body.detail === "string") {
          errDetail = body.detail;
        } else if (body.detail && typeof body.detail === "object") {
          errDetail = body.detail.message || JSON.stringify(body.detail);
        } else if (body.error) {
          errDetail = body.error;
        }
      } catch {
        // use default
      }
      throw new ApiError(res.status, errDetail, body);
    }

    if (res.status === 204) {
      return {} as T;
    }
    return res.json();
  }

  get<T>(endpoint: string): Promise<T> {
    return this.request<T>(endpoint, { method: "GET" });
  }

  post<T>(endpoint: string, body?: any): Promise<T> {
    return this.request<T>(endpoint, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  put<T>(endpoint: string, body?: any): Promise<T> {
    return this.request<T>(endpoint, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  delete<T>(endpoint: string): Promise<T> {
    return this.request<T>(endpoint, { method: "DELETE" });
  }
}

export const api = new ApiClient();
