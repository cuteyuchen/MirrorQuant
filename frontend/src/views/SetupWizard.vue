<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { RefreshCw, ShieldCheck, UserRoundPlus } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { MOCK_MODE } from "@/api/mode";
import { useI18n } from "@/i18n";
import { reasonText } from "@/utils/errors";
import AuthLayout from "@/components/AuthLayout.vue";

const router = useRouter();
const route = useRoute();
const authStore = useAuthStore();
const { t } = useI18n();
const previewMode = computed(() => MOCK_MODE && route.query.preview === "1");
const username = ref("admin");
const password = ref("");
const confirmPassword = ref("");
const bootstrapToken = ref("");
const showPasswords = ref(false);
const errors = ref<Record<string, string>>({});
const localError = ref("");
const previewError = ref("");

onMounted(async () => {
  if (!previewMode.value) return;
  const { mockAuthPreview } = await import("@/mocks/board");
  username.value = mockAuthPreview.username;
  previewError.value = reasonText(mockAuthPreview.errorCode);
});
async function handleSetup() {
  if (authStore.loading) return;
  errors.value = {};
  localError.value = "";
  // 初始化范围与后端 schema 对齐，安装授权令牌不持久化。
  if (username.value.trim().length < 3 || username.value.trim().length > 64) errors.value.username = t("auth.usernameLength");
  if (password.value.length < 8 || password.value.length > 128) errors.value.password = t("auth.passwordLength");
  if (password.value !== confirmPassword.value) errors.value["confirm-password"] = t("setup.errMismatch");
  if (!bootstrapToken.value.trim()) errors.value.token = t("validation.required");
  const firstError = Object.keys(errors.value)[0];
  if (firstError) {
    localError.value = t("auth.correctFields");
    await nextTick();
    document.getElementById(`setup-${firstError}`)?.focus();
    return;
  }
  if (previewMode.value) { localError.value = t("errors.mockReadonly"); return; }
  const ok = await authStore.setup(username.value.trim(), password.value, bootstrapToken.value.trim());
  if (ok) await router.replace("/dashboard");
}
</script>

<template>
  <AuthLayout :title="t('setup.title')" :description="t('setup.subtitle')" :badge="t(previewMode ? 'board.mock' : 'setup.badge')">
    <form class="space-y-5" novalidate :aria-busy="authStore.loading" @submit.prevent="handleSetup">
      <div v-if="localError || previewError || authStore.error" class="error-banner" role="alert">{{ localError || previewError || authStore.error }}</div>
      <fieldset class="min-w-0 space-y-4">
        <legend class="mb-3 text-sm font-semibold">{{ t("auth.adminAccount") }}</legend>
        <div>
          <label for="setup-username" class="field-label mb-1.5">{{ t("setup.username") }} <span class="text-danger">*</span></label>
          <input id="setup-username" v-model="username" class="form-input" type="text" required minlength="3" maxlength="64" autocomplete="username"
            :aria-invalid="!!errors.username" aria-describedby="setup-username-hint setup-username-error" />
          <p id="setup-username-hint" class="mt-1 text-xs text-muted">{{ t("auth.usernameHint") }}</p>
          <p v-if="errors.username" id="setup-username-error" class="field-error">{{ errors.username }}</p>
        </div>
        <div>
          <label for="setup-password" class="field-label mb-1.5">{{ t("setup.password") }} <span class="text-danger">*</span></label>
          <input id="setup-password" v-model="password" class="form-input" :type="showPasswords ? 'text' : 'password'" required minlength="8" maxlength="128"
            autocomplete="new-password" :aria-invalid="!!errors.password" aria-describedby="setup-password-hint setup-password-error" />
          <p id="setup-password-hint" class="mt-1 text-xs text-muted">{{ t("auth.passwordHint") }}</p>
          <p v-if="errors.password" id="setup-password-error" class="field-error">{{ errors.password }}</p>
        </div>
        <div>
          <label for="setup-confirm-password" class="field-label mb-1.5">{{ t("setup.confirmPassword") }} <span class="text-danger">*</span></label>
          <input id="setup-confirm-password" v-model="confirmPassword" class="form-input" :type="showPasswords ? 'text' : 'password'" required maxlength="128"
            autocomplete="new-password" :aria-invalid="!!errors['confirm-password']" :aria-describedby="errors['confirm-password'] ? 'setup-repeat-error' : undefined" />
          <p v-if="errors['confirm-password']" id="setup-repeat-error" class="field-error">{{ errors["confirm-password"] }}</p>
        </div>
        <label class="flex min-h-11 items-center gap-2 text-sm text-muted"><input v-model="showPasswords" type="checkbox" />{{ t("auth.showPassword") }}</label>
      </fieldset>
      <fieldset class="min-w-0 space-y-3 border-t border-border/50 pt-4">
        <legend class="pr-2 text-sm font-semibold">{{ t("auth.installAuthorization") }}</legend>
        <div>
          <label for="setup-token" class="field-label mb-1.5">{{ t("setup.bootstrapToken") }} <span class="text-danger">*</span></label>
          <input id="setup-token" v-model="bootstrapToken" class="form-input" type="password" required autocomplete="off"
            :aria-invalid="!!errors.token" aria-describedby="setup-token-hint setup-token-error" />
          <p id="setup-token-hint" class="mt-1 text-xs leading-relaxed text-muted">{{ t("auth.tokenHint") }}</p>
          <p v-if="errors.token" id="setup-token-error" class="field-error">{{ errors.token }}</p>
        </div>
      </fieldset>
      <button type="submit" class="button-primary w-full" :disabled="authStore.loading">
        <RefreshCw v-if="authStore.loading" :size="18" class="animate-spin" /><UserRoundPlus v-else :size="18" />
        {{ t(authStore.loading ? "setup.initializing" : "setup.submit") }}
      </button>
    </form>
    <template #footer><span class="inline-flex items-start gap-2"><ShieldCheck :size="16" class="shrink-0" />{{ t("setup.footer") }}</span></template>
  </AuthLayout>
</template>
