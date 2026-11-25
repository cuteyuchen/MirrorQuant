<script setup lang="ts">
import { computed } from "vue";
import { AlertTriangle, LoaderCircle } from "lucide-vue-next";
import BaseModal from "./BaseModal.vue";
import EnvBadge from "./EnvBadge.vue";
import { useUiStore } from "@/stores/ui";
import { useI18n } from "@/i18n";

const ui = useUiStore();
const { t } = useI18n();
const environments = computed(() => [...new Set([
  ...(ui.confirmation?.environments || []),
  ...(ui.confirmation && "environment" in ui.confirmation ? [ui.confirmation.environment ?? null] : []),
])]);
</script>

<template>
  <BaseModal :open="!!ui.confirmation" :level="10" :title="ui.confirmation?.title || t('common.confirm')" :busy="ui.confirming" @close="ui.close">
    <div class="space-y-4">
      <div class="flex items-start gap-3 rounded-xl border border-warning/35 bg-warning/10 p-3.5">
        <AlertTriangle class="mt-0.5 shrink-0 text-warning" :size="20" aria-hidden="true" />
        <p class="min-w-0 whitespace-pre-line break-words text-sm leading-relaxed">{{ ui.confirmation?.message }}</p>
      </div>
      <div v-if="environments.length" class="flex flex-wrap gap-2"><EnvBadge v-for="environment in environments" :key="environment || 'unknown'" :environment="environment" /></div>
      <footer class="flex flex-wrap justify-end gap-2.5 pt-1">
        <button type="button" class="button-secondary flex-1 sm:flex-none" :disabled="ui.confirming" @click="ui.close">{{ t("common.cancel") }}</button>
        <button type="button" class="button-danger flex-1 sm:flex-none" :disabled="ui.confirming" @click="ui.accept">
          <LoaderCircle v-if="ui.confirming" class="animate-spin" :size="16" aria-hidden="true" />
          {{ ui.confirming ? t("common.working") : ui.confirmation?.confirmLabel || t("common.confirm") }}
        </button>
      </footer>
    </div>
  </BaseModal>
</template>
