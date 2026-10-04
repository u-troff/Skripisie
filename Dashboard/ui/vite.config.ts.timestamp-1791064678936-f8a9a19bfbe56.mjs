// vite.config.ts
import react from "file:///C:/Users/Utroff/OneDrive/Desktop/Skripsie/Dashboard/ui/node_modules/@vitejs/plugin-react/dist/index.js";
import { defineConfig } from "file:///C:/Users/Utroff/OneDrive/Desktop/Skripsie/Dashboard/ui/node_modules/vite/dist/node/index.js";
var vite_config_default = defineConfig({
  plugins: [react()],
  server: {
    port: 3e3,
    // Proxy to the FastAPI brain so the browser sees one origin and CORS
    // never enters the picture.
    proxy: {
      "/api": {
        // 127.0.0.1, not localhost: uvicorn binds IPv4 only, while Node
        // resolves localhost to ::1 first on macOS.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, "")
      },
      // No rewrite here: the backend routes really are /ws/dialogue and
      // /ws/execution.
      "/ws": {
        target: "http://127.0.0.1:8000",
        ws: true
      },
      "/logs": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true
      }
    }
  }
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcudHMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCJDOlxcXFxVc2Vyc1xcXFxVdHJvZmZcXFxcT25lRHJpdmVcXFxcRGVza3RvcFxcXFxTa3JpcHNpZVxcXFxEYXNoYm9hcmRcXFxcdWlcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfZmlsZW5hbWUgPSBcIkM6XFxcXFVzZXJzXFxcXFV0cm9mZlxcXFxPbmVEcml2ZVxcXFxEZXNrdG9wXFxcXFNrcmlwc2llXFxcXERhc2hib2FyZFxcXFx1aVxcXFx2aXRlLmNvbmZpZy50c1wiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9pbXBvcnRfbWV0YV91cmwgPSBcImZpbGU6Ly8vQzovVXNlcnMvVXRyb2ZmL09uZURyaXZlL0Rlc2t0b3AvU2tyaXBzaWUvRGFzaGJvYXJkL3VpL3ZpdGUuY29uZmlnLnRzXCI7aW1wb3J0IHJlYWN0IGZyb20gJ0B2aXRlanMvcGx1Z2luLXJlYWN0J1xyXG5pbXBvcnQgeyBkZWZpbmVDb25maWcgfSBmcm9tICd2aXRlJ1xyXG5cclxuZXhwb3J0IGRlZmF1bHQgZGVmaW5lQ29uZmlnKHtcclxuICBwbHVnaW5zOiBbcmVhY3QoKV0sXHJcbiAgc2VydmVyOiB7XHJcbiAgICBwb3J0OiAzMDAwLFxyXG4gICAgLy8gUHJveHkgdG8gdGhlIEZhc3RBUEkgYnJhaW4gc28gdGhlIGJyb3dzZXIgc2VlcyBvbmUgb3JpZ2luIGFuZCBDT1JTXHJcbiAgICAvLyBuZXZlciBlbnRlcnMgdGhlIHBpY3R1cmUuXHJcbiAgICBwcm94eToge1xyXG4gICAgICAnL2FwaSc6IHtcclxuICAgICAgICAvLyAxMjcuMC4wLjEsIG5vdCBsb2NhbGhvc3Q6IHV2aWNvcm4gYmluZHMgSVB2NCBvbmx5LCB3aGlsZSBOb2RlXHJcbiAgICAgICAgLy8gcmVzb2x2ZXMgbG9jYWxob3N0IHRvIDo6MSBmaXJzdCBvbiBtYWNPUy5cclxuICAgICAgICB0YXJnZXQ6ICdodHRwOi8vMTI3LjAuMC4xOjgwMDAnLFxyXG4gICAgICAgIGNoYW5nZU9yaWdpbjogdHJ1ZSxcclxuICAgICAgICByZXdyaXRlOiAocGF0aCkgPT4gcGF0aC5yZXBsYWNlKC9eXFwvYXBpLywgJycpLFxyXG4gICAgICB9LFxyXG4gICAgICAvLyBObyByZXdyaXRlIGhlcmU6IHRoZSBiYWNrZW5kIHJvdXRlcyByZWFsbHkgYXJlIC93cy9kaWFsb2d1ZSBhbmRcclxuICAgICAgLy8gL3dzL2V4ZWN1dGlvbi5cclxuICAgICAgJy93cyc6IHtcclxuICAgICAgICB0YXJnZXQ6ICdodHRwOi8vMTI3LjAuMC4xOjgwMDAnLFxyXG4gICAgICAgIHdzOiB0cnVlLFxyXG4gICAgICB9LFxyXG4gICAgICAnL2xvZ3MnOiB7XHJcbiAgICAgICAgdGFyZ2V0OiAnaHR0cDovLzEyNy4wLjAuMTo4MDAwJyxcclxuICAgICAgICBjaGFuZ2VPcmlnaW46IHRydWUsXHJcbiAgICAgIH0sXHJcbiAgICB9LFxyXG4gIH0sXHJcbn0pXHJcbiJdLAogICJtYXBwaW5ncyI6ICI7QUFBb1csT0FBTyxXQUFXO0FBQ3RYLFNBQVMsb0JBQW9CO0FBRTdCLElBQU8sc0JBQVEsYUFBYTtBQUFBLEVBQzFCLFNBQVMsQ0FBQyxNQUFNLENBQUM7QUFBQSxFQUNqQixRQUFRO0FBQUEsSUFDTixNQUFNO0FBQUE7QUFBQTtBQUFBLElBR04sT0FBTztBQUFBLE1BQ0wsUUFBUTtBQUFBO0FBQUE7QUFBQSxRQUdOLFFBQVE7QUFBQSxRQUNSLGNBQWM7QUFBQSxRQUNkLFNBQVMsQ0FBQyxTQUFTLEtBQUssUUFBUSxVQUFVLEVBQUU7QUFBQSxNQUM5QztBQUFBO0FBQUE7QUFBQSxNQUdBLE9BQU87QUFBQSxRQUNMLFFBQVE7QUFBQSxRQUNSLElBQUk7QUFBQSxNQUNOO0FBQUEsTUFDQSxTQUFTO0FBQUEsUUFDUCxRQUFRO0FBQUEsUUFDUixjQUFjO0FBQUEsTUFDaEI7QUFBQSxJQUNGO0FBQUEsRUFDRjtBQUNGLENBQUM7IiwKICAibmFtZXMiOiBbXQp9Cg==
