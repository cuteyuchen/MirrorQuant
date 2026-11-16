<script setup lang="ts">
import { AlertTriangle, RefreshCw } from "lucide-vue-next";
import { useI18n } from "@/i18n";
// 重试按钮只发出事件，数据请求仍由页面负责。
defineProps<{ message: string; busy?: boolean }>();
defineEmits<{ retry: [] }>();
const { t } = useI18n();
</script>
<template>
  <div class="error-banner" role="alert">
    <div class="flex min-w-0 flex-1 items-start gap-2"><AlertTriangle class="mt-0.5 shrink-0" :size="18" aria-hidden="true" /><p class="min-w-0 font-medium">{{ message }}</p></div>
    <button class="button-secondary" :disabled="busy" @click="$emit('retry')"><RefreshCw :size="16" :class="{ 'animate-spin': busy }" aria-hidden="true" />{{ t("common.retry") }}</button>
  </div>
</template>
