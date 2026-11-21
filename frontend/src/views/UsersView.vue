<script setup lang="ts">
import { computed, onActivated, onMounted, ref } from "vue";
import { KeyRound, Plus, RefreshCw, Trash2 } from "lucide-vue-next";
import { api, ApiError, isCancelled } from "@/api/client";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useI18n } from "@/i18n";
import type { AdminUserBinding, AdminUserDetail, AdminUserSummary, InviteCode } from "@/types/api";
import { formatDateTime, formatQty, formatUsd } from "@/utils/format";
import { errorMessage } from "@/utils/errors";
import BaseModal from "@/components/BaseModal.vue";
import DataTable from "@/components/DataTable.vue";
import EmptyState from "@/components/EmptyState.vue";
import PageHeader from "@/components/PageHeader.vue";

defineOptions({ name: "UsersView" });
const { t } = useI18n();
const { pending, error, run } = useAsyncAction();
const users = ref<AdminUserSummary[]>([]);
const invites = ref<InviteCode[]>([]);
const loading = ref(false);
const loadError = ref("");
const expanded = ref<number | null>(null);
const details = ref<Record<number, AdminUserDetail>>({});
const detailError = ref<Record<number, string>>({});
const detailLoading = ref<Record<number, boolean>>({});
// 409 未关项以后端 detail 原文展示，不做翻译或改写，管理员按原文逐项处理。
const deleteBlockers = ref("");
const resetTarget = ref<AdminUserSummary | null>(null);
const newPassword = ref("");
const resetError = ref("");
const resetBusy = ref(false);
const inviteMaxUses = ref(1);
const inviteExpiresAt = ref("");
const inviteError = ref("");
const inviteBusy = ref(false);
const rows = computed(() => [...users.value].sort((a, b) => a.id - b.id));
const summary = computed(() => t("users.summary", { n: users.value.length }));
let sequence = 0;

