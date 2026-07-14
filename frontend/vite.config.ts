import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5173,
    host: "0.0.0.0",
    proxy: {
      "/ws": {
        target: "https://localhost:8000",
        ws: true,
        secure: false,
        changeOrigin: true,
      },
      "/api": {
        target: "https://localhost:8000",
        secure: false,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
