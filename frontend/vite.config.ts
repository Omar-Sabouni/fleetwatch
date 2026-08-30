import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

const BACKEND = "http://127.0.0.1:8000";

const proxy: ProxyOptions = {
  target: BACKEND,
  changeOrigin: true,
  configure(server) {
    server.on("proxyReq", (proxyRequest, request) => {
      try {
        const origin = new URL(request.headers.origin ?? "");
        if (origin.host === request.headers.host) proxyRequest.setHeader("Origin", BACKEND);
      } catch {
        // Requests without a browser Origin header remain unchanged.
      }
    });
  },
};

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  publicDir: false,
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": proxy, "/readyz": proxy },
  },
  build: {
    outDir: fileURLToPath(new URL("../app/static", import.meta.url)),
    emptyOutDir: true,
    assetsDir: "assets",
    target: "es2022",
    cssCodeSplit: false,
  },
});
