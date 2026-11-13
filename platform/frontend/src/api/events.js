import request from "../utils/request";

export const getEvents = (params) => request.get("/events", { params });
export const getEvent = (id) => request.get(`/events/${encodeURIComponent(id)}`);
export const feedback = (id, payload) => request.post(`/events/${encodeURIComponent(id)}/feedback`, payload);
export const batchFeedback = (payload) => request.post("/events/batch-feedback", payload);
export const createAlert = (id) => request.post(`/events/${encodeURIComponent(id)}/alert`);
export const deliverAlert = (id) => request.post(`/alerts/${encodeURIComponent(id)}/deliver`);
export const exportEvidence = (id) => request.get(`/events/${encodeURIComponent(id)}/evidence-export`, { responseType: "blob" });
export const exportEvents = (ids = []) => request.get("/events/export", { params: ids.length ? { event_ids: ids.join(",") } : {} });
export const getNotifications = () => request.get("/notifications");
export const recordNotification = (id) => request.post(`/notifications/${encodeURIComponent(id)}/record`);
export const retryNotification = (id) => request.post(`/notifications/${encodeURIComponent(id)}/retry`);
