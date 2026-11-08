import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const backendTarget = env.VITE_API_PROXY_TARGET;
  return {
    plugins: [react()],
    define: {
      global: "globalThis",
    },
    server: backendTarget
      ? {
          proxy: {
            "/api/v1": {
              target: backendTarget,
              changeOrigin: true,
              ws: true,
            },
          },
        }
      : undefined,
    test: {
      environment: "jsdom",
      setupFiles: ["./src/testSetup.ts"],
    },
  };
});
