import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import { router } from "./router";
import "./style.css";
import { setUnauthorizedHandler } from "./api/client";
import { useAuthStore } from "./stores/auth";
import { useI18n } from "./i18n";

const app = createApp(App);
const pinia = createPinia();

app.use(pinia);
// HTTP 与 WebSocket 共用登出入口，保留失效前的站内目标页面。
setUnauthorizedHandler(() => {
  const auth = useAuthStore(pinia);
  const current = router.currentRoute.value;
  // 登录失败的 401 保留当前验证码挑战；私有会话失效仍统一清理。
  if (auth.authenticated || current.path !== "/login") auth.clearSession();
  auth.initialized = true;
  if (current.path !== "/login") void router.replace({ path: "/login", query: { redirect: current.fullPath } });
});
document.title = useI18n().t("app.title");
app.use(router);
app.mount("#app");
