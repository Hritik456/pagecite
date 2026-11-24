import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server: /api goes to Caddy (which strips the prefix), same as in the built app.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5174,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } },
  },
});
