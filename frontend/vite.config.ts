import { fileURLToPath, URL } from 'node:url'

import { defineConfig, lazyPlugins } from 'vite-plus'
import vue from '@vitejs/plugin-vue'

// 产物输出到插件静态目录 views/studio，由宿主按目录通配伺服；
// base './' 让 index.html 引用保持相对路径，适配嵌套路径伺服。
export default defineConfig({
  fmt: {},
  lint: {},
  base: './',
  plugins: lazyPlugins(() => [
    vue(),
  ]),
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: fileURLToPath(new URL('../views/studio', import.meta.url)),
    emptyOutDir: true,
  },
})
