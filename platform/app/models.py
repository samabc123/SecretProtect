from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: str
    implementation_status: str
    data: dict[str, Any] | None = None


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EventRecord(ContractModel):
    event_id: str = Field(min_length=1, max_length=80)
    source_kind: Literal["真实检测结果", "受控投放检出", "离线授权导入", "桩数据", "合成样例"]
    channel: str = Field(min_length=1, max_length=80)
    platform: str = Field(min_length=1, max_length=80)
    credential_type: str = Field(min_length=1, max_length=80)
    redacted_value: str = Field(min_length=1, max_length=160)
    evidence_level: Literal["L0", "L1", "L2", "L3", "L4"]
    risk_level: Literal["critical", "high", "medium", "low", "unknown"]
    disposition: str = Field(min_length=1, max_length=80)
    first_observed_at: str | None
    published_at: str | None = None
    published_source: str = ""
    published_confidence: str = "none"
    published_meaning: str = ""
    published_evidence: str = ""
    published_precision: str = "unknown"
    published_evidence_scope: str = ""
    published_content_version: str = ""
    public_time_status: str = "unknown"
    public_time_unknown_reason: str = ""
    fetched_at: str | None = None
    fetched_at_source: str = "unknown"
    source_updated_at: str | None = None
    source_updated_source: str = ""
    source_updated_confidence: str = "none"
    source_updated_meaning: str = ""
    source_updated_evidence: str = ""
    time_provenance: dict[str, Any] = Field(default_factory=dict)
    source_locator: str | None = None
    detected_at: str | None = None
    first_discovered_at: str | None = None
    first_discovery_time_status: str = "unknown"
    first_discovery_unknown_reason: str = ""
    last_discovered_at: str | None = None
    discovery_latency_seconds: float | None = None
    discovery_latency_status: str = "not_calculable"
    discovery_latency_reason: str = ""
    discovery_latency_calculation_type: str = "not_calculable"
    discovery_latency_basis: str = ""
    discovery_latency_end_at: str | None = None
    detection_method: str | None = None
    credential_fingerprint: str | None = None
    fingerprint_key_version: str | None = None

    @field_validator("redacted_value")
    @classmethod
    def require_mask(cls, value: str) -> str:
        # This is a mask marker check, not proof that arbitrary input is safe.
        if "****" not in value or "\n" in value or "\r" in value:
            raise ValueError("only single-line masked previews are accepted")
        return value


class AuthorizedManifest(ContractModel):
    manifest_version: Literal["1.0"]
    authorization_id: str = Field(min_length=1, max_length=120)
    owner: str = Field(min_length=1, max_length=160)
    authorization_scope: str = Field(min_length=1, max_length=500)
    expires_at: str = Field(min_length=1, max_length=40)
    channel: str = Field(min_length=1, max_length=80)
    platform: str = Field(min_length=1, max_length=80)
    export_file: str = Field(min_length=1, max_length=200)
    synthetic_only: bool


class ConnectorRun(ContractModel):
    run_id: str
    connector_name: str
    target: str
    started_at: str
    finished_at: str | None
    status: Literal["dry_run", "running", "succeeded", "failed"]
    candidates: int
    inserted: int
    filtered: int = 0
    filtered_reason: str | None = None
    new_credentials: int = 0
    error_code: str | None = None
    task_id: str | None = None


class ConnectorRunsResponse(ContractModel):
    schema_version: Literal["1.0"]
    page_title: str
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    items: list[ConnectorRun]


class EventFilter(ContractModel):
    key: str
    label: str
    default: str
    options: list[str]


