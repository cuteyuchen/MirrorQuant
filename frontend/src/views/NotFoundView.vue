<script setup lang="ts">
import { ArrowLeft, FileQuestion } from "lucide-vue-next";
import { useI18n } from "@/i18n";
import AuthLayout from "@/components/AuthLayout.vue";
// 未知地址保留显式的 404 页面。
const { t } = useI18n();
</script>
<template>
  <AuthLayout :title="t('notFound.title')" :description="t('notFound.description')" :badge="t('auth.notFoundCode')">
    <div class="space-y-5">
      <span class="flex h-14 w-14 items-center justify-center rounded-2xl bg-surface2"><FileQuestion :size="30" class="text-muted" /></span>
      <RouterLink to="/dashboard" class="button-primary w-full"><ArrowLeft :size="18" />{{ t("common.back") }}</RouterLink>
    </div>
  </AuthLayout>
</template>
