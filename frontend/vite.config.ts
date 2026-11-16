import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";

export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/mq/",
  plugins: [vue()],
  resolve: {
    alias: {
      // 原生 ESM 配置加载也可使用，避免依赖打包器注入 __dirname。
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 默认只监听本机；兼容部署前缀下的开发请求。
    host: "localhost",
    proxy: {
      "/mq/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (url) => url.replace(/^\/mq/, ""),
      },
      "/mq/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        rewrite: (url) => url.replace(/^\/mq/, ""),
      },
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
