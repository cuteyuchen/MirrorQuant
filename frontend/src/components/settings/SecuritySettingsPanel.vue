<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { KeyRound, LockKeyhole, RefreshCw, ShieldCheck, Unlock } from "lucide-vue-next";
import { api, ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useDataCacheStore } from "@/stores/dataCache";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useNow } from "@/composables/useNow";
import { useI18n } from "@/i18n";
import type { SecurityGuard, SettingsResponse, TotpSetup } from "@/types/api";
import { errorMessage, fieldErrors, reasonText } from "@/utils/errors";
import { formatDateTime } from "@/utils/format";
import DataTable from "@/components/DataTable.vue";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";

const props = defineProps<{ security: SettingsResponse["security"] }>();
const emit = defineEmits<{ dirty: [value: boolean]; busy: [value: boolean] }>();
const auth = useAuthStore();
const cache = useDataCacheStore();
const router = useRouter();
const { t } = useI18n();
const now = useNow();
const action = useAsyncAction();
const readAction = useAsyncAction();
const passwordForm = ref({ current: "", next: "", repeat: "" });
const passwordStep = ref(false);
const enablePassword = ref("");
const enableCode = ref("");
const disablePassword = ref("");
const disableCode = ref("");
const flow = ref<"enable" | "disable" | null>(null);
const setup = ref<TotpSetup | null>(null);
const errors = ref<Record<string, string>>({});
const guardError = ref("");
const auditError = ref("");
const totpEnabled = computed(() => auth.user?.totpEnabled ?? props.security?.totpEnabled ?? null);
const dirty = computed(() => passwordStep.value || flow.value != null);
watch(dirty, value => emit("dirty", value));
watch(action.pending, value => emit("busy", value));

// 密码、启用及停用各有独立草稿；关闭流程时销毁敏感输入。
function reset() {
  passwordStep.value = false; passwordForm.value = { current: "", next: "", repeat: "" };
  flow.value = null; setup.value = null; enablePassword.value = ""; enableCode.value = "";
  disablePassword.value = ""; disableCode.value = ""; errors.value = {}; action.error.value = "";
}
async function leaveFlow() {
  if (!dirty.value) return true;
  const result = await action.run(async () => { reset(); }, {
    confirm: { title: t("set.discard"), message: t("set.discardConfirm") }, success: false,
  });
  return result.ok;
}
async function openFlow(next: "enable" | "disable") { if (await leaveFlow()) { reset(); flow.value = next; } }
async function openPassword() { if (await leaveFlow()) { reset(); passwordStep.value = true; } }
async function signedOut() {
  reset(); auth.clearSession();
  await router.replace({ path: "/login", query: { redirect: "/settings" } });
}
async function updateSecurity(endpoint: string, payload: Record<string, string>) {
  // 安全接口必须明确确认成功，缺失或 false 的回执不结束当前流程。
  const response = await api.post<{ ok: boolean | null }>(endpoint, payload);
  if (response?.ok !== true) throw new ApiError(502, "INVALID_RESPONSE");
  return response;
}
async function changePassword() {
  errors.value = {};
  if (!passwordForm.value.current) errors.value.current_password = t("validation.required");
  if (passwordForm.value.next.length < 8) errors.value.new_password = t("setup.errMinLen");
  if (passwordForm.value.next !== passwordForm.value.repeat) errors.value.repeat = t("setup.errMismatch");
  if (Object.keys(errors.value).length) return;
  const result = await action.run(() => updateSecurity("/api/auth/change-password", {
    currentPassword: passwordForm.value.current, newPassword: passwordForm.value.next,
  }), { confirm: { title: t("security.changePassword"), message: t("security.reloginNotice") },
    onError: cause => { errors.value = fieldErrors(cause); }, success: t("security.changed") });
  if (result.ok) await signedOut();
}
async function prepare() {
  if (!enablePassword.value) { errors.value.current_password = t("validation.required"); return; }
  const result = await action.run(async () => {
    const response = await api.post<TotpSetup>("/api/auth/2fa/setup", { currentPassword: enablePassword.value });
    // 验证材料不完整时保留密码步骤，不能伪装成可验证的二维码。
    if (!response?.qrSvgDataUri || !response.secret) throw new ApiError(502, "INVALID_RESPONSE");
    return response;
  }, { success: false, onError: cause => { errors.value = fieldErrors(cause); } });
  if (result.ok) { setup.value = result.value; enablePassword.value = ""; errors.value = {}; }
}
async function enable() {
  if (!/^\d{6}$/.test(enableCode.value)) { errors.value.code = t("security.sixDigits"); return; }
  const result = await action.run(() => updateSecurity("/api/auth/2fa/enable", { code: enableCode.value }), {
    success: t("security.changed"), onError: cause => { errors.value = fieldErrors(cause); },
  });
  if (result.ok) await signedOut();
}
async function disable() {
  errors.value = {};
  if (!disablePassword.value) errors.value.current_password = t("validation.required");
  if (!/^\d{6}$/.test(disableCode.value)) errors.value.code = t("security.sixDigits");
  if (Object.keys(errors.value).length) return;
  const result = await action.run(() => updateSecurity("/api/auth/2fa/disable", {
    currentPassword: disablePassword.value, code: disableCode.value,
  }), { confirm: { title: t("security.disableTitle"), message: t("security.disableConfirm"), strong: true },
    success: t("security.changed"), onError: cause => { errors.value = fieldErrors(cause); } });
  if (result.ok) await signedOut();
}
async function load(force = false) {
  // 审计列表失败不影响修改密码或配置两步验证。
  await readAction.run(async () => {
    const results = await Promise.allSettled([cache.fetchAuditLogs(force), cache.fetchSecurityGuards(force)]);
    guardError.value = results[1].status === "rejected" ? errorMessage(results[1].reason) : "";
    auditError.value = results[0].status === "rejected" ? errorMessage(results[0].reason) : "";
  }, { success: false });
}
function remaining(guard: SecurityGuard) {
  if (guard.lockRemainingSec == null || !cache.securityGuards) return null;
  return Math.max(0, Math.ceil(guard.lockRemainingSec - (now.value - cache.securityGuards.fetchedAt) / 1000));
}
async function unlock(guard: SecurityGuard) {
  await action.run(async () => {
    const response = await api.post<{ ok?: boolean }>(`/api/settings/security-guards/${guard.id}/unlock`);
    if (response.ok !== true) throw new ApiError(502, "INVALID_RESPONSE");
    await load(true);
  }, { confirm: { title: t("set.unlock"), message: t("security.unlockConfirm"), strong: true } });
}
onMounted(() => { void load(); });
</script>