class PageInfo(ContractModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    sort_by: Literal["event_id", "first_observed_at", "risk_level", "credential_type"]
    sort_order: Literal["asc", "desc"]


class EventSummary(ContractModel):
    candidate_count: int | None = Field(ge=0)
    classified_count: int | None = Field(ge=0)
    public_exposure_confirmed_count: int | None = Field(ge=0)
    unique_credential_count: int | None = Field(ge=0)
    event_count: int = Field(ge=0)


class EventResponse(ContractModel):
    schema_version: Literal["1.0"]
    page_title: str
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    filters: list[EventFilter]
    summary: EventSummary
    items: list[EventRecord]
    pagination: PageInfo


class DetailItem(ContractModel):
    label: str
    value: str


class TimelineItem(ContractModel):
    time: str
    title: str
    detail: str


class DetailAction(ContractModel):
    label: str
    status: str
    action: str | None = None


class EventDetailResponse(ContractModel):
    schema_version: Literal["1.0"]
    page_title: str
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    event: dict[str, Any]
    evidence: list[DetailItem]
    rechecks: list[DetailItem]
    timeline: list[TimelineItem]
    actions: list[DetailAction]
    alerts: list["AlertRecord"] = Field(default_factory=list)
    feedbacks: list["FeedbackRecord"] = Field(default_factory=list)


class FeedbackRequest(ContractModel):
    action: Literal["confirm_public", "mark_duplicate", "mark_false_positive", "recheck", "close_event"]
    note: str = Field(default="", max_length=500)


class FeedbackRecord(ContractModel):
    feedback_id: str
    event_id: str
    action: str
    disposition: str
    created_at: str
    note: str


class FeedbackResponse(ContractModel):
    schema_version: Literal["1.0"]
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    feedback: FeedbackRecord | None


class AlertResponse(ContractModel):
    schema_version: Literal["1.0"]
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    alert: "AlertRecord | None"


class AlertRecord(ContractModel):
    alert_id: str
    event_id: str
    severity: Literal["critical", "high", "medium", "low", "unknown"]
    status: Literal["queued", "offline_recorded"]
    channel: Literal["offline-sink"]
    created_at: str
    delivered_at: str | None
    delivery_note: str


class AlertsResponse(ContractModel):
    schema_version: Literal["1.0"]
    page_title: str
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    items: list[AlertRecord]


EventDetailResponse.model_rebuild()
AlertResponse.model_rebuild()


class MonitoringTask(ContractModel):
    task_id: str = Field(min_length=1, max_length=80)
    task_name: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=80)
    platform: str = Field(min_length=1, max_length=80)
    monitor_object: str = Field(min_length=1, max_length=300)
    collection_method: str = Field(min_length=1, max_length=80)
    schedule: str = Field(min_length=1, max_length=80)
    last_run: str | None
    status: Literal["运行中", "待配置", "采集失败", "已暂停"]
    status_color: Literal["low", "medium", "high"]
    last_run_status: Literal["dry_run", "running", "succeeded", "failed"] | None = None
    config: dict[str, Any] | None = None
    config_raw: str | None = None
    config_parse_error: bool = False
    last_error: str | None = None


class MonitoringTaskRequest(ContractModel):
    task_id: str = Field(min_length=1, max_length=80)
    task_name: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=80)
    platform: str = Field(min_length=1, max_length=80)
    monitor_object: str = Field(min_length=1, max_length=300)
    collection_method: str = Field(min_length=1, max_length=80)
    schedule: str = Field(min_length=1, max_length=80)


class TaskSummary(ContractModel):
    label: str
    value: int = Field(ge=0)
    hint: str


class MonitoringTaskResponse(ContractModel):
    schema_version: Literal["1.0"]
    page_title: str
    source: str
    implementation_status: Literal["未实现", "部分实现", "已实现"]
    error: str | None
    filters: list[EventFilter]
    summary: list[TaskSummary]
    items: list[MonitoringTask]
    runs: list[ConnectorRun] = []
    release_notice: str | None = None


class SettingsUpdateRequest(ContractModel):
    values: dict[str, Any]


class GithubConnectionRequest(ContractModel):
    repository: str = Field(min_length=3, max_length=200)
