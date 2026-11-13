import request from "../utils/request";

export const getRules = () => request.get("/rules");
export const toggleRule = (id, enabled) => request.patch(`/rules/${encodeURIComponent(id)}`, { enabled });
export const runExperiments = () => request.post("/extensibility-experiments/run");
export const runExperiment = (experimentKey) => (
  request.post(`/extensibility-experiments/${encodeURIComponent(experimentKey)}/run`)
);
