import { defineConfig } from 'vite'
import pages from '@hono/vite-cloudflare-pages'

export default defineConfig({
  plugins: [pages()],
  build: {
    outDir: 'dist',
    ssr: true,
    ssrEmitAssets: true,
    rollupOptions: {
      input: './src/index.tsx'
    }
  },
  ssr: {
    noExternal: true,
    target: 'webworker'
  }
})
