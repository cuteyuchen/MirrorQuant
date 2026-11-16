<script setup lang="ts">
import { AlertTriangle } from "lucide-vue-next";
import { useI18n } from "@/i18n";
import type { BoardAccount } from "@/types/api";
import { formatPct, formatUsd, signedUsd } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import EnvBadge from "@/components/EnvBadge.vue";
import StatusBadge from "@/components/StatusBadge.vue";
defineProps<{ accounts: BoardAccount[] | null }>();
const { t } = useI18n();
// 每个跟单账户一张紧凑卡片：权益、今日盈亏、可用余额与未实现盈亏。
// 未知资金仍显示破折号，不用零顶替。
</script>
<template>
  <section :aria-label="t('nav.accounts')" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
    <div v-if="accounts == null || !accounts.length" class="card col-span-full px-4 py-6 text-center text-sm text-muted">{{ accounts == null ? t("common.unknown") : t("board.noAccounts") }}</div>
    <!-- 卡片内固定两列且均可收缩，长名称和大额数字不能撑开卡片。 -->
    <div v-for="account in accounts || []" :key="account.id || account.name || 'account'" class="card grid min-w-0 grid-cols-2 gap-x-4 gap-y-3 p-3.5">
      <div class="col-span-2 min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="min-w-0 flex-1 font-semibold [overflow-wrap:anywhere]">{{ account.name ?? t("common.unknown") }}</span>
          <EnvBadge :environment="account.environment" />
        </div>
        <div class="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-muted">
          <span class="font-medium uppercase tracking-wide">{{ account.exchange ?? t("common.unknown") }}</span>
          <StatusBadge :state="account.status" />
        </div>
        <p v-if="account.riskLegacyUnverified" class="mt-1.5 flex items-center gap-1 text-xs font-medium text-warning"><AlertTriangle class="shrink-0" :size="13" aria-hidden="true" />{{ t("board.riskLegacy") }}</p>
      </div>
      <dl class="number min-w-0 text-left">
        <dt class="font-sans text-xs text-muted">{{ t("board.equity") }}</dt>
        <dd class="mt-0.5 text-base font-bold">{{ formatUsd(account.equity) }}</dd>
      </dl>
      <dl class="number min-w-0">
        <dt class="font-sans text-xs text-muted">{{ t("board.todayPnl") }}</dt>
        <dd class="mt-0.5 text-base font-bold" :class="pnlClass(account.todayPnl)">{{ signedUsd(account.todayPnl) }} <span class="text-xs font-semibold">{{ formatPct(account.todayPnlPct) }}</span></dd>
      </dl>
      <dl class="number min-w-0 text-left">
        <dt class="font-sans text-xs text-muted">{{ t("board.available") }}</dt>
        <dd class="mt-0.5 text-sm font-semibold">{{ formatUsd(account.available) }}</dd>
      </dl>
      <dl class="number min-w-0">
        <dt class="font-sans text-xs text-muted">{{ t("board.unrealized") }}</dt>
        <dd class="mt-0.5 text-sm font-semibold" :class="pnlClass(account.unrealizedPnl)">{{ signedUsd(account.unrealizedPnl) }}</dd>
      </dl>
    </div>
  </section>
</template>
