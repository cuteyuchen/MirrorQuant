/** @type {import('tailwindcss').Config} */
// 语义色全部指向 style.css 里的 CSS 变量（RGB 三元组），亮/暗主题只需切换变量，
// 同时保留 Tailwind 的透明度修饰符（如 bg-accent/10）。
const token = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;

export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        background: token("bg"),
        surface: token("surface"),
        surface2: token("surface2"),
        surface3: token("surface3"),
        border: token("border"),
        foreground: token("fg"),
        muted: token("muted"),
        faint: token("faint"),
        // accent 为主强调色，brand 只用于渐变与次级点缀。
        accent: token("accent"),
        brand: token("brand"),
        success: token("success"),
        danger: token("danger"),
        dangerSolid: token("danger-solid"),
        warning: token("warning"),
        // 实底强调色上的文字，以及弹层遮罩。
        onAccent: token("on-accent"),
        scrim: token("scrim"),
      },
      boxShadow: {
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
        glow: "0 0 0 1px rgb(var(--c-accent) / 0.25), 0 8px 28px -10px rgb(var(--c-accent) / 0.45)",
      },
      fontFamily: {
        // 不加载外部字体：本机装有 JetBrains Mono 时使用，否则回退到系统等宽字体。
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "PingFang SC", "Microsoft YaHei", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Cascadia Mono", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
