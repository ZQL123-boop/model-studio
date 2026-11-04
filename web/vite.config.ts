import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时把 /api 代理到后端 8600：同源，不用改后端 CORS。
// 生产构建产物交给 FastAPI StaticFiles 托管，也是同源。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",   // 显式绑 IPv4，否则 Vite 只监听 [::1]，127.0.0.1 打不开
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8600",
        changeOrigin: true,
        // SSE 必须关缓冲，否则流式会攒成一坨再吐
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.headers["cache-control"] = "no-cache";
            delete proxyRes.headers["content-encoding"];
          });
        },
      },
    },
  },
});
