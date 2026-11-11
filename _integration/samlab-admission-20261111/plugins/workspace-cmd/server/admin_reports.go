package main

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

func (a *app) adminWeeklyReports(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	start, err := parseWeek(r.URL.Query().Get("week_start"), a.cfg.Timezone)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	q := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("q")))
	roleFilter := strings.TrimSpace(r.URL.Query().Get("role"))
	statusFilter := strings.TrimSpace(r.URL.Query().Get("status"))
	reviewFilter := strings.TrimSpace(r.URL.Query().Get("review_status"))
	page := queryInt(r, "page", 1, 1, 100000)
	pageSize := queryInt(r, "page_size", 30, 1, 100)

	rows, err := a.db.Query(r.Context(), `SELECT id,username,COALESCE(email,''),role FROM users WHERE status='active' AND deleted_at IS NULL ORDER BY role,username,id`)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取成员周报失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	summary := map[string]int{"total_users": 0, "ready": 0, "partial": 0, "failed": 0, "generating": 0, "missing": 0, "confirmed": 0}
	for rows.Next() {
		var userID int64
		var username, email, role string
		if rows.Scan(&userID, &username, &email, &role) != nil {
			continue
		}
		summary["total_users"]++
		report, reportErr := a.loadReportSummaryForUser(r.Context(), userID, start)
		job, jobErr := a.latestReportJobForWeek(r.Context(), userID, start)
		effectiveStatus := "missing"
		reviewStatus := "unreviewed"
		if reportErr == nil {
			effectiveStatus, _ = report["status"].(string)
			if value, ok := report["review_status"].(string); ok && value != "" {
				reviewStatus = value
			}
		}
		if jobErr == nil && (job.Status == "queued" || job.Status == "running") {
			effectiveStatus = "generating"
		}
		if _, exists := summary[effectiveStatus]; exists {
			summary[effectiveStatus]++
		} else if effectiveStatus == "metrics_only" {
			summary["partial"]++
		}
		if reviewStatus == "confirmed" {
			summary["confirmed"]++
		}
		if q != "" && !strings.Contains(strings.ToLower(username), q) && !strings.Contains(strings.ToLower(email), q) {
			continue
		}
		if roleFilter != "" && role != roleFilter {
			continue
		}
		if statusFilter != "" && effectiveStatus != statusFilter {
			continue
		}
		if reviewFilter != "" && reviewStatus != reviewFilter {
			continue
		}
		item := map[string]any{
			"user":   map[string]any{"user_id": userID, "username": username, "email": email, "role": role},
			"status": effectiveStatus, "review_status": reviewStatus,
		}
		if reportErr == nil {
			item["report"] = report
		}
		if jobErr == nil {
			item["job"] = job
		}
		items = append(items, item)
	}
	if rows.Err() != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取成员周报失败")
		return
	}
	total := len(items)
	from := (page - 1) * pageSize
	if from > total {
		from = total
	}
	to := from + pageSize
	if to > total {
		to = total
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"week_start": start.Format("2006-01-02"), "week_end": start.AddDate(0, 0, 6).Format("2006-01-02"),
		"items": items[from:to], "summary": summary,
		"pagination": map[string]int{"page": page, "page_size": pageSize, "total": total},
	})
}

func (a *app) adminWeeklyReportByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/admin/weekly-reports/"), "/"), "/")
	if len(parts) == 1 && parts[0] == "generate" && r.Method == http.MethodPost {
		a.adminGenerateWeekly(w, r, id)
		return
	}
	if len(parts) == 2 && parts[1] == "history" && r.Method == http.MethodGet {
		userID, err := strconv.ParseInt(parts[0], 10, 64)
		if err != nil || userID <= 0 {
			writeAPIError(w, http.StatusBadRequest, "成员编号无效")
			return
		}
		a.adminWeeklyReportHistory(w, r, userID)
		return
	}
	reportID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || reportID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "周报编号无效")
		return
	}
	if a.adminReportEvidence(w, r, id, reportID, parts) {
		return
	}
	switch {
	case len(parts) == 1 && r.Method == http.MethodGet:
		report, err := a.loadReportByID(r.Context(), reportID)
		if err != nil {
			writeAPIError(w, http.StatusNotFound, "周报不存在")
			return
		}
		var userID int64
		var username, email, role string
		if err := a.db.QueryRow(r.Context(), `SELECT u.id,u.username,COALESCE(u.email,''),u.role FROM users u JOIN weekly_reports wr ON wr.user_id=u.id WHERE wr.id=$1`, reportID).Scan(&userID, &username, &email, &role); err != nil {
			writeAPIError(w, http.StatusNotFound, "周报成员不存在")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"user": map[string]any{"user_id": userID, "username": username, "email": email, "role": role}, "report": report})
	case len(parts) == 2 && parts[1] == "retry" && r.Method == http.MethodPost:
		var userID int64
		var start time.Time
		if err := a.db.QueryRow(r.Context(), `SELECT user_id,week_start FROM weekly_reports WHERE id=$1`, reportID).Scan(&userID, &start); err != nil {
			writeAPIError(w, http.StatusNotFound, "周报不存在")
			return
		}
		job, err := a.enqueueReportJob(r.Context(), userID, start.In(a.cfg.Timezone), id.ID, true)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "重新生成周报失败")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_report_retry", map[string]any{"report_id": reportID, "job_id": job.ID, "user_id": userID})
		writeJSON(w, http.StatusAccepted, map[string]any{"job": job})
	case len(parts) == 2 && parts[1] == "review" && r.Method == http.MethodPatch:
		a.saveWeeklyReportReview(w, r, id, reportID)
	case len(parts) == 3 && parts[1] == "review" && parts[2] == "confirm" && r.Method == http.MethodPost:
		a.confirmWeeklyReportReview(w, r, id, reportID)
	default:
		writeAPIError(w, http.StatusNotFound, "周报管理接口不存在")
	}
}

