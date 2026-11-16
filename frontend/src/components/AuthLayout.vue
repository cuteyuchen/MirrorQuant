<script setup lang="ts">
import { useId } from "vue";
import { Activity } from "lucide-vue-next";
import { useI18n } from "@/i18n";
import LangToggle from "@/components/LangToggle.vue";
import ThemeToggle from "@/components/ThemeToggle.vue";

// 认证与连接错误共用同一套布局，长表单在窄屏只向下延伸。
// 根元素不再铺纯色背景，页面级柔光可以透出来；登录前也能切换主题与语言。
defineProps<{ title: string; description?: string; badge?: string }>();
const { t } = useI18n();
const titleId = useId();
</script>

<template>
  <div class="grid min-h-dvh min-w-0 place-items-center px-4 py-8">
    <div class="w-full min-w-0 max-w-md">
      <header class="mb-5 flex items-center justify-between gap-3">
        <RouterLink to="/dashboard" class="flex min-w-0 items-center gap-3">
          <span class="brand-mark"><Activity :size="19" aria-hidden="true" /></span>
          <span class="min-w-0">
            <span class="block text-lg font-bold leading-tight tracking-tight">{{ t("app.name") }}</span>
            <span class="block text-xs text-faint">{{ t("app.brandSub") }}</span>
          </span>
        </RouterLink>
        <div class="flex shrink-0 items-center gap-1"><ThemeToggle /><LangToggle /></div>
      </header>
      <section :aria-labelledby="titleId" class="card card-accent p-5 sm:p-7">
        <header class="mb-6 space-y-2">
          <div class="flex flex-wrap items-center gap-2">
            <h1 :id="titleId" class="text-xl font-bold tracking-tight">{{ title }}</h1>
            <span v-if="badge" class="pill tone-warning font-semibold">{{ badge }}</span>
          </div>
          <p v-if="description" class="text-sm leading-relaxed text-muted">{{ description }}</p>
        </header>
        <slot />
        <footer v-if="$slots.footer" class="mt-6 border-t border-border/50 pt-4 text-xs text-muted">
          <slot name="footer" />
        </footer>
      </section>
    </div>
  </div>
</template>
