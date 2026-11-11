package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type reportGenerationJob struct {
	ID              int64      `json:"id"`
	UserID          int64      `json:"user_id"`
	WeekStart       string     `json:"week_start"`
	TemplateID      *int64     `json:"template_id,omitempty"`
	RequestedBy     *int64     `json:"requested_by,omitempty"`
	RebuildBaseline bool       `json:"rebuild_baseline"`
	Status          string     `json:"status"`
	Stage           string     `json:"stage"`
	Attempts        int        `json:"attempts"`
	ReportID        *int64     `json:"report_id,omitempty"`
	ErrorMessage    string     `json:"error_message,omitempty"`
	CreatedAt       time.Time  `json:"created_at"`
	StartedAt       *time.Time `json:"started_at,omitempty"`
	FinishedAt      *time.Time `json:"finished_at,omitempty"`
	UpdatedAt       time.Time  `json:"updated_at"`
}

func scanReportGenerationJob(row rowScanner) (reportGenerationJob, error) {
	var item reportGenerationJob
	var start time.Time
	if err := row.Scan(&item.ID, &item.UserID, &start, &item.TemplateID, &item.RequestedBy, &item.RebuildBaseline, &item.Status, &item.Stage,
		&item.Attempts, &item.ReportID, &item.ErrorMessage, &item.CreatedAt, &item.StartedAt, &item.FinishedAt, &item.UpdatedAt); err != nil {
		return reportGenerationJob{}, err
	}
	item.WeekStart = start.Format("2006-01-02")
	return item, nil
}

func (a *app) enqueueReportJob(ctx context.Context, userID int64, start time.Time, requestedBy int64, rebuildBaseline bool) (reportGenerationJob, error) {
	row := a.db.QueryRow(ctx, `INSERT INTO weekly_report_generation_jobs(user_id,week_start,template_id,requested_by,rebuild_baseline)
		VALUES($1,$2,NULL,$3,false) ON CONFLICT DO NOTHING
		RETURNING id,user_id,week_start,template_id,requested_by,rebuild_baseline,status,stage,attempts,report_id,COALESCE(error_message,''),created_at,started_at,finished_at,updated_at`,
		userID, start.Format("2006-01-02"), requestedBy)
	var err error
	job, err := scanReportGenerationJob(row)
	if errors.Is(err, pgx.ErrNoRows) {
		if rebuildBaseline {
			_, _ = a.db.Exec(ctx, `UPDATE weekly_report_generation_jobs SET rebuild_baseline=true,updated_at=now()
				WHERE user_id=$1 AND week_start=$2 AND status IN ('queued','running')`, userID, start.Format("2006-01-02"))
		}
		job, err = scanReportGenerationJob(a.db.QueryRow(ctx, `SELECT id,user_id,week_start,template_id,requested_by,rebuild_baseline,status,stage,attempts,report_id,COALESCE(error_message,''),created_at,started_at,finished_at,updated_at
			FROM weekly_report_generation_jobs WHERE user_id=$1 AND week_start=$2 AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1`,
			userID, start.Format("2006-01-02")))
	}
	if err == nil {
		select {
		case a.reportWake <- struct{}{}:
		default:
		}
	}
	return job, err
}

func (a *app) weeklyReportJob(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	raw := strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/weekly-report-jobs/"), "/")
	jobID, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || jobID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "生成任务编号无效")
		return
	}
	job, err := scanReportGenerationJob(a.db.QueryRow(r.Context(), `SELECT id,user_id,week_start,template_id,requested_by,rebuild_baseline,status,stage,attempts,report_id,COALESCE(error_message,''),created_at,started_at,finished_at,updated_at
		FROM weekly_report_generation_jobs WHERE id=$1`, jobID))
	if err != nil || (id.Role != "admin" && job.UserID != id.ID) {
		writeAPIError(w, http.StatusNotFound, "生成任务不存在")
		return
	}
	response := map[string]any{"job": job}
	if job.ReportID != nil {
		if report, loadErr := a.loadReportByID(r.Context(), *job.ReportID); loadErr == nil {
			if id.Role != "admin" {
				if review, ok := report["review"].(map[string]any); !ok || review["status"] != "confirmed" {
					report["review"] = nil
				}
			}
			response["report"] = report
		}
	}
	writeJSON(w, http.StatusOK, response)
}

