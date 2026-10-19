import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';
import { prepaintScript } from './src/theme/prepaint.ts';

/** Replaces the `<!-- prepaint -->` marker in index.html with the inline theme script. */
function prepaint(): Plugin {
  return {
    name: 'novelhog-prepaint',
    transformIndexHtml: (html) =>
      html.replace('<!-- prepaint -->', `<script>${prepaintScript}</script>`),
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), prepaint()],
  server: { proxy: { '/api': 'http://localhost:8000', '/static': 'http://localhost:8000' } },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
});