async function load(force = false) {
  if (loading.value && !force) return;
  const request = ++sequence; loading.value = true; loadError.value = "";
  const results = await Promise.allSettled([
    api.get<{ items: AdminUserSummary[] }>("/api/admin/users"),
    api.get<{ items: InviteCode[] }>("/api/admin/invite-codes"),
  ]);
  if (request !== sequence) return;
  if (results[0].status === "fulfilled") users.value = results[0].value.items ?? [];
  else if (!isCancelled(results[0].reason)) loadError.value = errorMessage(results[0].reason);
  if (results[1].status === "fulfilled") invites.value = results[1].value.items ?? [];
  else if (!isCancelled(results[1].reason) && !loadError.value) loadError.value = errorMessage(results[1].reason);
  loading.value = false;
}
async function selectUser(row: AdminUserSummary) {
  const opening = expanded.value !== row.id;
  expanded.value = opening ? row.id : null;
  deleteBlockers.value = "";
  if (!opening || details.value[row.id]) return;
  detailLoading.value[row.id] = true; detailError.value[row.id] = "";
  try {
    details.value[row.id] = await api.get<AdminUserDetail>(`/api/admin/users/${row.id}`);
  } catch (cause: unknown) { detailError.value[row.id] = errorMessage(cause); }
  finally { detailLoading.value[row.id] = false; }
}
function inviteStatus(code: InviteCode) {
  return code.status ?? (code.isRevoked ? "revoked" : "active");
}
const inviteStatusKey: Record<string, string> = {
  active: "invites.statusActive", revoked: "invites.statusRevoked",
  expired: "invites.statusExpired", exhausted: "invites.statusExhausted",
};
async function toggleActive(user: AdminUserSummary) {
  const next = user.isActive !== true;
  await run(async () => {
    const updated = await api.request<{ user: AdminUserSummary }>(`/api/admin/users/${user.id}`, {
      method: "PATCH", body: JSON.stringify({ isActive: next }),
    });
    const index = users.value.findIndex(item => item.id === user.id);
    if (index >= 0) users.value[index] = updated.user;
  }, { confirm: { title: t(next ? "common.enable" : "common.disable"), message: t("confirm.toggle", { name: user.username, action: t(next ? "common.enable" : "common.disable") }) }, success: t("users.statusChanged") });
}
async function changeRole(user: AdminUserSummary) {
  const next = user.role === "admin" ? "user" : "admin";
  await run(async () => {
    const updated = await api.request<{ user: AdminUserSummary }>(`/api/admin/users/${user.id}`, {
      method: "PATCH", body: JSON.stringify({ role: next }),
    });
    const index = users.value.findIndex(item => item.id === user.id);
    if (index >= 0) users.value[index] = updated.user;
  }, { confirm: { title: t(next === "admin" ? "users.makeAdmin" : "users.makeUser"), message: t("confirm.toggle", { name: user.username, action: t(next === "admin" ? "users.makeAdmin" : "users.makeUser") }) }, success: t("users.roleChanged") });
}
function openReset(user: AdminUserSummary) { resetTarget.value = user; newPassword.value = ""; resetError.value = ""; }
async function submitReset() {
  if (!resetTarget.value || resetBusy.value) return;
  if (newPassword.value.length < 8 || newPassword.value.length > 128) { resetError.value = t("auth.passwordLength"); return; }
  resetBusy.value = true; resetError.value = "";
  try {
    await api.post(`/api/admin/users/${resetTarget.value.id}/reset-password`, { newPassword: newPassword.value });
    resetTarget.value = null; newPassword.value = "";
  } catch (cause: unknown) { resetError.value = errorMessage(cause); }
  finally { resetBusy.value = false; }
}
function extractBlockers(cause: unknown): string {
  if (!(cause instanceof ApiError)) return "";
  const detail = cause.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail)) return detail.map(item => typeof item === "string" ? item : item.msg ?? "").filter(Boolean).join("\n");
  return cause.detail && !cause.detail.startsWith("HTTP ") ? cause.detail : "";
}
async function deleteUser(user: AdminUserSummary) {
  deleteBlockers.value = "";
  await run(async () => {
    await api.delete(`/api/admin/users/${user.id}`);
    users.value = users.value.filter(item => item.id !== user.id);
    if (expanded.value === user.id) expanded.value = null;
  }, {
    confirm: { title: t("common.delete"), message: t("confirm.delete", { name: user.username }) },
    onError: cause => { if (cause instanceof ApiError && cause.status === 409) deleteBlockers.value = extractBlockers(cause); },
  });
}
async function createInvite() {
  if (inviteBusy.value) return;
  inviteError.value = "";
  const maxUses = Math.floor(Number(inviteMaxUses.value));
  if (!Number.isFinite(maxUses) || maxUses < 1) { inviteError.value = t("validation.min", { n: 1 }); return; }
  inviteBusy.value = true;
  try {
    const created = await api.post<{ item: InviteCode }>("/api/admin/invite-codes", {
      maxUses, expiresAt: inviteExpiresAt.value ? new Date(inviteExpiresAt.value).toISOString() : null,
    });
    if (created?.item) invites.value = [created.item, ...invites.value];
    inviteExpiresAt.value = "";
  } catch (cause: unknown) { inviteError.value = errorMessage(cause); }
  finally { inviteBusy.value = false; }
}
async function revokeInvite(code: InviteCode) {
  await run(async () => {
    const updated = await api.post<{ item: InviteCode }>(`/api/admin/invite-codes/${code.id}/revoke`);
    const index = invites.value.findIndex(item => item.id === code.id);
    if (index >= 0 && updated?.item) invites.value[index] = updated.item;
  }, { confirm: { title: t("invites.revoke"), message: t("invites.revokeConfirm", { code: code.code }) }, success: t("invites.revoked") });
}
function bindingPositions(binding: AdminUserBinding) { return binding.openPositions ?? []; }
onMounted(() => { void load(); });
onActivated(() => { void load(); });
</script>

