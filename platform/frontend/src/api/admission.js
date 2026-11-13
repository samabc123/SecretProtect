import axios from "axios";

export const admissionApi = axios.create({ baseURL: "/api/lab", timeout: 20000, withCredentials: true });

admissionApi.interceptors.request.use((config) => {
  if (!["get", "head", "options"].includes(String(config.method || "get").toLowerCase())) {
    config.headers["X-CSRF-Token"] = document.querySelector('meta[name="csrf-token"]')?.content || "";
  }
  return config;
});

export function setAdmissionCsrf(payload) {
  if (payload.csrf_token) document.querySelector('meta[name="csrf-token"]').content = payload.csrf_token;
}

export function admissionError(error) {
  const detail = error.response?.data?.detail;
  return typeof detail === "string" ? detail : "请求未完成，请检查输入或稍后重试。";
}

export function admissionFieldErrors(error) {
  const fields = error.response?.data?.field_errors;
  return Array.isArray(fields)
    ? Object.fromEntries(fields.filter(item => typeof item?.field === "string" && typeof item?.message === "string").map(item => [item.field, item.message]))
    : {};
}
