/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Plain Vite + React + Tailwind setup. No visual design decisions live here on purpose
// (see frontend/README.md): a design skill picks palette/fonts/components in a later wave.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // The browser only ever talks to this dev server, so no CORS is needed and the bearer
      // token never crosses origins. 127.0.0.1, not localhost: `make api` (uvicorn) binds
      // IPv4 only, and localhost can resolve to ::1 first.
      "/api": {
        // WARDEN_API_PORT, the same variable `make api` reads: port 8000 is often taken on a
        // dev machine by another project's container, and both ends have to move together.
        target: `http://127.0.0.1:${process.env.WARDEN_API_PORT ?? "8000"}`,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});
