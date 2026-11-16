<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Eye, EyeOff, KeyRound, Lock, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { MOCK_MODE } from "@/api/mode";
import { useI18n } from "@/i18n";
import { useNow } from "@/composables/useNow";
import { safeRedirect } from "@/router/guard";
import { reasonText } from "@/utils/errors";
import type { CaptchaChallenge } from "@/types/api";
import AuthLayout from "@/components/AuthLayout.vue";

const router = useRouter();
const route = useRoute();
const authStore = useAuthStore();
const { t } = useI18n();
const previewMode = computed(() => MOCK_MODE && route.query.preview === "1");
const previewCaptcha = ref<CaptchaChallenge | null>(null);
const previewError = ref("");
const username = ref("");
const password = ref("");
const showPassword = ref(false);
const captchaAnswer = ref("");
const totpCode = ref("");
const errors = ref<Record<string, string>>({});
const localError = ref("");
const captchaLoading = ref(false);
const now = useNow();
const lockExpiresAt = ref<number | null>(null);
const captchaRequired = computed(() => authStore.captchaRequired || previewCaptcha.value != null);
const totpRequired = computed(() => authStore.totpRequired || previewCaptcha.value != null);
const captchaData = computed(() => previewCaptcha.value ?? authStore.captchaData);
const displayedError = computed(() => localError.value || previewError.value || authStore.error);

// 锁定记录绝对截止时间，标签页计时停顿也不会延长锁定。
watch(() => authStore.lockRemainingSec, seconds => {
  if (!previewMode.value) lockExpiresAt.value = seconds > 0 ? Date.now() + seconds * 1000 : null;
}, { immediate: true });
const lockRemaining = computed(() => lockExpiresAt.value == null ? 0 : Math.max(0, Math.ceil((lockExpiresAt.value - now.value) / 1000)));
watch(lockRemaining, seconds => {
  if (!previewMode.value && seconds === 0 && authStore.lockRemainingSec > 0) authStore.lockRemainingSec = 0;
});
// 验证码只能由图片解码，绝不把后端 SVG 作为 HTML 执行。
const captchaSrc = computed(() => captchaData.value?.svg
  ? `${["data", "image/svg+xml;charset=utf-8,"].join(":")}${encodeURIComponent(captchaData.value.svg)}` : "");

onMounted(async () => {
  if (!previewMode.value) return;
  // 截图预览使用局部数据，不改变全局认证状态或发送写请求。
  const { mockAuthPreview } = await import("@/mocks/board");
  username.value = mockAuthPreview.username;
  previewCaptcha.value = mockAuthPreview.captcha;
  previewError.value = reasonText(mockAuthPreview.errorCode);
  lockExpiresAt.value = Date.now() + mockAuthPreview.lockRemainingSec * 1000;
});

function formatSec(sec: number) {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}
async function refreshCaptcha() {
  if (captchaLoading.value || authStore.loading) return;
  captchaLoading.value = true;
  captchaAnswer.value = "";
  try {
    if (previewMode.value) {
      const { mockAuthPreview } = await import("@/mocks/board");
      previewCaptcha.value = { ...mockAuthPreview.captcha };
    } else await authStore.fetchCaptcha();
  } finally { captchaLoading.value = false; }
}
async function handleSubmit() {
  if (authStore.loading || lockRemaining.value > 0 || captchaLoading.value) return;
  errors.value = {};
  localError.value = "";
  if (!username.value.trim()) errors.value.username = t("validation.required");
  if (!password.value) errors.value.password = t("validation.required");
  if (captchaRequired.value && !captchaAnswer.value.trim()) errors.value.captcha = t("validation.required");
  if (totpRequired.value && !/^\d{6}$/.test(totpCode.value)) errors.value.totp = t("auth.codeLength");
  const firstError = Object.keys(errors.value)[0];
  if (firstError) {
    localError.value = t("auth.correctFields");
    await nextTick();
    document.getElementById(`login-${firstError}`)?.focus();
    return;
  }
  if (previewMode.value) { localError.value = t("errors.mockReadonly"); return; }
  const ok = await authStore.login(username.value.trim(), password.value, captchaData.value?.captchaToken, captchaAnswer.value, totpCode.value);
  if (ok) await router.replace(safeRedirect(route.query.redirect));
  else captchaAnswer.value = "";
}
</script>

