import fs from 'fs';
import path from 'path';

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    https: {
      key: fs.readFileSync('../certs/key.pem'),
      cert: fs.readFileSync('../certs/cert.pem'),
    },
    host: true,
    port: 5173,
    // HMR WebSocket can't traverse the public reverse proxy at red.c3rber.us
    // (mTLS / no Upgrade support).  Disable HMR entirely; saves trigger a
    // full page reload via the file-watcher below.  Re-enable + configure
    // server.hmr.{host,protocol,clientPort} once a direct browser→Vite path
    // exists (e.g. SSH tunnel).
    hmr: false,
    proxy: {
      '/api': {
        target: process.env.BACKEND_URL ?? 'https://localhost:3000',
        rewrite: (path) => path.replace(/^\/api/, ''),
        secure: false,
        ws: true,
        changeOrigin: true,
        // Clear the idle-read timeout on upgraded WebSocket sockets.  Without
        // this, Node's default socket timeout on the http.Server + http-proxy's
        // failure to reset it on `upgrade` closes an idle WS at ~60 s, which
        // showed up in the operator shell as crisp 60.0 s WSS 1006 drops
        // (session_shell / sliver_shell) even during quiet periods.
        configure: (proxy) => {
          proxy.on('proxyReqWs', (_proxyReq, _req, socket) => {
            socket.setTimeout(0);
          });
          proxy.on('open', (socket) => {
            socket.setTimeout(0);
          });
        },
      },
    },
    watch: {
      usePolling: true,
      interval: 500,
    },
  },
})
