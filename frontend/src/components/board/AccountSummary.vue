<script setup lang="ts">
import { computed, inject } from "vue";
import { routerKey } from "vue-router";
import { AlertTriangle } from "lucide-vue-next";
import { useI18n } from "@/i18n";
import type { BoardAccount } from "@/types/api";
import { formatPct, formatUsd, signedUsd } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import EnvBadge from "@/components/EnvBadge.vue";
import StatusBadge from "@/components/StatusBadge.vue";
const props = defineProps<{ accounts: BoardAccount[] | null; currentUserId?: number | null }>();
const { t } = useI18n();
const router = inject(routerKey, null);
function isMine(account: BoardAccount) {
  return props.currentUserId != null && (account.ownerUserId === props.currentUserId || account.ownerUserId == null);
}
function goAccount(id?: number | null) {
  if (id != null && router) void router.push(`/accounts/${id}`);
}
// 桌面端按实际账户数量自适应列数（<=2 占满两列、3 列对齐、>=4 恢复 4 列）；移动端 <=2 纵向展示避免半截裁切，>=3 横向滑动。
const gridClass = computed(() => {
  const count = props.accounts?.length ?? 0;
  if (count <= 2) return "grid grid-cols-1 gap-2.5 sm:grid-cols-2";
  if (count === 3) return "flex overflow-x-auto gap-3 pb-1 snap-x no-scrollbar sm:grid sm:grid-cols-2 lg:grid-cols-3";
  return "flex overflow-x-auto gap-3 pb-1 snap-x no-scrollbar sm:grid sm:grid-cols-2 xl:grid-cols-4";
});
</script>
<template>
  <section :aria-label="t('nav.accounts')" :class="gridClass">
    <div v-if="accounts == null || !accounts.length" class="card w-full sm:col-span-full px-4 py-6 text-center text-sm text-muted">{{ accounts == null ? t("common.unknown") : t("board.noAccounts") }}</div>
    <!-- 长名称和大额数字不撑开卡片；>=3 个账户时移动端以 260px 横向滑动对齐；点击卡片跳转账户持仓页。 -->
    <div v-for="account in accounts || []" :key="account.id || account.name || 'account'"
      class="card grid grid-cols-2 gap-x-4 gap-y-2 p-3 sm:min-w-0 sm:shrink sm:p-3.5"
      :class="[
        (accounts?.length ?? 0) >= 3 ? 'min-w-[260px] shrink-0 snap-start' : '',
        account.id != null ? 'cursor-pointer transition hover:border-accent/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent' : '',
      ]"
      :role="account.id != null ? 'link' : undefined"
      :tabindex="account.id != null ? 0 : undefined"
      :aria-label="account.id != null ? t('board.viewPositions', { name: account.name ?? t('common.unknown') }) : undefined"
      :data-testid="account.id != null ? `account-card-${account.id}` : undefined"
      @click="goAccount(account.id)"
      @keydown.enter.prevent="goAccount(account.id)"
      @keydown.space.prevent="goAccount(account.id)">
      <div class="col-span-2 min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="min-w-0 flex-1 font-semibold [overflow-wrap:anywhere]">{{ account.name ?? t("common.unknown") }}</span>
          <span v-if="isMine(account)" class="pill tone-accent">{{ t("board.myAccountBadge") }}</span>
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
