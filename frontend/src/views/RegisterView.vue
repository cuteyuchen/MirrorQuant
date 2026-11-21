<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import { useRouter } from "vue-router";
import { Eye, EyeOff, KeyRound, UserPlus } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useI18n } from "@/i18n";
import AuthLayout from "@/components/AuthLayout.vue";

defineOptions({ name: "RegisterView" });
const router = useRouter();
const authStore = useAuthStore();
const { t } = useI18n();
const username = ref("");
const password = ref("");
const inviteCode = ref("");
const showPassword = ref(false);
const errors = ref<Record<string, string>>({});
const localError = ref("");
const displayedError = computed(() => localError.value || authStore.error);

async function handleSubmit() {
  if (authStore.loading) return;
  errors.value = {};
  localError.value = "";
  const name = username.value.trim();
  if (name.length < 3 || name.length > 64) errors.value.username = t("auth.usernameLength");
  if (password.value.length < 8 || password.value.length > 128) errors.value.password = t("auth.passwordLength");
  if (!inviteCode.value.trim()) errors.value.inviteCode = t("validation.required");
  const firstError = Object.keys(errors.value)[0];
  if (firstError) {
    localError.value = t("auth.correctFields");
    await nextTick();
    document.getElementById(`register-${firstError}`)?.focus();
    return;
  }
  const ok = await authStore.register(name, password.value, inviteCode.value.trim());
  if (ok) await router.replace("/dashboard");
}
</script>

<template>
  <AuthLayout :title="t('register.title')" :description="t('register.subtitle')">
    <form class="space-y-4" novalidate :aria-busy="authStore.loading" @submit.prevent="handleSubmit">
      <div v-if="displayedError" class="error-banner" role="alert">{{ displayedError }}</div>
      <div>
        <label for="register-username" class="field-label mb-1.5">{{ t("register.username") }} <span class="text-danger">*</span></label>
        <input id="register-username" v-model="username" class="form-input" type="text" required maxlength="64" autocomplete="username"
          :placeholder="t('auth.usernamePlaceholder')" :aria-invalid="!!errors.username" :aria-describedby="errors.username ? 'register-username-error' : undefined" />
        <p v-if="errors.username" id="register-username-error" class="field-error">{{ errors.username }}</p>
        <p v-else class="mt-1 text-xs text-muted">{{ t("auth.usernameHint") }}</p>
      </div>
      <div>
        <label for="register-password" class="field-label mb-1.5">{{ t("register.password") }} <span class="text-danger">*</span></label>
        <div class="relative">
          <input id="register-password" v-model="password" class="form-input pr-12" :type="showPassword ? 'text' : 'password'" required maxlength="128"
            autocomplete="new-password" :aria-invalid="!!errors.password" :aria-describedby="errors.password ? 'register-password-error' : undefined" />
          <button type="button" class="icon-button absolute right-0 top-0" :aria-label="t(showPassword ? 'auth.hidePassword' : 'auth.showPassword')"
            :title="t(showPassword ? 'auth.hidePassword' : 'auth.showPassword')" @click="showPassword = !showPassword">
            <EyeOff v-if="showPassword" :size="18" /><Eye v-else :size="18" />
          </button>
        </div>
        <p v-if="errors.password" id="register-password-error" class="field-error">{{ errors.password }}</p>
        <p v-else class="mt-1 text-xs text-muted">{{ t("auth.passwordHint") }}</p>
      </div>
      <div>
        <label for="register-inviteCode" class="field-label mb-1.5 flex items-center gap-2"><KeyRound :size="16" />{{ t("register.inviteCode") }} <span class="text-danger">*</span></label>
        <input id="register-inviteCode" v-model="inviteCode" class="form-input" type="text" required autocomplete="off"
          :placeholder="t('register.inviteCodePlaceholder')" :aria-invalid="!!errors.inviteCode" :aria-describedby="errors.inviteCode ? 'register-inviteCode-error' : undefined" />
        <p v-if="errors.inviteCode" id="register-inviteCode-error" class="field-error">{{ errors.inviteCode }}</p>
        <p v-else class="mt-1 text-xs text-muted">{{ t("register.inviteCodeHint") }}</p>
      </div>
      <button type="submit" class="button-primary w-full" :disabled="authStore.loading">
        <UserPlus :size="18" />
        {{ t(authStore.loading ? "register.submitting" : "register.submit") }}
      </button>
    </form>
    <template #footer>
      <RouterLink to="/login" class="text-accent hover:underline">{{ t("register.backToLogin") }}</RouterLink>
    </template>
  </AuthLayout>
</template>
