import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { parse } from "@vue/compiler-sfc";
import { baseParse, NodeTypes } from "@vue/compiler-dom";
import { messages } from "@/i18n";
import { SETTINGS_TABS } from "@/utils/settings";
import { ORDER_MODES, TP_SL_MODES, RISK_RULES, RESILIENCE_RULES, RISK_SWITCHES, RISK_SCALE_RULE, type NumericRule } from "@/constants/validation";

// 旧字典和新元组统一检查，动态枚举文案也必须保持双语契约。
describe("双语文案契约", () => {
  it("zh-CN与en-US的key完全一致", () => {
    expect(Object.keys(messages["zh-CN"]).sort()).toEqual(Object.keys(messages["en-US"]).sort());
  });
  it("两种语言的参数占位符一致", () => {
    const params = (value: string) => [...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort();
    for (const [key, value] of Object.entries(messages["zh-CN"])) {
      expect(params(value), key).toEqual(params(messages["en-US"][key] ?? ""));
    }
  });

  // 检查真实调用点与条件分支，避免两个字典同时缺 key 却通过对齐测试。
  it("源码中的字面与条件翻译调用都有双语文案", () => {
    const missing = new Set<string>();
    function check(key: string, file: string) {
      if (!(key in messages["zh-CN"]) || !(key in messages["en-US"])) missing.add(`${file}: ${key}`);
    }
    function inspect(text: string, file: string) {
      const ast = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
      function argument(node: ts.Expression): void {
        if (ts.isStringLiteralLike(node)) check(node.text, file);
        else if (ts.isConditionalExpression(node)) { argument(node.whenTrue); argument(node.whenFalse); }
        else if (ts.isParenthesizedExpression(node) || ts.isNonNullExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) argument(node.expression);
      }
      function visit(node: ts.Node): void {
        if (ts.isCallExpression(node) && node.expression.getText(ast) === "t" && node.arguments[0]) argument(node.arguments[0]);
        if (ts.isPropertyAssignment(node) && ["label", "hint"].includes(node.name.getText(ast))
          && ts.isStringLiteralLike(node.initializer) && /^\w+\.[\w.]+$/.test(node.initializer.text)) check(node.initializer.text, file);
        ts.forEachChild(node, visit);
      }
      visit(ast);
    }
    function template(node: unknown, file: string): void {
      if (!node || typeof node !== "object") return;
      const value = node as Record<string, unknown>;
      if (value.type === NodeTypes.SIMPLE_EXPRESSION && typeof value.content === "string") inspect(value.content, file);
      for (const item of Object.values(value)) {
        if (Array.isArray(item)) item.forEach(child => template(child, file));
        else if (item && typeof item === "object") template(item, file);
      }
    }
    function directory(path: string): void {
      for (const entry of readdirSync(path, { withFileTypes: true })) {
        const file = join(path, entry.name);
        if (entry.isDirectory()) { directory(file); continue; }
        if (!/\.(ts|vue)$/.test(file)) continue;
        const source = readFileSync(file, "utf8");
        if (file.endsWith(".ts")) { inspect(source, file); continue; }
        const { descriptor, errors } = parse(source, { filename: file });
        expect(errors, file).toEqual([]);
        if (descriptor.script) inspect(descriptor.script.content, file);
        if (descriptor.scriptSetup) inspect(descriptor.scriptSetup.content, file);
        if (descriptor.template) template(baseParse(descriptor.template.content), file);
      }
    }
    // npm test 从 frontend 启动，不让 Vite 把目录 URL 当成浏览器资源转换。
    directory(join(process.cwd(), "src"));
    expect([...missing].sort()).toEqual([]);
  });

  it("页签、风控参数、开关与下单模式的动态文案完整", () => {
    const rules: NumericRule[] = [...Object.values(RISK_RULES), ...Object.values(RESILIENCE_RULES), RISK_SCALE_RULE];
    const keys = [
      ...SETTINGS_TABS.map(tab => `settingsTab.${tab}`),
      ...[...ORDER_MODES, ...TP_SL_MODES].map(mode => `mode.${mode}`),
      ...Object.values(RISK_SWITCHES),
      ...Object.keys(RISK_SWITCHES).map(key => `riskHelp.${key}`),
      ...rules.flatMap(rule => [rule.label, rule.hint, rule.unit ? `unit.${rule.unit}` : undefined]).filter((key): key is string => !!key),
    ];
    for (const key of keys) {
      expect(messages["zh-CN"][key], key).toBeTypeOf("string");
      expect(messages["en-US"][key], key).toBeTypeOf("string");
    }
  });
});
