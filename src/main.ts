import { createApp } from 'vue'
// 主题3 字体（Vite 原生支持 node_modules 裸导入，CSS 内 @import 会被 postcss 误解析为相对路径）
// 性能优化：Rajdhani 仅用于英文/数字装饰，只引 latin 子集（去掉 devanagari/latin-ext）；
// 移除 zcool-qingke-huangyou 中文字体 —— 全项目无任何 CSS 引用该字体，属死依赖（曾全量打包约 5.4MB）。
import '@fontsource/rajdhani/latin-500.css'
import '@fontsource/rajdhani/latin-600.css'
import '@fontsource/rajdhani/latin-700.css'
import './style.css'
import App from './App.vue'
import { patchFetch } from './auth'

// 全局 fetch 包装：自动附带 JWT Authorization 头（认证功能）
patchFetch()

createApp(App).mount('#app')
