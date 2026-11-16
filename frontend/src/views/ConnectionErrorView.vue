<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { RefreshCw, WifiOff } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { MOCK_MODE } from "@/api/mode";
import { useI18n } from "@/i18n";
import { safeRedirect } from "@/router/guard";
import AuthLayout from "@/components/AuthLayout.vue";
const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const retrying = ref(false);
const previewMode = computed(() => MOCK_MODE && route.query.preview === "1");
const message = computed(() => auth.error || (previewMode.value ? t("errors.network") : ""));
// 重试仍失败就留在本页，不把未知状态解释成首次安装。
async function retry() {
  if (retrying.value || auth.loading) return;
  retrying.value = true;
  try {
    await auth.checkStatus();
    if (auth.initialized !== null) await router.replace(safeRedirect(route.query.redirect));
  } finally { retrying.value = false; }
}
</script>
<template>
  <AuthLayout :title="t('connection.title')" :description="t('connection.description')" :badge="previewMode ? t('board.mock') : undefined">
    <div class="space-y-5">
      <div class="warning-banner flex items-start gap-3"><WifiOff :size="24" class="shrink-0" /><p>{{ t("auth.connectionPending") }}</p></div>
      <p v-if="message" class="error-banner" role="alert">{{ message }}</p>
      <button class="button-primary w-full" :disabled="auth.loading || retrying" @click="retry">
        <RefreshCw :size="18" :class="{ 'animate-spin': auth.loading || retrying }" />{{ t(retrying ? "auth.reconnecting" : "common.retry") }}
      </button>
    </div>
  </AuthLayout>
</template>
