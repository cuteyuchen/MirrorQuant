<script setup lang="ts">
import { Moon, Sun } from "lucide-vue-next";
import { useI18n } from "@/i18n";
import { useTheme } from "@/composables/useTheme";

// 亮暗主题切换：图标展示“点击后会切到的主题”，状态由 useTheme 单例共享，所有入口保持同步。
const { t } = useI18n();
const { theme, toggleTheme } = useTheme();
</script>

<template>
  <button
    type="button"
    class="icon-button"
    data-testid="theme-toggle"
    :title="t(theme === 'dark' ? 'theme.toLight' : 'theme.toDark')"
    :aria-label="t(theme === 'dark' ? 'theme.toLight' : 'theme.toDark')"
    @click="toggleTheme"
  >
    <Sun v-if="theme === 'dark'" :size="18" aria-hidden="true" />
    <Moon v-else :size="18" aria-hidden="true" />
  </button>
</template>
