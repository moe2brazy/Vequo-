import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path' // <---【第 1 步】必须引入 path
// Gzip 预压缩：构建产物直接生成 .gz，配 Nginx gzip_static 生效。
// 注：该插件包缺 .d.mts，TS 6 nodenext 下把默认导入解析为模块命名空间
// （无 call signature），故用运行时 default 兜底取函数（vite 配置文件类型要求放宽）。
import viteCompressionModule from 'vite-plugin-compression'
const viteCompression = (viteCompressionModule as any)?.default ?? viteCompressionModule

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // Gzip 预压缩（部署层优化）：dist 中 .js/.css/.html/.svg 等生成 .gz 兄弟文件，
    // Nginx 开启 gzip_static 后直接伺服预压缩文件（省 CPU + 传输降 ~70%）。
    // woff2 已自带压缩不再处理；<1KB 小文件跳过（收益可忽略）。
    viteCompression({
      verbose: true,
      threshold: 1024,
      filter: /\.(js|mjs|css|html|svg|json|ttf|otf|eot)$/,
    }),
  ],
  // <---【第 2 步】添加 resolve 配置，告诉 Vite "@" 代表 "src"
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  server: {
    // 同时监听 IPv4 与 IPv6（host:true 双栈）——避免浏览器 localhost 解析到 127.0.0.1 时连不上
    host: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
        // 确保 SSE 流式响应不被代理缓冲，事件逐条实时到达
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['Cache-Control'] = 'no-cache'
            proxyRes.headers['Connection'] = 'keep-alive'
          })
        },
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 按依赖拆分包：框架/图表/可视化独立 chunk，业务页面各自懒加载，
        // 首屏不再下载全部页面 + echarts 单体（原 1.08MB 单 chunk）。
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts') || id.includes('zrender')) return 'echarts'
            // AntV：src/charts/g2plot.ts 对每种图表按需 import 单个 plot 目录，
            // 这些 plot 保持独立 chunk（返回 undefined 交给 Vite 自动切分，命中才下载）；
            // 只把各 plot 共享的 G2 核心与工具库归并，避免每个 chunk 重复打包核心。
            if (id.includes('@antv')) {
              if (id.includes('@antv/g2plot/esm/plots/')) return undefined
              return 'antv-g2-core'
            }
            if (id.includes('vis-network') || id.includes('vis-data')) return 'vis'
            if (id.includes('vue') || id.includes('@vue') || id.includes('vue-router') || id.includes('pinia')) return 'vue'
            return 'vendor'
          }
        },
      },
    },
  },
})