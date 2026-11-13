import request from "../utils/request";

export const login = (payload) => request.post("/auth/login", payload);
export const logout = () => request.post("/auth/logout");
export const me = () => request.get("/auth/me");
export const changePassword = (payload) => request.post("/auth/password", payload);
export const listAdmins = () => request.get("/auth/admins");
export const createAdmin = (payload) => request.post("/auth/admins", payload);
export const setAdminStatus = (id, is_active) => request.post(`/auth/admins/${id}/status`, { is_active });