<template>
  <div class="space-y-5">
    <PageHeader :title="t('users.title')" :summary="summary"><template #actions><button class="icon-button" :disabled="loading || pending" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading }" /></button></template></PageHeader>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <div v-if="deleteBlockers" class="error-banner space-y-2" role="alert">
      <p class="font-semibold">{{ t("users.deleteBlockedTitle") }}</p>
      <p class="whitespace-pre-line break-words">{{ deleteBlockers }}</p>
    </div>

    <section class="card space-y-4 p-4" :aria-label="t('invites.title')">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="section-title">{{ t("invites.title") }}</h2>
        <form class="flex flex-wrap items-end gap-3" @submit.prevent="createInvite">
          <label class="field-label">{{ t("invites.maxUses") }}<input v-model.number="inviteMaxUses" name="maxUses" class="form-input mt-1 w-24" type="number" min="1" required /></label>
          <label class="field-label">{{ t("invites.expiresAt") }}<input v-model="inviteExpiresAt" name="expiresAt" class="form-input mt-1" type="datetime-local" /></label>
          <button type="submit" class="button-primary" :disabled="inviteBusy || pending"><Plus :size="17" />{{ t("invites.create") }}</button>
        </form>
      </div>
      <p v-if="inviteError" class="error-banner" role="alert">{{ inviteError }}</p>
      <EmptyState v-if="!invites.length && !loading" :title="t('invites.empty')" />
      <div v-else class="divide-y divide-border/50">
        <article v-for="code in invites" :key="code.id" class="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
          <code class="min-w-0 flex-1 break-all font-mono text-sm font-semibold">{{ code.code }}</code>
          <span class="pill" :class="inviteStatus(code) === 'active' ? 'tone-success' : inviteStatus(code) === 'revoked' ? 'tone-danger' : 'tone-warning'">{{ t(inviteStatusKey[inviteStatus(code)] ?? "common.unknown") }}</span>
          <span class="text-sm text-muted">{{ t("invites.usedCount") }} {{ code.usedCount ?? "—" }} / {{ code.maxUses ?? "—" }}</span>
          <span class="text-sm text-muted">{{ code.expiresAt ? formatDateTime(code.expiresAt, { short: true, withSeconds: false }) : t("invites.noExpiry") }}</span>
          <span class="text-sm text-muted">{{ t("invites.createdBy") }} {{ code.createdByUsername ?? t("common.unknown") }}</span>
          <button type="button" class="button-secondary" :disabled="pending || inviteStatus(code) !== 'active'" @click="revokeInvite(code)">{{ t("invites.revoke") }}</button>
        </article>
      </div>
    </section>

    <DataTable :rows="rows" :label="t('users.title')" :row-key="row => row.id" :loading="loading" :error="loadError" :empty="t('users.empty')" selectable :expanded="expanded" @select="selectUser" @retry="load(true)" :columns="[
      { key: 'user', label: t('users.username'), width: '16%', mobileWide: true },
      { key: 'role', label: t('users.role'), width: '10%' },
      { key: 'status', label: t('users.status'), width: '9%' },
      { key: 'accounts', label: t('users.accounts'), numeric: true, width: '7%' },
      { key: 'bindings', label: t('users.bindings'), numeric: true, width: '7%' },
      { key: 'enabled', label: t('users.enabledBindings'), numeric: true, width: '9%' },
      { key: 'positions', label: t('users.openPositions'), numeric: true, width: '8%' },
      { key: 'equity', label: t('users.equity'), numeric: true, width: '11%' },
      { key: 'lastLogin', label: t('users.lastLogin'), width: '12%' },
      { key: 'actions', label: t('common.actions'), width: '11%' },
    ]">
      <template #cell-user="{ row }"><p class="min-w-0 flex-1 font-semibold [overflow-wrap:anywhere]">{{ row.username }}</p><p class="text-xs text-muted">{{ t("users.createdAt") }} {{ formatDateTime(row.createdAt, { short: true, withSeconds: false }) }}</p></template>
      <template #cell-role="{ row }"><span class="pill" :class="row.role === 'admin' ? 'tone-accent' : ''">{{ t(row.role === "admin" ? "users.roleAdmin" : "users.roleUser") }}</span></template>
      <template #cell-status="{ row }"><span :class="row.isActive === true ? 'text-success' : 'text-warning'">{{ t(row.isActive === true ? "users.active" : "users.inactive") }}</span></template>
      <template #cell-accounts="{ row }"><strong>{{ row.accountCount ?? "—" }}</strong></template>
      <template #cell-bindings="{ row }"><strong>{{ row.bindingCount ?? "—" }}</strong></template>
      <template #cell-enabled="{ row }"><strong>{{ row.enabledBindingCount ?? "—" }}</strong></template>
      <template #cell-positions="{ row }"><strong>{{ row.openPositionCount ?? "—" }}</strong></template>
      <template #cell-equity="{ row }"><strong>{{ formatUsd(row.totalEquity) }}</strong></template>
      <template #cell-lastLogin="{ row }"><span class="text-sm text-muted">{{ formatDateTime(row.lastLoginAt, { short: true, withSeconds: false }) }}</span></template>
      <template #cell-actions="{ row }">
        <div class="flex flex-wrap items-center justify-end gap-1">
          <button type="button" class="button-secondary" :disabled="pending" @click="toggleActive(row)">{{ t(row.isActive === true ? "common.disable" : "common.enable") }}</button>
          <button type="button" class="button-secondary" :disabled="pending" @click="changeRole(row)">{{ t(row.role === "admin" ? "users.makeUser" : "users.makeAdmin") }}</button>
          <button type="button" class="icon-button" :disabled="pending" :aria-label="t('users.resetPassword')" :title="t('users.resetPassword')" @click="openReset(row)"><KeyRound :size="17" /></button>
          <button type="button" class="icon-button text-danger" :disabled="pending" :aria-label="t('common.delete')" :title="t('common.delete')" @click="deleteUser(row)"><Trash2 :size="17" /></button>
        </div>
      </template>
      <template #select-label>{{ t("common.details") }}</template>
      <template #details="{ row }">
        <div class="space-y-4">
          <p v-if="detailError[row.id]" class="error-banner" role="alert">{{ detailError[row.id] }}</p>
          <p v-else-if="detailLoading[row.id]" class="text-sm text-muted" role="status">{{ t("common.loading") }}</p>
          <template v-else-if="details[row.id]">
            <section>
              <h3 class="section-title">{{ t("users.detailAccounts") }}</h3>
              <EmptyState v-if="!details[row.id].accounts?.length" :title="t('users.noAccounts')" />
              <div v-else class="divide-y divide-border/50">
                <article v-for="account in details[row.id].accounts" :key="account.id" class="flex flex-wrap items-center gap-x-4 gap-y-1 py-2 text-sm">
                  <span class="font-semibold">{{ account.name ?? t("common.unknown") }}</span>
                  <span class="text-muted">{{ account.exchange?.toUpperCase() ?? t("common.unknown") }} · {{ t(account.environment === "live" ? "env.live" : account.environment === "demo" ? "env.demo" : "common.unknown") }}</span>
                  <span :class="account.isEnabled === true ? 'text-success' : 'text-warning'">{{ t(account.isEnabled === true ? "common.enabled" : "common.disabled") }}</span>
                  <span class="text-muted">{{ account.status ?? t("common.unknown") }} · {{ formatUsd(account.equity) }}</span>
                </article>
              </div>
            </section>
            <section>
              <h3 class="section-title">{{ t("users.detailBindings") }}</h3>
              <EmptyState v-if="!details[row.id].bindings?.length" :title="t('users.noBindings')" />
              <div v-else class="divide-y divide-border/50">
                <article v-for="binding in details[row.id].bindings" :key="binding.id" class="py-2 text-sm">
                  <p><span class="font-semibold">{{ binding.name ?? t("common.unknown") }}</span>
                    <span class="text-muted"> · {{ binding.leadName ?? t("common.unknown") }} → {{ binding.accountName ?? t("common.unknown") }}</span>
                    <span :class="binding.isEnabled === true ? 'text-success' : 'text-warning'"> · {{ t(binding.isEnabled === true ? "common.enabled" : "common.disabled") }}</span></p>
                  <p v-if="bindingPositions(binding).length" class="mt-1 text-muted">
                    <span v-for="position in bindingPositions(binding)" :key="`${position.instId}:${position.posSide}`" class="mr-3">{{ position.instId ?? t("common.unknown") }} {{ t(position.posSide === "SHORT" ? "side.SHORT" : position.posSide === "LONG" ? "side.LONG" : "side.UNKNOWN") }} {{ formatQty(position.qty) }}</span>
                  </p>
                  <p v-else class="mt-1 text-xs text-muted">{{ t("users.noOpenPositions") }}</p>
                </article>
              </div>
            </section>
            <section>
              <h3 class="section-title">{{ t("users.detailSubscriptions") }}</h3>
              <EmptyState v-if="!details[row.id].subscriptions?.length" :title="t('users.noSubscriptions')" />
              <div v-else class="flex flex-wrap gap-2">
                <span v-for="sub in details[row.id].subscriptions" :key="sub.leadId" class="pill" :class="sub.isEnabled ? 'tone-success' : ''">{{ sub.leadName ?? t("common.unknown") }} · {{ t(sub.isEnabled ? "users.subEnabled" : "users.subDisabled") }}</span>
              </div>
            </section>
          </template>
        </div>
      </template>
    </DataTable>

    <BaseModal :open="resetTarget != null" :title="resetTarget ? t('users.resetPassword') : ''" @close="resetTarget = null">
      <form v-if="resetTarget" class="space-y-4" @submit.prevent="submitReset">
        <p class="text-sm text-muted">{{ resetTarget.username }}</p>
        <div>
          <label for="reset-password" class="field-label mb-1.5">{{ t("users.newPassword") }} <span class="text-danger">*</span></label>
          <input id="reset-password" v-model="newPassword" class="form-input" type="password" required minlength="8" maxlength="128" autocomplete="new-password" :placeholder="t('users.newPasswordPlaceholder')" />
        </div>
        <p v-if="resetError" class="error-banner" role="alert">{{ resetError }}</p>
        <div class="flex justify-end gap-2">
          <button type="button" class="button-secondary" :disabled="resetBusy" @click="resetTarget = null">{{ t("common.cancel") }}</button>
          <button type="submit" class="button-primary" :disabled="resetBusy">{{ t(resetBusy ? "common.working" : "users.resetPassword") }}</button>
        </div>
      </form>
    </BaseModal>
  </div>
</template>
