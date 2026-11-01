import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The studio talks to the local API through Vite's proxy, so the browser only sees one loopback origin.
const api = `http://127.0.0.1:${process.env.GHOST_ROUTES_API_PORT ?? "8077"}`;

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: api, changeOrigin: false } } },
  preview: { proxy: { "/api": { target: api, changeOrigin: false } } },
});
