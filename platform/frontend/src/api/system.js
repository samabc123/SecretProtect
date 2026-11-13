import request from "../utils/request";

export const getOverview = () => request.get("/overview/stats");
export const getSystemInfo = () => request.get("/system/info");
export const getReports = () => request.get("/evaluation-report");
export const getChannels = (params) => request.get("/channels", { params });
export const getRuns = (params) => request.get("/backend-runs", { params });
export const getAudit = (limit = 200) => request.get(`/security/audit?limit=${limit}`);
export const runDue = () => request.post("/backend/run-due");
export const exportReport = () => request.get("/reports/export", { responseType: "json" });