func (a *app) adminWeeklyReportHistory(w http.ResponseWriter, r *http.Request, userID int64) {
	limit := queryInt(r, "limit", 52, 1, 52)
	var exists bool
	if err := a.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND deleted_at IS NULL)`, userID).Scan(&exists); err != nil || !exists {
		writeAPIError(w, http.StatusNotFound, "成员不存在")
		return
	}
	rows, err := a.db.Query(r.Context(), `SELECT id,week_start,week_end,status,generated_at,updated_at,template_id,analysis_version,
		COALESCE(narrative->'previous_week_plan'->>'plan_status',''),COALESCE(narrative->'previous_week_plan'->>'week_start',''),
		COALESCE(narrative->'previous_week_plan'->>'source','')
		FROM weekly_reports WHERE user_id=$1 ORDER BY week_start DESC LIMIT $2`, userID, limit)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取历史周报失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var reportID int64
		var start, end, updated time.Time
		var status, baselineStatus, baselineWeek, baselineSource string
		var generated *time.Time
		var templateID *int64
		var analysisVersion int
		if rows.Scan(&reportID, &start, &end, &status, &generated, &updated, &templateID, &analysisVersion,
			&baselineStatus, &baselineWeek, &baselineSource) != nil {
			continue
		}
		items = append(items, map[string]any{"id": reportID, "week_start": start.Format("2006-01-02"),
			"week_end": end.Format("2006-01-02"), "status": status, "generated_at": generated, "updated_at": updated,
			"template_id": templateID, "analysis_version": analysisVersion, "baseline_status": baselineStatus,
			"baseline_week": baselineWeek, "baseline_source": baselineSource})
	}
	if rows.Err() != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取历史周报失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"user_id": userID, "items": items})
}

func (a *app) adminGenerateWeekly(w http.ResponseWriter, r *http.Request, id identity) {
	var body struct {
		UserID    int64  `json:"user_id"`
		WeekStart string `json:"week_start"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil || body.UserID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "生成参数无效")
		return
	}
	start, err := parseWeek(body.WeekStart, a.cfg.Timezone)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	var exists bool
	if err := a.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND status='active' AND deleted_at IS NULL)`, body.UserID).Scan(&exists); err != nil || !exists {
		writeAPIError(w, http.StatusNotFound, "成员不存在或已停用")
		return
	}
	job, err := a.enqueueReportJob(r.Context(), body.UserID, start, id.ID, true)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "创建生成任务失败")
		return
	}
	_ = a.auditEvent(r.Context(), id.ID, "weekly_report_admin_generate", map[string]any{"job_id": job.ID, "user_id": body.UserID, "week_start": start.Format("2006-01-02")})
	writeJSON(w, http.StatusAccepted, map[string]any{"job": job})
}

func (a *app) saveWeeklyReportReview(w http.ResponseWriter, r *http.Request, id identity, reportID int64) {
	var body struct {
		Scores         map[string]*float64 `json:"scores"`
		OverallComment string              `json:"overall_comment"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil {
		writeAPIError(w, http.StatusBadRequest, "评分内容无效")
		return
	}
	var templateID *int64
	if err := a.db.QueryRow(r.Context(), `SELECT template_id FROM weekly_reports WHERE id=$1`, reportID).Scan(&templateID); err != nil || templateID == nil {
		writeAPIError(w, http.StatusConflict, "旧版周报不支持项目评分，请先重新生成 v3")
		return
	}
	template, err := a.loadReportTemplate(r.Context(), *templateID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取评分规则失败")
		return
	}
	normalized, overall, err := normalizeReviewScores(template.Definition, body.Scores)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	scores := map[string]any{"items": normalized, "overall_score": overall}
	comment := truncateRunes(body.OverallComment, 4000)
	_, err = a.db.Exec(r.Context(), `INSERT INTO weekly_report_reviews(report_id,reviewer_id,status,scores,overall_comment)
		VALUES($1,$2,'draft',$3,$4) ON CONFLICT(report_id) DO UPDATE SET reviewer_id=excluded.reviewer_id,status='draft',scores=excluded.scores,
		overall_comment=excluded.overall_comment,confirmed_at=NULL,updated_at=now()`, reportID, id.ID, jsonBytes(scores), comment)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "保存评分失败")
		return
	}
	_ = a.auditEvent(r.Context(), id.ID, "weekly_report_review_save", map[string]any{"report_id": reportID, "overall_score": scores["overall_score"]})
	report, _ := a.loadReportByID(r.Context(), reportID)
	writeJSON(w, http.StatusOK, report)
}

