import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import * as vue from "vue";

// Exercise the actual view's loader with a reactive cache and offline requests.
async function view(overrides = {}) {
  const cache = vue.reactive({
    fetchSettings: async () => ({ risk_defaults: {} }),
    fetchAccounts: async () => [{ id: 1 }],
    fetchProxies: async () => [],
    fetchAuditLogs: async () => [],
    fetchSecurityGuards: async () => [],
    ...overrides,
  });
  const source = await readFile(new URL("../src/views/SettingsView.vue", import.meta.url), "utf8");
  const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const { outputText } = ts.transpileModule(script + "\nexport { loadData, settingsData, loading, loadError, accounts, proxies, auditLogs, securityGuards };", {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ESNext },
  });
  const exports = {};
  runInNewContext(outputText, {
    exports, defineOptions() {},
    console: { warn() {} },
    require(name) {
      if (name === "vue") return { ...vue, onMounted() {}, onActivated() {} };
      if (name === "vue-router") return { useRouter: () => ({}) };
      if (name === "@/stores/dataCache") return { useDataCacheStore: () => cache };
      if (name === "@/stores/auth") return { useAuthStore: () => ({}) };
      if (name === "@/i18n") return { useI18n: () => ({ t: key => key }) };
      return {};
    },
  });
  return exports;
}

test("settings render when all optional requests fail", async () => {
  const fail = async () => { throw Error("offline"); };
  const v = await view({ fetchAccounts: fail, fetchProxies: fail, fetchAuditLogs: fail, fetchSecurityGuards: fail });
  await v.loadData();
  assert.ok(v.settingsData.value);
  assert.equal(v.loadError.value, "");
  for (const name of ["accounts", "proxies", "auditLogs", "securityGuards"]) assert.equal(v[name].value.length, 0);
  assert.equal(v.loading.value, false);
});

test("core failure ends loading and retry can recover", async () => {
  let fail = true;
  const flags = [];
  const v = await view({ fetchSettings: async force => {
    flags.push(force);
    if (fail) throw Error("offline");
    return { risk_defaults: {} };
  } });
  await v.loadData();
  assert.equal(v.settingsData.value, null);
  assert.equal(v.loadError.value, "set.loadError");
  assert.equal(v.loading.value, false);
  fail = false;
  await v.loadData(true);
  assert.ok(v.settingsData.value);
  assert.equal(v.loadError.value, "");
  assert.deepEqual(flags, [false, true]);
});

test("loading stays visible while requests are pending", async () => {
  let resolve;
  const pending = new Promise(r => { resolve = r; });
  const v = await view({ fetchSettings: () => pending });
  const loaded = v.loadData();
  assert.equal(v.loading.value, true);
  assert.equal(v.settingsData.value, null);
  resolve({ risk_defaults: {} });
  await loaded;
  assert.equal(v.loading.value, false);
  assert.ok(v.settingsData.value);
});

test("reactive cached settings survive refresh failure without clone errors", async () => {
  const v = await view({ settings: { data: { risk_defaults: { enable_circuit_breaker: false } } },
    fetchSettings: async () => { throw Error("offline"); } });
  assert.equal(v.settingsData.value.risk_defaults.enable_circuit_breaker, false);
  await v.loadData();
  assert.equal(v.settingsData.value.risk_defaults.enable_circuit_breaker, false);
  assert.equal(v.loadError.value, "set.loadError");
});
