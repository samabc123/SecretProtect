import axios from "axios";
import { ElMessage, ElLoading } from "element-plus";

let loadingInstance;

const request = axios.create({
  baseURL: "/api",
  timeout: 30000,
  withCredentials: true,
});

request.interceptors.request.use((config) => {
  const method = String(config.method || "get").toLowerCase();
  if (!["get", "head", "options"].includes(method)) {
    config.headers["X-CSRF-Token"] =
      document.querySelector('meta[name="csrf-token"]')?.content || "";
  }
  if (config.showLoading) {
    loadingInstance = ElLoading.service({ fullscreen: true, text: "处理中..." });
  }
  return config;
});

request.interceptors.response.use(
  (response) => {
    loadingInstance?.close();
    return response.data;
  },
  (error) => {
    loadingInstance?.close();
    if (error.response?.status === 401) {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    } else if (error.response?.data?.detail) {
      ElMessage.error(error.response.data.detail);
    } else if (!error.config?.silent) {
      const messages = {
        403: "当前账号无权执行此操作。",
        409: "当前数据状态已变化，请刷新后重试。",
        422: "提交内容未通过校验，请检查输入。",
        429: "操作过于频繁，请稍后重试。",
        503: "服务或数据源暂不可用，请稍后重试。",
      };
      ElMessage.error(messages[error.response?.status] || "请求失败，请稍后重试。");
    }
    return Promise.reject(error);
  },
);

export default request;