func normalizeReviewScores(def reportTemplateDefinition, input map[string]*float64) (map[string]*float64, float64, error) {
	items := templateItemMap(def)
	normalized := make(map[string]*float64, len(input))
	overall := 0.0
	for key, score := range input {
		itemID, err := strconv.Atoi(key)
		item, ok := items[itemID]
		if err != nil || !ok {
			return nil, 0, fmt.Errorf("评分项目不存在：%s", key)
		}
		if score == nil {
			normalized[key] = nil
			continue
		}
		if math.IsNaN(*score) || math.IsInf(*score, 0) || *score < 0 || (item.MaxScore != nil && *score > *item.MaxScore) || *score > 10000 {
			return nil, 0, fmt.Errorf("项目 %s 的评分超出范围", key)
		}
		value := math.Round(*score*100) / 100
		normalized[key] = &value
		overall += value
	}
	return normalized, math.Round(overall*100) / 100, nil
}

func (a *app) confirmWeeklyReportReview(w http.ResponseWriter, r *http.Request, id identity, reportID int64) {
	result, err := a.db.Exec(r.Context(), `UPDATE weekly_report_reviews SET status='confirmed',reviewer_id=$2,confirmed_at=now(),updated_at=now() WHERE report_id=$1`, reportID, id.ID)
	if err != nil || result.RowsAffected() == 0 {
		writeAPIError(w, http.StatusConflict, "请先保存评分草稿")
		return
	}
	_ = a.auditEvent(r.Context(), id.ID, "weekly_report_review_confirm", map[string]any{"report_id": reportID})
	report, _ := a.loadReportByID(r.Context(), reportID)
	writeJSON(w, http.StatusOK, report)
}

func (a *app) loadReportSummaryForUser(ctx context.Context, userID int64, start time.Time) (map[string]any, error) {
	var reportID int64
	var status string
	var generated *time.Time
	var templateID *int64
	var analysisVersion int
	var reviewStatus *string
	err := a.db.QueryRow(ctx, `SELECT wr.id,wr.status,wr.generated_at,wr.template_id,wr.analysis_version,rv.status
		FROM weekly_reports wr LEFT JOIN weekly_report_reviews rv ON rv.report_id=wr.id WHERE wr.user_id=$1 AND wr.week_start=$2`,
		userID, start.Format("2006-01-02")).Scan(&reportID, &status, &generated, &templateID, &analysisVersion, &reviewStatus)
	if err != nil {
		return nil, err
	}
	return map[string]any{"id": reportID, "status": status, "generated_at": generated, "template_id": templateID, "analysis_version": analysisVersion, "review_status": reviewStatus}, nil
}

func (a *app) latestReportJobForWeek(ctx context.Context, userID int64, start time.Time) (reportGenerationJob, error) {
	return scanReportGenerationJob(a.db.QueryRow(ctx, `SELECT id,user_id,week_start,template_id,requested_by,rebuild_baseline,status,stage,attempts,report_id,COALESCE(error_message,''),created_at,started_at,finished_at,updated_at
		FROM weekly_report_generation_jobs WHERE user_id=$1 AND week_start=$2 ORDER BY created_at DESC LIMIT 1`, userID, start.Format("2006-01-02")))
}

func queryInt(r *http.Request, key string, fallback, minimum, maximum int) int {
	value, err := strconv.Atoi(strings.TrimSpace(r.URL.Query().Get(key)))
	if err != nil {
		return fallback
	}
	if value < minimum {
		return minimum
	}
	if value > maximum {
		return maximum
	}
	return value
}
