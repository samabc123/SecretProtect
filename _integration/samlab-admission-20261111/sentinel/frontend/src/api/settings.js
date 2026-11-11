import request from "../utils/request";

export const getSettings = () => request.get("/settings");
export const saveSettings = (values) => request.put("/settings", { values });
export const testGithub = (repository) => request.post("/settings/test-github", { repository });
export const testGitee = (repository) => request.post("/settings/test-gitee", { repository });
export const testGiteeToken = () => request.post("/settings/test-gitee-token");
export const getSources = () => request.get("/sources");
export const createSource = (payload) => request.post("/sources", payload);
export const patchSource = (id, payload) => request.patch(`/sources/${id}`, payload);
export const deleteSource = (id) => request.delete(`/sources/${id}`);
export const runSource = (id) => request.post(`/sources/${id}/run`);
export const getTasks = (params) => request.get("/monitoring-tasks", { params });
export const createTask = (payload) => request.post("/monitoring-tasks", payload);
export const updateTask = (id, payload) => request.patch(`/monitoring-tasks/${id}`, payload);
export const taskAction = (id, action) => request.post(`/monitoring-tasks/${id}/${action}`);
export const deleteTask = (id) => request.delete(`/monitoring-tasks/${id}`);
