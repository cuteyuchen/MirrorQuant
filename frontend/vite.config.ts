import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

function readVersionFile(): string | null {
  // 版本号以仓库根目录的 VERSION 文件为准，缺失时回落到 package.json。
  try {
    const text = readFileSync(fileURLToPath(new URL("../VERSION", import.meta.url)), "utf-8").trim().split(/\s+/)[0];
    return text || null;
  } catch { return null; }
}
function readPackageVersion(): string | null {
  try {
    const pkg = JSON.parse(readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf-8"));
    return typeof pkg.version === "string" && pkg.version ? pkg.version : null;
  } catch { return null; }
}
const APP_VERSION = readVersionFile() ?? readPackageVersion() ?? "dev";

const API_TARGET = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";
const WS_TARGET = API_TARGET.replace(/^http/i, "ws");

export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/mq/",
  define: { __APP_VERSION__: JSON.stringify(APP_VERSION) },
  plugins: [vue()],
  resolve: {
    alias: {
      // 原生 ESM 配置加载也可使用，避免依赖打包器注入 __dirname。
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 默认只监听本机；兼容部署前缀下的开发请求，支持 VITE_API_TARGET 指向远程后端。
    host: "localhost",
    proxy: {
      "/mq/api": {
        target: API_TARGET,
        changeOrigin: true,
        secure: false,
        rewrite: (url) => url.replace(/^\/mq/, ""),
      },
      "/mq/ws": {
        target: WS_TARGET,
        ws: true,
        changeOrigin: true,
        secure: false,
        rewrite: (url) => url.replace(/^\/mq/, ""),
      },
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: WS_TARGET,
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
