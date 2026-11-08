/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// Dev server proxies /api and /health to FastAPI, so the browser talks to one origin
// (http://127.0.0.1:5173) and no CORS is needed. WW_BACKEND_URL is read only here, by the
// dev server; it is NOT exposed to the browser (only VITE_* variables are).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "WW_");
  const backend = env.WW_BACKEND_URL || "http://127.0.0.1:8000";
  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": { target: backend },
        "/health": { target: backend },
      },
    },
    build: { sourcemap: false },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      restoreMocks: true,
      // The App integration tests drive whole workflows with user-event (typing, approvals,
      // streams); each takes 1.5-3.5 s alone and longer when 15 files run in parallel.
      testTimeout: 20_000,
    },
  };
});