func (a *app) reportWorker(ctx context.Context) {
	_, _ = a.db.Exec(ctx, `UPDATE weekly_report_generation_jobs SET status='queued',stage='recovered',started_at=NULL,updated_at=now()
		WHERE status='running' AND updated_at < now()-INTERVAL '20 minutes'`)
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		for {
			job, ok := a.claimReportJob(ctx)
			if !ok {
				break
			}
			a.runReportJob(ctx, job)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-a.reportWake:
		}
	}
}

func (a *app) claimReportJob(ctx context.Context) (reportGenerationJob, bool) {
	tx, err := a.db.Begin(ctx)
	if err != nil {
		return reportGenerationJob{}, false
	}
	defer func() { _ = tx.Rollback(ctx) }()
	job, err := scanReportGenerationJob(tx.QueryRow(ctx, `SELECT id,user_id,week_start,template_id,requested_by,rebuild_baseline,status,stage,attempts,report_id,COALESCE(error_message,''),created_at,started_at,finished_at,updated_at
		FROM weekly_report_generation_jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`))
	if err != nil {
		return reportGenerationJob{}, false
	}
	if _, err := tx.Exec(ctx, `UPDATE weekly_report_generation_jobs SET status='running',stage='collecting',attempts=attempts+1,started_at=COALESCE(started_at,now()),updated_at=now() WHERE id=$1`, job.ID); err != nil {
		return reportGenerationJob{}, false
	}
	if err := tx.Commit(ctx); err != nil {
		return reportGenerationJob{}, false
	}
	job.Status = "running"
	job.Stage = "collecting"
	job.Attempts++
	return job, true
}

func (a *app) runReportJob(parent context.Context, job reportGenerationJob) {
	ctx, cancel := context.WithTimeout(parent, 90*time.Minute)
	defer cancel()
	start, err := time.ParseInLocation("2006-01-02", job.WeekStart, a.cfg.Timezone)
	if err != nil {
		a.failReportJob(ctx, job, 0, "周报周期无效")
		return
	}
	// 新生成任务统一使用 v6“工作总结 + 周/月指导”结构。
	// 模板记录继续作为生成快照保留，旧 v2-v5 报告仍由前端兼容展示，
	// 但不再创建固定项目分类、项目归因或评分结果。
	report, err := a.generateReportV6(ctx, job, start)
	reportID := reportIDFromMap(report)
	if err != nil {
		a.failReportJob(context.Background(), job, reportID, truncateRunes(err.Error(), 1000))
		return
	}
	_, _ = a.db.Exec(context.Background(), `UPDATE weekly_report_generation_jobs SET status='succeeded',stage='completed',report_id=NULLIF($2,0),error_message=NULL,finished_at=now(),updated_at=now() WHERE id=$1`, job.ID, reportID)
	a.auditReportJobOutcome(job, reportID, "weekly_report_generation_succeeded", "")
}

func reportIDFromMap(report map[string]any) int64 {
	if report == nil {
		return 0
	}
	switch value := report["id"].(type) {
	case int64:
		return value
	case int:
		return int64(value)
	case float64:
		return int64(value)
	case json.Number:
		id, _ := value.Int64()
		return id
	default:
		return 0
	}
}

func (a *app) failReportJob(ctx context.Context, job reportGenerationJob, reportID int64, message string) {
	message = redactReportContent(truncateRunes(message, 1000))
	_, _ = a.db.Exec(ctx, `UPDATE weekly_report_generation_jobs SET status='failed',stage='failed',report_id=NULLIF($2,0),error_message=$3,finished_at=now(),updated_at=now() WHERE id=$1`, job.ID, reportID, message)
	a.auditReportJobOutcome(job, reportID, "weekly_report_generation_failed", message)
}

func (a *app) auditReportJobOutcome(job reportGenerationJob, reportID int64, action, message string) {
	if job.RequestedBy == nil || *job.RequestedBy <= 0 {
		return
	}
	detail := map[string]any{"job_id": job.ID, "report_id": reportID, "user_id": job.UserID, "week_start": job.WeekStart}
	if message != "" {
		detail["error"] = message
	}
	_ = a.auditEvent(context.Background(), *job.RequestedBy, action, detail)
}

func (a *app) updateReportJobStage(ctx context.Context, jobID int64, stage string) {
	_, _ = a.db.Exec(ctx, `UPDATE weekly_report_generation_jobs SET stage=$2,updated_at=now() WHERE id=$1`, jobID, truncateRunes(stage, 64))
}
