import request from "../utils/request";

export const getFindings = (params) => request.get("/findings", { params });
export const getFinding = (id) => request.get(`/findings/${id}`);
export const reviewFinding = (id, payload) => request.post(`/findings/${id}/review`, payload);
export const batchReview = (payload) => request.post("/findings/batch-review", payload);
export const verifyFinding = (id) => request.post(`/findings/${id}/verify`);
export const batchVerify = (payload) => request.post("/findings/batch-verify", payload);
export const getJobs = (status) => request.get("/verification/jobs", { params: status ? { status } : {} });
export const getHistory = () => request.get("/verification/history");
export const processJobs = () => request.post("/verification/process");
export const testConfiguredToken = (provider) =>
  request.post("/verification/test-configured-token", { provider });
export const exportFindings = () => request.get("/export/findings.json");
