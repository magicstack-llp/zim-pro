import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const backend = process.env.KIWIX_BACKEND ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: Object.fromEntries(
      ["/api", "/content", "/markdown", "/random", "/mcp"].map((path) => [path, { target: backend }]),
    ),
  },
});
