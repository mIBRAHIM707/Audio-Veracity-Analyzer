import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Static build served by the API on the same origin (architecture 2.6, ADR-6): relative
// asset paths, everything bundled, no CDN or runtime download.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: 'dist', emptyOutDir: true, assetsInlineLimit: 0, sourcemap: false },
});
