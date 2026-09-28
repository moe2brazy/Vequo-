/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
  sidebar: "#ffffff",        // 侧栏底色：白
  "sidebar-hover": "#e8f3ff", // 侧栏 hover：浅蓝
  contentbg: "#F5F7FA",       // 内容区底色：浅灰蓝
  primary: "#4D9EFF",         // 主色：柔和浅蓝（用户要求按钮浅一档，2026-09-02）
  "primary-dark": "#2E7CF0",  // 主色 hover/暗档（随主色变浅）
  success: "#4D9EFF",         // 成功态统一浅蓝（全站无绿色残留）
  danger: "#f53f3f",          // 危险红（功能色保留）
  // 品牌蓝阶（2026-09-27）：以 primary #4D9EFF 为 500 档，整体接管 Tailwind 自带 blue。
  // 此前页面里散落的 blue-600/700 与 primary 是两个不相干的蓝（#2563EB vs #4D9EFF），
  // 同页出现像两套系统。接管后所有 blue-* 自动落进品牌色系，换主色只改这里。
  blue: {
    50: "#EEF5FF",
    100: "#E3EFFF",
    200: "#C7E0FF",
    300: "#9CCBFF",
    400: "#74B4FF",
    500: "#4D9EFF",
    600: "#2E7CF0",
    700: "#1D5FC4",
    800: "#12508F",
    900: "#0C3A66",
  },
},
    },
  },
  plugins: [],
}