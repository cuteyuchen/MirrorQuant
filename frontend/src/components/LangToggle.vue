<script setup lang="ts">
import { useI18n } from "../i18n";
import { Languages } from "lucide-vue-next";
// 语言切换使用同一主题和可访问标签，不留下旧调色板或硬编码提示。
const { isZh, toggleLocale, t } = useI18n();
</script>

<template>
  <button
    type="button"
    @click="toggleLocale"
    class="inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium text-muted transition hover:bg-surface2 hover:text-foreground md:min-h-10"
    :title="t(isZh ? 'lang.switchEnglish' : 'lang.switchChinese')"
    :aria-label="t(isZh ? 'lang.switchEnglish' : 'lang.switchChinese')"
    data-testid="lang-toggle"
  >
    <Languages :size="17" aria-hidden="true" />
    <span :class="isZh ? 'font-semibold text-accent' : 'text-muted'">{{ t("lang.zhShort") }}</span>
    <span aria-hidden="true">/</span>
    <span :class="!isZh ? 'font-semibold text-accent' : 'text-muted'">{{ t("lang.enShort") }}</span>
  </button>
</template>