<template>
  <AuthLayout :title="t('login.signIn')" :description="t('login.subtitle')" :badge="previewMode ? t('board.mock') : undefined">
    <form class="space-y-4" novalidate :aria-busy="authStore.loading" @submit.prevent="handleSubmit">
      <div v-if="lockRemaining > 0" class="warning-banner flex items-start gap-2" role="status">
        <ShieldAlert :size="18" class="mt-0.5 shrink-0" />
        <span class="tabular-nums">{{ t("login.lockedNotice", { time: formatSec(lockRemaining) }) }}</span>
      </div>
      <div v-if="displayedError" class="error-banner" role="alert">{{ displayedError }}</div>
      <div>
        <label for="login-username" class="field-label mb-1.5">{{ t("login.username") }} <span class="text-danger">*</span></label>
        <input id="login-username" v-model="username" class="form-input" type="text" required maxlength="64" autocomplete="username"
          :placeholder="t('auth.usernamePlaceholder')" :aria-invalid="!!errors.username" :aria-describedby="errors.username ? 'login-username-error' : undefined" />
        <p v-if="errors.username" id="login-username-error" class="field-error">{{ errors.username }}</p>
      </div>
      <div>
        <label for="login-password" class="field-label mb-1.5">{{ t("login.password") }} <span class="text-danger">*</span></label>
        <div class="relative">
          <input id="login-password" v-model="password" class="form-input pr-12" :type="showPassword ? 'text' : 'password'" required maxlength="128"
            autocomplete="current-password" :aria-invalid="!!errors.password" :aria-describedby="errors.password ? 'login-password-error' : undefined" />
          <button type="button" class="icon-button absolute right-0 top-0" :aria-label="t(showPassword ? 'auth.hidePassword' : 'auth.showPassword')"
            :title="t(showPassword ? 'auth.hidePassword' : 'auth.showPassword')" @click="showPassword = !showPassword">
            <EyeOff v-if="showPassword" :size="18" /><Eye v-else :size="18" />
          </button>
        </div>
        <p v-if="errors.password" id="login-password-error" class="field-error">{{ errors.password }}</p>
      </div>
      <fieldset v-if="captchaRequired" class="min-w-0 space-y-2 border-t border-border/50 pt-4">
        <legend class="field-label pr-2">{{ t("login.captchaLabel") }}</legend>
        <div class="flex min-w-0 items-center justify-between gap-3">
          <img v-if="captchaSrc" :src="captchaSrc" :alt="t('login.captchaLabel')" class="h-12 w-40 max-w-full rounded border border-border bg-surface" />
          <p v-else class="text-xs text-warning">{{ t(captchaLoading ? "common.loading" : "auth.captchaUnavailable") }}</p>
          <button type="button" class="icon-button shrink-0" :disabled="captchaLoading || authStore.loading"
            :aria-label="t('login.refreshCaptcha')" :title="t('login.refreshCaptcha')" @click="refreshCaptcha">
            <RefreshCw :size="18" :class="{ 'animate-spin': captchaLoading }" />
          </button>
        </div>
        <label for="login-captcha" class="sr-only">{{ t("login.captchaLabel") }}</label>
        <input id="login-captcha" v-model="captchaAnswer" class="form-input" type="text" required autocomplete="off"
          :aria-label="t('login.captchaLabel')" :placeholder="t('login.captchaPlaceholder')"
          :aria-invalid="!!errors.captcha" :aria-describedby="errors.captcha ? 'login-captcha-error' : undefined" />
        <p v-if="errors.captcha" id="login-captcha-error" class="field-error">{{ errors.captcha }}</p>
      </fieldset>
      <div v-if="totpRequired">
        <label for="login-totp" class="field-label mb-1.5 flex items-center gap-2"><KeyRound :size="16" />{{ t("login.totpLabel") }}</label>
        <input id="login-totp" v-model="totpCode" class="form-input tabular-nums" type="text" inputmode="numeric" maxlength="6" required
          autocomplete="one-time-code" :placeholder="t('login.totpPlaceholder')" :aria-invalid="!!errors.totp"
          :aria-describedby="errors.totp ? 'login-totp-error' : undefined" />
        <p v-if="errors.totp" id="login-totp-error" class="field-error">{{ errors.totp }}</p>
      </div>
      <button type="submit" class="button-primary w-full" :disabled="authStore.loading || lockRemaining > 0 || captchaLoading">
        <RefreshCw v-if="authStore.loading" :size="18" class="animate-spin" /><Lock v-else :size="18" />
        {{ t(authStore.loading ? "login.signingIn" : "login.signIn") }}
      </button>
    </form>
    <template #footer>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span class="inline-flex items-center gap-1.5"><ShieldCheck :size="15" />{{ t("login.securityFoot") }}</span>
        <RouterLink to="/public" class="text-accent hover:underline">{{ t("login.publicLink") }}</RouterLink>
      </div>
    </template>
  </AuthLayout>
</template>
