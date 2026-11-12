<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { Cpu, ShieldCheck, Sparkles } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useI18n } from "@/i18n";
import LangToggle from "@/components/LangToggle.vue";

const router = useRouter();
const authStore = useAuthStore();
const { t } = useI18n();

const username = ref("admin");
const password = ref("");
const confirmPassword = ref("");
const localError = ref("");

async function handleSetup() {
  localError.value = "";
  if (password.value.length < 8) {
    localError.value = t("setup.errMinLen");
    return;
  }
  if (password.value !== confirmPassword.value) {
    localError.value = t("setup.errMismatch");
    return;
  }
  const ok = await authStore.setup(username.value, password.value);
  if (ok) {
    router.push("/dashboard");
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center p-4 bg-background relative overflow-hidden">
    <div class="absolute top-1/4 left-1/2 -translate-x-1/2 w-[500px] h-[500px] bg-brand/10 rounded-full blur-[120px] pointer-events-none"></div>

    <div class="w-full max-w-md bg-surface/90 backdrop-blur-xl border border-slate-800 rounded-2xl p-6 sm:p-8 shadow-2xl relative z-10">
      <div class="flex justify-end mb-2">
        <LangToggle />
      </div>

      <div class="flex flex-col items-center text-center mb-7">
        <div class="w-12 h-12 rounded-2xl bg-gradient-to-br from-accent/20 to-brand/20 border border-accent/30 flex items-center justify-center mb-4">
          <Cpu class="w-6 h-6 text-accent" />
        </div>
        <span class="px-2.5 py-0.5 rounded-full bg-accent/10 border border-accent/25 text-accent text-[11px] font-mono mb-2">
          {{ t("setup.badge") }}
        </span>
        <h1 class="text-2xl font-bold text-white">{{ t("setup.title") }}</h1>
        <p class="text-sm text-slate-400 mt-1">{{ t("setup.subtitle") }}</p>
      </div>

      <form @submit.prevent="handleSetup" class="space-y-4">
        <div v-if="localError || authStore.error" class="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-medium">
          {{ localError || authStore.error }}
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">{{ t("setup.username") }}</label>
          <input
            v-model="username"
            type="text"
            required
            minlength="3"
            class="w-full px-4 py-2.5 rounded-xl bg-background border border-slate-700 text-slate-100 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent text-sm"
          />
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">{{ t("setup.password") }}</label>
          <input
            v-model="password"
            type="password"
            required
            minlength="8"
            placeholder="••••••••••••"
            class="w-full px-4 py-2.5 rounded-xl bg-background border border-slate-700 text-slate-100 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent text-sm"
          />
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">{{ t("setup.confirmPassword") }}</label>
          <input
            v-model="confirmPassword"
            type="password"
            required
            placeholder="••••••••••••"
            class="w-full px-4 py-2.5 rounded-xl bg-background border border-slate-700 text-slate-100 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent text-sm"
          />
        </div>

        <button
          type="submit"
          :disabled="authStore.loading"
          class="w-full py-2.5 px-4 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-sm flex items-center justify-center gap-2 hover:opacity-90 disabled:opacity-50 transition-all shadow-lg shadow-accent/20"
        >
          <Sparkles class="w-4 h-4" />
          <span>{{ authStore.loading ? t("setup.initializing") : t("setup.submit") }}</span>
        </button>
      </form>

      <div class="mt-6 pt-4 border-t border-slate-800/80 text-center text-[11px] text-slate-500 flex items-center justify-center gap-1.5">
        <ShieldCheck class="w-3.5 h-3.5 text-emerald-400" />
        {{ t("setup.footer") }}
      </div>
    </div>
  </div>
</template>
