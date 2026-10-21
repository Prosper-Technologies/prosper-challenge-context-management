import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The backend is the Pipecat dev runner (bot.py) on :7860. It serves the builder
// API and the SmallWebRTC signalling: /start, then /sessions/{id}/api/offer.
const BACKEND = "http://localhost:7860";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": BACKEND,
      "/start": BACKEND,
      "/sessions": BACKEND,
    },
  },
});
