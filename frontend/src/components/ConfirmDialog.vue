<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { AlertTriangle, LoaderCircle } from "lucide-vue-next";
import BaseModal from "./BaseModal.vue";
import EnvBadge from "./EnvBadge.vue";
import { useUiStore } from "@/stores/ui";
import { useI18n } from "@/i18n";

const ui = useUiStore();
const { t } = useI18n();
const acknowledgement = ref(false);
const typed = ref("");
const environments = computed(() => [...new Set([
  ...(ui.confirmation?.environments || []),
  ...(ui.confirmation && "environment" in ui.confirmation ? [ui.confirmation.environment ?? null] : []),
])]);
// 未知账户环境同样需要强确认，不能把缺失环境当模拟盘。
const strong = computed(() => ui.confirmation?.strong || environments.value.some(environment => environment !== "demo"));
const allowed = computed(() => !strong.value || (acknowledgement.value && typed.value.trim() === "CONFIRM"));
// 每次操作重新确认，不能沿用上一次实盘确认状态。
watch(() => ui.confirmation, () => { acknowledgement.value = false; typed.value = ""; });
</script>

<template>
  <BaseModal :open="!!ui.confirmation" :level="10" :title="ui.confirmation?.title || t('common.confirm')" :busy="ui.confirming" @close="ui.close">
    <div class="space-y-4">
      <div class="flex items-start gap-3 rounded-xl border border-warning/35 bg-warning/10 p-3">
        <AlertTriangle class="mt-0.5 shrink-0 text-warning" :size="20" aria-hidden="true" />
        <p class="min-w-0 whitespace-pre-line break-words">{{ ui.confirmation?.message }}</p>
      </div>
      <div v-if="environments.length" class="flex flex-wrap gap-2"><EnvBadge v-for="environment in environments" :key="environment || 'unknown'" :environment="environment" /></div>
      <!-- 实盘或未知环境的操作：必须勾选知情并手动输入 CONFIRM 才能继续。 -->
      <template v-if="strong">
        <label class="flex items-start gap-3 text-sm"><input v-model="acknowledgement" type="checkbox" class="mt-0.5" :disabled="ui.confirming" />{{ t("confirm.riskAcknowledgement") }}</label>
        <label class="field-label">{{ t("confirm.typeConfirm") }}<input v-model="typed" autocomplete="off" :disabled="ui.confirming" class="form-input mt-2 font-mono" /></label>
      </template>
      <footer class="flex flex-wrap justify-end gap-3">
        <button class="button-secondary" :disabled="ui.confirming" @click="ui.close">{{ t("common.cancel") }}</button>
        <button class="button-danger" :disabled="!allowed || ui.confirming" @click="ui.accept">
          <LoaderCircle v-if="ui.confirming" class="animate-spin" :size="16" aria-hidden="true" />
          {{ ui.confirming ? t("common.working") : ui.confirmation?.confirmLabel || t("common.confirm") }}
        </button>
      </footer>
    </div>
  </BaseModal>
</template>
