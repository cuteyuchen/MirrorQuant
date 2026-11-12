<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { Cpu, KeyRound, Lock, RefreshCw, ShieldAlert, ShieldCheck, ArrowRight } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useI18n } from "@/i18n";
import LangToggle from "@/components/LangToggle.vue";

const router = useRouter();
const authStore = useAuthStore();
const { t } = useI18n();

const username = ref("");
const password = ref("");
const captchaAnswer = ref("");
const totpCode = ref("");

let countdownTimer: number | null = null;

function stopTimer() {
  if (countdownTimer !== null) {
    window.clearInterval(countdownTimer);
    countdownTimer = null;
  }
}

watch(
  () => authStore.lockRemainingSec,
  (val) => {
    stopTimer();
    if (val > 0) {
      countdownTimer = window.setInterval(() => {
        if (authStore.lockRemainingSec > 0) {
          authStore.lockRemainingSec -= 1;
        } else {
          stopTimer();
        }
      }, 1000);
    }
  }
);

onUnmounted(() => stopTimer());

function formatSec(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function refreshCaptcha() {
  captchaAnswer.value = "";
  await authStore.fetchCaptcha();
}

async function handleSubmit() {
  if (!username.value || !password.value || authStore.lockRemainingSec > 0) return;
  const ok = await authStore.login(
    username.value,
    password.value,
    authStore.captchaData?.captchaToken,
    captchaAnswer.value,
    totpCode.value
  );
  if (ok) {
    router.push("/dashboard");
  } else {
    captchaAnswer.value = "";
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center p-4 bg-background relative overflow-hidden">
    <div class="absolute top-1/4 left-1/2 -translate-x-1/2 w-[500px] h-[500px] bg-accent/10 rounded-full blur-[120px] pointer-events-none"></div>

    <div class="w-full max-w-md bg-surface/90 backdrop-blur-xl border border-slate-800 rounded-2xl p-6 sm:p-8 shadow-2xl relative z-10">
      <div class="flex justify-end mb-2">
        <LangToggle />
      </div>

      <div class="flex flex-col items-center text-center mb-8">
        <div class="w-12 h-12 rounded-2xl bg-gradient-to-br from-accent/20 to-brand/20 border border-accent/30 flex items-center justify-center mb-4 shadow-lg shadow-accent/10">
          <Cpu class="w-6 h-6 text-accent" />
        </div>
        <h1 class="text-2xl font-bold tracking-tight text-white">MirrorQuant</h1>
        <p class="text-sm text-slate-400 mt-1">{{ t("login.subtitle") }}</p>
      </div>

      <form @submit.prevent="handleSubmit" class="space-y-4">
        <div
          v-if="authStore.lockRemainingSec > 0"
          class="p-3 rounded-xl bg-amber-500/15 border border-amber-500/40 text-amber-300 text-xs font-medium flex items-center gap-2"
        >
          <ShieldAlert class="w-4 h-4 shrink-0" />
          <span>{{ t("login.lockedNotice", { time: formatSec(authStore.lockRemainingSec) }) }}</span>
        </div>

        <div v-if="authStore.error" class="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-medium">
          {{ authStore.error }}
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">{{ t("login.username") }}</label>
          <input
            v-model="username"
            type="text"
            required
            autocomplete="username"
            placeholder="admin"
            class="w-full px-4 py-2.5 rounded-xl bg-background border border-slate-700 text-slate-100 placeholder-slate-500 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-all text-sm"
          />
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-1.5">{{ t("login.password") }}</label>
          <input
            v-model="password"
            type="password"
            required
            autocomplete="current-password"
            placeholder="••••••••••••"
            class="w-full px-4 py-2.5 rounded-xl bg-background border border-slate-700 text-slate-100 placeholder-slate-500 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent transition-all text-sm"
          />
        </div>

        <div v-if="authStore.captchaRequired && authStore.captchaData" class="space-y-2 p-3 rounded-xl bg-background/70 border border-slate-700">
          <div class="flex items-center justify-between gap-2">
            <span class="text-[11px] font-medium text-slate-300">{{ t("login.captchaLabel") }}</span>
            <button
              type="button"
              @click="refreshCaptcha"
              class="text-[11px] text-accent hover:underline flex items-center gap-1"
            >
              <RefreshCw class="w-3 h-3" />
              <span>{{ t("login.refreshCaptcha") }}</span>
            </button>
          </div>
          <div class="flex items-center gap-2.5">
            <div
              class="rounded-lg overflow-hidden border border-slate-700 shrink-0 bg-[#0b1220]"
              v-html="authStore.captchaData.svg"
            ></div>
            <input
              v-model="captchaAnswer"
              type="text"
              required
              :placeholder="t('login.captchaPlaceholder')"
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:border-accent"
            />
          </div>
        </div>

        <div v-if="authStore.totpRequired" class="p-3 rounded-xl bg-background/70 border border-accent/40 space-y-1.5">
          <label class="flex items-center gap-1.5 text-xs font-medium text-accent">
            <KeyRound class="w-3.5 h-3.5" />
            <span>{{ t("login.totpLabel") }}</span>
          </label>
          <input
            v-model="totpCode"
            type="text"
            inputmode="numeric"
            maxlength="6"
            required
            :placeholder="t('login.totpPlaceholder')"
            class="w-full px-3.5 py-2 rounded-lg bg-surface border border-slate-700 text-slate-100 font-mono tracking-widest text-sm focus:outline-none focus:border-accent"
          />
        </div>

        <button
          type="submit"
          :disabled="authStore.loading || authStore.lockRemainingSec > 0"
          class="w-full py-2.5 px-4 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-sm flex items-center justify-center gap-2 hover:opacity-90 disabled:opacity-50 transition-all shadow-lg shadow-accent/20"
        >
          <Lock class="w-4 h-4" />
          <span>{{ authStore.loading ? t("login.signingIn") : t("login.signIn") }}</span>
          <ArrowRight class="w-4 h-4" />
        </button>
      </form>

      <div class="mt-6 pt-5 border-t border-slate-800/80 flex items-center justify-between text-xs text-slate-500">
        <span class="inline-flex items-center gap-1.5">
          <ShieldCheck class="w-3.5 h-3.5 text-emerald-400" />
          {{ t("login.securityFoot") }}
        </span>
        <router-link to="/public" class="text-accent hover:underline">{{ t("login.publicLink") }}</router-link>
      </div>
    </div>
  </div>
</template>
