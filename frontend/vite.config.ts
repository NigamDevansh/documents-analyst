import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API so the browser sees ONE origin in development
// too — the same-origin assumption the deliverable pane depends on is then not
// something that only holds in production. Without it, `fetch("/piles")` from
// :5173 would need CORS the deployed app deliberately does not have.
const api = { target: "http://localhost:8000", changeOrigin: true };

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/piles": api,
      "/documents": api,
      "/decisions": api,
      "/health": api,
    },
  },
  build: { outDir: "dist", sourcemap: false, target: "es2022" },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