<template>
  <div class="space-y-6">
    <section class="border-b border-border/50 pb-5">
      <header class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="section-title flex items-center gap-2"><LockKeyhole :size="18" />{{ t("security.changePassword") }}</h2>
        <button v-if="!passwordStep" class="button-secondary" :disabled="action.pending.value" @click="openPassword"><KeyRound :size="16" />{{ t("security.changePassword") }}</button>
      </header>
      <form v-if="passwordStep" class="mt-4 max-w-xl" @submit.prevent="changePassword">
        <fieldset class="space-y-4" :disabled="action.pending.value">
          <!-- 安全流程错误留在当前表单，不让用户到其他分区寻找。 -->
          <p v-if="action.error.value" class="error-banner" role="alert">{{ action.error.value }}</p>
          <label class="field-label">{{ t("security.password") }} *<input v-model="passwordForm.current" class="form-input mt-1" type="password" required autocomplete="current-password" /><span class="field-error">{{ errors.current_password }}</span></label>
          <label class="field-label">{{ t("security.newPassword") }} *<input v-model="passwordForm.next" class="form-input mt-1" type="password" required minlength="8" maxlength="128" autocomplete="new-password" /><span class="field-error">{{ errors.new_password }}</span></label>
          <label class="field-label">{{ t("setup.confirmPassword") }} *<input v-model="passwordForm.repeat" class="form-input mt-1" type="password" required maxlength="128" autocomplete="new-password" /><span class="field-error">{{ errors.repeat }}</span></label>
          <p class="text-xs text-muted">{{ t("security.reloginNotice") }}</p>
          <div class="flex flex-wrap gap-2"><button type="button" class="button-secondary" @click="reset">{{ t("common.cancel") }}</button><button class="button-primary"><KeyRound :size="16" />{{ t("common.save") }}</button></div>
        </fieldset>
      </form>
    </section>
    <section class="border-b border-border/50 pb-5">
      <header class="flex flex-wrap items-center justify-between gap-3">
        <div><h2 class="section-title flex items-center gap-2"><ShieldCheck :size="18" />{{ t("security.twoFactor") }}</h2><p class="mt-1 text-xs" :class="totpEnabled ? 'text-success' : 'text-warning'">{{ t(totpEnabled == null ? "security.totpUnknown" : totpEnabled ? "security.enabled" : "security.disabled") }}</p></div>
        <button v-if="!flow" class="button-secondary" :disabled="action.pending.value || totpEnabled == null" @click="openFlow(totpEnabled ? 'disable' : 'enable')">{{ t(totpEnabled ? "security.disableTitle" : "security.enableTitle") }}</button>
      </header>
      <form v-if="flow === 'enable'" class="mt-4 max-w-xl" @submit.prevent="setup ? enable() : prepare()">
        <fieldset class="space-y-4" :disabled="action.pending.value">
          <p v-if="action.error.value" class="error-banner" role="alert">{{ action.error.value }}</p>
          <p class="text-xs text-muted">{{ t(setup ? "security.stepVerify" : "security.stepPassword") }}</p>
          <label v-if="!setup" class="field-label">{{ t("security.password") }} *<input v-model="enablePassword" type="password" class="form-input mt-1" autocomplete="current-password" required /><span class="field-error">{{ errors.current_password }}</span></label>
          <template v-else>
            <img :src="setup.qrSvgDataUri" :alt="t('security.qrAlt')" width="192" height="192" class="rounded bg-foreground p-2" />
            <p class="select-all break-all font-mono text-xs">{{ setup.secret }}</p>
            <label class="field-label">{{ t("security.code") }} *<input v-model="enableCode" type="text" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" class="form-input mt-1" required /><span class="field-error">{{ errors.code }}</span></label>
          </template>
          <div class="flex flex-wrap gap-2"><button type="button" class="button-secondary" @click="reset">{{ t("common.cancel") }}</button><button class="button-primary"><ShieldCheck :size="16" />{{ t(setup ? "security.enable" : "security.prepare") }}</button></div>
        </fieldset>
      </form>
      <form v-if="flow === 'disable'" class="mt-4 max-w-xl" @submit.prevent="disable">
        <fieldset class="space-y-4" :disabled="action.pending.value">
          <p v-if="action.error.value" class="error-banner" role="alert">{{ action.error.value }}</p>
          <label class="field-label">{{ t("security.password") }} *<input v-model="disablePassword" type="password" class="form-input mt-1" autocomplete="current-password" required /><span class="field-error">{{ errors.current_password }}</span></label>
          <label class="field-label">{{ t("security.code") }} *<input v-model="disableCode" type="text" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" class="form-input mt-1" required /><span class="field-error">{{ errors.code }}</span></label>
          <div class="flex flex-wrap gap-2"><button type="button" class="button-secondary" @click="reset">{{ t("common.cancel") }}</button><button class="button-danger">{{ t("security.disable") }}</button></div>
        </fieldset>
      </form>
    </section>
    <details class="border-b border-border/50 pb-5" :open="!!guardError || !!action.error.value && !passwordStep && !flow">
      <summary class="cursor-pointer text-sm font-semibold">{{ t("set.secGuardsTitle") }}<span v-if="guardError" class="ml-2 text-warning">{{ t("state.ERROR") }}</span></summary>
      <div class="mt-4 space-y-3">
      <div class="flex justify-end"><button class="icon-button" :disabled="readAction.pending.value" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" /></button></div>
      <p v-if="action.error.value && !passwordStep && !flow" class="error-banner" role="alert">{{ action.error.value }}</p>
      <ErrorState v-if="guardError" :message="guardError" :busy="readAction.pending.value" @retry="load(true)" />
      <LoadingSkeleton v-if="readAction.pending.value && !cache.securityGuards" />
      <EmptyState v-else-if="!cache.securityGuards?.data.length && !guardError && !readAction.pending.value" :title="t('set.noGuards')" />
      <div v-for="guard in cache.securityGuards?.data || []" :key="guard.id" class="flex flex-wrap items-center justify-between gap-3 border-b border-border/50 py-3">
        <span class="break-all text-sm">{{ guard.guardKey ?? t("common.unknown") }}</span>
        <span class="number text-xs" :class="(remaining(guard) ?? 0) > 0 ? 'text-warning' : 'text-muted'">{{ remaining(guard) == null ? t("common.unknown") : t("set.activeLocked", { sec: remaining(guard)! }) }}</span>
        <button class="button-secondary" :disabled="action.pending.value || !((guard.failCount ?? 0) > 0 || (remaining(guard) ?? 0) > 0)" @click="unlock(guard)"><Unlock :size="16" />{{ t("set.unlock") }}</button>
      </div>
      </div>
    </details>
    <details :open="!!auditError">
      <summary class="cursor-pointer text-sm font-semibold">{{ t("set.auditTitle") }}<span class="ml-2 text-xs font-normal" :class="auditError ? 'text-warning' : 'text-muted'">{{ t(auditError ? "state.ERROR" : "set.auditSummary") }}</span></summary>
      <DataTable class="mt-4" :rows="cache.auditLogs?.data || []" :loading="readAction.pending.value" :error="auditError" :label="t('set.auditTitle')" :row-key="row => row.id" @retry="load(true)" :columns="[
        { key: 'time', label: t('common.time'), width: '22%' }, { key: 'user', label: t('set.colUser'), width: '18%' },
        { key: 'action', label: t('set.colAction'), width: '22%' }, { key: 'details', label: t('executions.details'), mobileWide: true },
      ]">
        <template #cell-time="{ row }"><span class="tabular-nums text-xs">{{ formatDateTime(row.createdAt) }}</span></template>
        <template #cell-user="{ row }">{{ row.username ?? t("common.unknown") }}</template>
        <template #cell-action="{ row }">{{ reasonText(row.action) }}</template>
        <template #cell-details="{ row }"><span class="break-words text-xs text-muted">{{ reasonText(row.details) }}</span></template>
        <template #mobile-card="{ row }">
          <div class="space-y-1">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <strong class="text-sm">{{ reasonText(row.action) }}</strong>
              <time class="font-mono text-xs tabular-nums text-muted">{{ formatDateTime(row.createdAt, { short: true }) }}</time>
            </div>
            <p class="text-xs text-muted">{{ row.username ?? t("common.unknown") }}</p>
            <p v-if="row.details" class="break-words text-xs text-muted">{{ reasonText(row.details) }}</p>
          </div>
        </template>
      </DataTable>
    </details>
  </div>
</template>
