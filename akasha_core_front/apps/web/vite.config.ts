import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

/**
 * Build/ dev server for the workbench.
 *
 * base=/app/ (docs/08): the build output is served by FastAPI StaticFiles or a
 * same-origin reverse proxy; /v1/* and missing assets must stay JSON 404s and
 * never be swallowed by the SPA fallback (the server decides that, not Vite).
 */
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5273,
    strictPort: true,
    // Dev-only proxy to the SAME trusted backend; production enables no CORS.
    proxy: {
      '/v1': { target: process.env.PAPERINTEL_API_BASE ?? 'http://127.0.0.1:8420', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
});
