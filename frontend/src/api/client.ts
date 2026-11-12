export const BASE_PREFIX = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");

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
      credentials: "include", // Send HttpOnly cookie
    });

    if (!res.ok) {
      let errDetail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        errDetail = body.detail || body.error || errDetail;
      } catch {
        // use default
      }
      throw new Error(errDetail);
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
