import { defineConfig } from "vitest/config";
import { compileScript, parse, registerTS } from "@vue/compiler-sfc";
import ts from "typescript";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

// 使用官方 SFC 编译器保留宏和模板语义，TS 转译留在进程内，避免 esbuild 子进程。
registerTS(() => ts);
const sourceCompiler = {
  name: "mirrorquant-test-source",
  enforce: "pre",
  transform(source, id) {
    const filename = id.split("?")[0];
    if (filename.includes("/node_modules/") || !/\.(ts|vue)$/.test(filename)) return;
    let script = source;
    if (filename.endsWith(".vue")) {
      const { descriptor, errors } = parse(source, { filename });
      if (errors.length) throw errors[0];
      const compiled = compileScript(descriptor, {
        id: createHash("sha256").update(filename).digest("hex").slice(0, 8),
        inlineTemplate: true,
      });
      script = compiled.content;
    }
    const result = ts.transpileModule(script, {
      fileName: filename,
      compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext, sourceMap: true },
    });
    return { code: result.outputText, map: result.sourceMapText };
  },
};

// 单线程测试池避免 Windows 沙箱的子进程限制，测试仍挂载真实 Vue 组件。
export default defineConfig({
  plugins: [sourceCompiler],
  esbuild: false,
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    // 本仓库没有链接依赖；避免测试服务为映射盘枚举 Windows 网络连接。
    preserveSymlinks: true,
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.spec.ts"],
    pool: "threads",
    poolOptions: { threads: { singleThread: true } },
    restoreMocks: true,
    clearMocks: true,
  },
});
