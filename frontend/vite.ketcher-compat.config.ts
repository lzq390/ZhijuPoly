import { defineConfig, mergeConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { ketcherCompatibility } from './build/ketcher-compat.ts';
export default defineConfig(mergeConfig(ketcherCompatibility(), {
  plugins: [react()],
  build: { rollupOptions: { input: 'ketcher-compat.html' } },
}));
