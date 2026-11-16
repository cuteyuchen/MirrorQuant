import { readonly, ref } from "vue";

export type Theme = "light" | "dark";

// 与 index.html 首屏脚本共用同一个存储键：默认深色，用户明确选择后才记住浅色。
const STORAGE_KEY = "mq_theme";
const THEME_COLOR: Record<Theme, string> = { light: "#f0f4f9", dark: "#080c14" };

function readTheme(): Theme {
  try { return localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark"; } catch { return "dark"; }
}

// 主题只通过 <html> 上的 dark 类切换，颜色全部来自 style.css 的 CSS 变量。
function applyTheme(next: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("dark", next === "dark");
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLOR[next]);
}

// 模块级单例：所有切换按钮共享同一份状态，不依赖 Pinia 是否已安装。
const theme = ref<Theme>(readTheme());
applyTheme(theme.value);

function setTheme(next: Theme) {
  theme.value = next;
  applyTheme(next);
  try { localStorage.setItem(STORAGE_KEY, next); } catch { /* 存储不可用时仅本次会话生效 */ }
}

export function useTheme() {
  return {
    theme: readonly(theme),
    setTheme,
    toggleTheme: () => setTheme(theme.value === "dark" ? "light" : "dark"),
  };
}
