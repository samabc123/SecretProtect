package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// 人工校准回路。
// 管理员修改的是「档位」而不是最终分，改动与 AI 原值一并落库形成标注样本，
// 既用于在线校准参数统计，也可导出为微调数据集。

// scoringEngineVersion 返回当前生效的评分引擎版本。
func (a *app) scoringEngineVersion() string {
	if a.cfg.ScoringEngineVersion == "v4" {
		return "v4"
	}
	return "v5"
}

// ---------------------------------------------------------------------------
// 校准参数
// ---------------------------------------------------------------------------

// calibrationProfile 读取在线校准参数，失败时返回恒等校准。
func (a *app) calibrationProfile(ctx context.Context) CalibrationProfile {
	rows, err := a.db.Query(ctx, `SELECT field,bias,scale,enabled FROM weekly_score_calibration_profiles`)
	if err != nil {
		logWarningf("scoring calibration profile load failed: %v", err)
		return DefaultCalibrationProfile()
	}
	defer rows.Close()
	profile := DefaultCalibrationProfile()
	for rows.Next() {
		var field string
		var param CalibrationField
		if err := rows.Scan(&field, &param.Bias, &param.Scale, &param.Enabled); err != nil {
			continue
		}
		if !validCalibrationField(field) {
			continue
		}
		profile.Fields[field] = param
	}
	return profile
}

// adminScoringCalibration 管理端校准参数的读写。
func (a *app) adminScoringCalibration(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		rows, err := a.db.Query(r.Context(), `SELECT id,field,scope,bias,scale,enabled,version,comment,updated_by,created_at,updated_at
			FROM weekly_score_calibration_profiles ORDER BY field,scope`)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取校准参数失败")
			return
		}
		defer rows.Close()
		items := make([]map[string]any, 0)
		for rows.Next() {
			var profileID int64
			var field, scope, comment string
			var bias, scale float64
			var enabled bool
			var version int
			var updatedBy *int64
			var createdAt, updatedAt time.Time
			if err := rows.Scan(&profileID, &field, &scope, &bias, &scale, &enabled, &version, &comment, &updatedBy, &createdAt, &updatedAt); err != nil {
				continue
			}
			items = append(items, map[string]any{"id": profileID, "field": field, "scope": scope, "bias": bias, "scale": scale,
				"enabled": enabled, "version": version, "comment": comment, "updated_by": updatedBy, "created_at": createdAt, "updated_at": updatedAt})
		}
		writeJSON(w, http.StatusOK, map[string]any{"items": items, "fields": validCalibrationFields()})
	case http.MethodPut:
		var body struct {
			Field   string   `json:"field"`
			Scope   string   `json:"scope"`
			Bias    *float64 `json:"bias"`
			Scale   *float64 `json:"scale"`
			Enabled *bool    `json:"enabled"`
			Comment string   `json:"comment"`
		}
		if err := readJSON(r, &body, 1<<16); err != nil {
			writeAPIError(w, http.StatusBadRequest, "请求内容无效")
			return
		}
		if !validCalibrationField(body.Field) {
			writeAPIError(w, http.StatusBadRequest, "校准字段无效")
			return
		}
		scope := strings.TrimSpace(body.Scope)
		if scope == "" {
			scope = "global"
		}
		scope = truncateRunes(scope, 64)
		param := CalibrationField{Scale: 1, Enabled: true}
		if body.Bias != nil {
			param.Bias = math.Max(-4, math.Min(4, *body.Bias))
		}
		if body.Scale != nil {
			param.Scale = math.Max(0.25, math.Min(4, *body.Scale))
		}
		if body.Enabled != nil {
			param.Enabled = *body.Enabled
		}
		if _, err := a.db.Exec(r.Context(), `INSERT INTO weekly_score_calibration_profiles(field,scope,bias,scale,enabled,comment,updated_by)
			VALUES($1,$2,$3,$4,$5,$6,$7)
			ON CONFLICT(field,scope) DO UPDATE SET bias=excluded.bias,scale=excluded.scale,enabled=excluded.enabled,
			comment=excluded.comment,updated_by=excluded.updated_by,version=weekly_score_calibration_profiles.version+1,updated_at=now()`,
			body.Field, scope, param.Bias, param.Scale, param.Enabled, truncateRunes(body.Comment, 500), id.ID); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "保存校准参数失败")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_scoring_calibration_update", map[string]any{"field": body.Field, "scope": scope})
		writeJSON(w, http.StatusOK, map[string]any{"field": body.Field, "scope": scope, "bias": param.Bias, "scale": param.Scale, "enabled": param.Enabled})
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// ---------------------------------------------------------------------------
// 历史数据：连续性与项目分序列
// ---------------------------------------------------------------------------

// previousWeekFingerprints 返回上一周已存在任务的指纹集合，用于判断任务是否跨周连续。
func (a *app) previousWeekFingerprints(ctx context.Context, userID int64, start time.Time) map[string]bool {
	result := make(map[string]bool)
	rows, err := a.db.Query(ctx, `SELECT fingerprint FROM weekly_work_items WHERE user_id=$1 AND week_start=$2`,
		userID, start.AddDate(0, 0, -7).Format("2006-01-02"))
	if err != nil {
		return result
	}
	defer rows.Close()
	for rows.Next() {
		var fingerprint string
		if err := rows.Scan(&fingerprint); err == nil {
			result[fingerprint] = true
		}
	}
	return result
}

// recentProjectScoreHistory 一次性读取全部项目的历史得分，避免每个项目各查一次。
func (a *app) recentProjectScoreHistory(ctx context.Context, userID int64, start time.Time, weeks int) map[int][]float64 {
	result := make(map[int][]float64)
	if weeks <= 0 {
		weeks = 8
	}
	rows, err := a.db.Query(ctx, `SELECT a.item_id, w.week_start, COALESCE(SUM(a.score_component),0)
		FROM weekly_work_items w JOIN weekly_project_attributions a ON a.work_item_id=w.id
		WHERE w.user_id=$1 AND w.week_start < $2 AND w.week_start >= ($2::date - make_interval(weeks => $3))
		GROUP BY a.item_id, w.week_start ORDER BY a.item_id, w.week_start`, userID, start.Format("2006-01-02"), weeks)
	if err != nil {
		return result
	}
	defer rows.Close()
	for rows.Next() {
		var itemID int
		var weekStart time.Time
		var total float64
		if err := rows.Scan(&itemID, &weekStart, &total); err != nil {
			continue
		}
		result[itemID] = append(result[itemID], total)
	}
	return result
}

// recentProjectScores 返回本人近若干周同一项目的历史得分，用于异常跳变检测。
func (a *app) recentProjectScores(ctx context.Context, userID int64, itemID int, start time.Time, weeks int) []float64 {
	if weeks <= 0 {
		weeks = 8
	}
	rows, err := a.db.Query(ctx, `SELECT COALESCE(SUM(a.score_component),0)
		FROM weekly_work_items w JOIN weekly_project_attributions a ON a.work_item_id=w.id
		WHERE w.user_id=$1 AND a.item_id=$2 AND w.week_start < $3 AND w.week_start >= ($3::date - make_interval(weeks => $4))
		GROUP BY w.week_start ORDER BY w.week_start`, userID, itemID, start.Format("2006-01-02"), weeks)
	if err != nil {
		return nil
	}
	defer rows.Close()
	result := make([]float64, 0, weeks)
	for rows.Next() {
		var total float64
		if err := rows.Scan(&total); err == nil {
			result = append(result, total)
		}
	}
	return result
}

// ---------------------------------------------------------------------------
// 人工改档位
// ---------------------------------------------------------------------------

// adminWeeklyReportAttribution 修改单条归因的档位并留下校准样本。
func (a *app) adminWeeklyReportAttribution(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	if r.Method != http.MethodPatch {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/admin/weekly-scoring/attributions/"), "/"), "/")
	attributionID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || attributionID <= 0 || len(parts) != 1 {
		writeAPIError(w, http.StatusBadRequest, "归因编号无效")
		return
	}
	var body struct {
		Stage                *string  `json:"stage"`
		EvidenceQuality      *float64 `json:"evidence_quality"`
		PersonalContribution *float64 `json:"personal_contribution"`
		Complexity           *float64 `json:"complexity"`
		AllocationPercent    *float64 `json:"allocation_percent"`
		MilestoneCode        *string  `json:"milestone_code"`
		UnitCount            *float64 `json:"unit_count"`
		Reason               string   `json:"review_reason"`
	}
	if err := readJSON(r, &body, 1<<16); err != nil {
		writeAPIError(w, http.StatusBadRequest, "请求内容无效")
		return
	}
	if strings.TrimSpace(body.Reason) == "" {
		writeAPIError(w, http.StatusBadRequest, "必须填写修改原因")
		return
	}
	result, err := a.applyAttributionCalibration(r.Context(), id.ID, attributionID, attributionCalibrationRequest{
		Stage: body.Stage, EvidenceQuality: body.EvidenceQuality, PersonalContribution: body.PersonalContribution,
		Complexity: body.Complexity, AllocationPercent: body.AllocationPercent, MilestoneCode: body.MilestoneCode,
		UnitCount: body.UnitCount, Reason: body.Reason,
	})
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

type attributionCalibrationRequest struct {
	Stage                *string
	EvidenceQuality      *float64
	PersonalContribution *float64
	Complexity           *float64
	AllocationPercent    *float64
	MilestoneCode        *string
	UnitCount            *float64
	Reason               string
}

// applyAttributionCalibration 原子地写入人工档位、校准样本并重算受影响分数。
func (a *app) applyAttributionCalibration(ctx context.Context, reviewerID, attributionID int64, body attributionCalibrationRequest) (map[string]any, error) {
	var workItemID int64
	var itemID int
	var oldAllocation, oldUnitCount float64
	var oldMilestone string
	err := a.db.QueryRow(ctx, `SELECT work_item_id,item_id,allocation_percent,milestone_code,unit_count
		FROM weekly_project_attributions WHERE id=$1`, attributionID).Scan(&workItemID, &itemID, &oldAllocation, &oldMilestone, &oldUnitCount)
	if err != nil {
		return nil, errors.New("归因不存在")
	}
	var userID int64
	var weekStart time.Time
	var oldStage string
	var oldStageScore, oldQuality, oldContribution, oldComplexity float64
	var ruleSignals []byte
	var aiStage string
	var aiQuality, aiContribution, aiComplexity float64
	err = a.db.QueryRow(ctx, `SELECT user_id,week_start,stage,stage_score,evidence_quality,personal_contribution,complexity,rule_signals,
		ai_stage,ai_evidence_quality,ai_personal_contribution,ai_complexity
		FROM weekly_work_items WHERE id=$1`, workItemID).Scan(&userID, &weekStart, &oldStage, &oldStageScore, &oldQuality,
		&oldContribution, &oldComplexity, &ruleSignals, &aiStage, &aiQuality, &aiContribution, &aiComplexity)
	if err != nil {
		return nil, errors.New("任务不存在")
	}
	template, err := a.activeReportTemplate(ctx)
	if err != nil {
		return nil, errors.New("缺少已发布模板")
	}
	item, ok := templateItemMap(template.Definition)[itemID]
	if !ok {
		return nil, errors.New("评分项目不存在")
	}

	rubrics := numericRubrics()
	reason := truncateRunes(strings.TrimSpace(body.Reason), 1000)
	changes := make([]map[string]any, 0, 7)

	newStage, newStageScore := oldStage, oldStageScore
	if body.Stage != nil {
		newStage = NormalizeStageCode(*body.Stage)
		newStageScore = StageScore(newStage)
	}
	newQuality, newContribution, newComplexity := oldQuality, oldContribution, oldComplexity
	if body.EvidenceQuality != nil {
		newQuality, _, _ = SnapToLadder(rubrics[fieldEvidenceQuality], *body.EvidenceQuality)
	}
	if body.PersonalContribution != nil {
		newContribution, _, _ = SnapToLadder(rubrics[fieldPersonalContribution], *body.PersonalContribution)
	}
	if body.Complexity != nil {
		newComplexity, _, _ = SnapToLadder(rubrics[fieldComplexity], *body.Complexity)
	}
	newAllocation, newMilestone, newUnitCount := oldAllocation, oldMilestone, oldUnitCount
	if body.AllocationPercent != nil {
		newAllocation = math.Max(0, math.Min(100, *body.AllocationPercent))
	}
	if body.MilestoneCode != nil {
		newMilestone = validateMilestoneCode(item, strings.TrimSpace(*body.MilestoneCode))
	}
	if body.UnitCount != nil {
		newUnitCount = math.Max(0, math.Min(1000, *body.UnitCount))
	}

	tx, err := a.db.Begin(ctx)
	if err != nil {
		return nil, errors.New("开启事务失败")
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback(ctx)
		}
	}()

	if _, err := tx.Exec(ctx, `UPDATE weekly_work_items SET stage=$2,stage_score=$3,evidence_quality=$4,personal_contribution=$5,complexity=$6,
		reviewed_by=$7,reviewed_at=now(),requires_manual_review=false,updated_at=now() WHERE id=$1`,
		workItemID, newStage, newStageScore, newQuality, newContribution, newComplexity, reviewerID); err != nil {
		return nil, errors.New("更新任务失败")
	}
	if _, err := tx.Exec(ctx, `UPDATE weekly_project_attributions SET allocation_percent=$2,milestone_code=$3,unit_count=$4,
		reviewed_by=$5,reviewed_at=now(),requires_manual_review=false,updated_at=now() WHERE id=$1`,
		attributionID, newAllocation, newMilestone, newUnitCount, reviewerID); err != nil {
		return nil, errors.New("更新归因失败")
	}

	appendChange := func(field, aiValue, oldValue, newValue string, delta float64) {
		changes = append(changes, map[string]any{"field": field, "ai_value": aiValue, "old_value": oldValue, "new_value": newValue, "delta": delta})
	}
	if newStage != oldStage {
		appendChange("stage", aiStage, oldStage, newStage, newStageScore-oldStageScore)
		if _, err := tx.Exec(ctx, `INSERT INTO weekly_score_calibrations(user_id,week_start,work_item_id,attribution_id,item_id,field,ai_value,human_value,delta,reason,rule_signals,engine_version,reviewer_id)
			VALUES($1,$2,$3,$4,$5,'stage',$6,$7,$8,$9,$10,$11,$12)`, userID, weekStart.Format("2006-01-02"), workItemID, attributionID, itemID,
			aiStage, newStage, newStageScore-oldStageScore, reason, ruleSignals, a.scoringEngineVersion(), reviewerID); err != nil {
			return nil, errors.New("写入校准样本失败")
		}
	}
	if newQuality != oldQuality {
		appendChange(fieldEvidenceQuality, strconv.FormatFloat(aiQuality, 'f', 2, 64), strconv.FormatFloat(oldQuality, 'f', 2, 64), strconv.FormatFloat(newQuality, 'f', 2, 64), newQuality-oldQuality)
		if _, err := tx.Exec(ctx, `INSERT INTO weekly_score_calibrations(user_id,week_start,work_item_id,attribution_id,item_id,field,ai_value,human_value,delta,reason,rule_signals,engine_version,reviewer_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, userID, weekStart.Format("2006-01-02"), workItemID, attributionID, itemID,
			fieldEvidenceQuality, strconv.FormatFloat(aiQuality, 'f', 2, 64), strconv.FormatFloat(newQuality, 'f', 2, 64), newQuality-oldQuality, reason, ruleSignals, a.scoringEngineVersion(), reviewerID); err != nil {
			return nil, errors.New("写入校准样本失败")
		}
	}
	if newContribution != oldContribution {
		appendChange(fieldPersonalContribution, strconv.FormatFloat(aiContribution, 'f', 2, 64), strconv.FormatFloat(oldContribution, 'f', 2, 64), strconv.FormatFloat(newContribution, 'f', 2, 64), newContribution-oldContribution)
		if _, err := tx.Exec(ctx, `INSERT INTO weekly_score_calibrations(user_id,week_start,work_item_id,attribution_id,item_id,field,ai_value,human_value,delta,reason,rule_signals,engine_version,reviewer_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, userID, weekStart.Format("2006-01-02"), workItemID, attributionID, itemID,
			fieldPersonalContribution, strconv.FormatFloat(aiContribution, 'f', 2, 64), strconv.FormatFloat(newContribution, 'f', 2, 64), newContribution-oldContribution, reason, ruleSignals, a.scoringEngineVersion(), reviewerID); err != nil {
			return nil, errors.New("写入校准样本失败")
		}
	}
	if newComplexity != oldComplexity {
		appendChange(fieldComplexity, strconv.FormatFloat(aiComplexity, 'f', 2, 64), strconv.FormatFloat(oldComplexity, 'f', 2, 64), strconv.FormatFloat(newComplexity, 'f', 2, 64), newComplexity-oldComplexity)
		if _, err := tx.Exec(ctx, `INSERT INTO weekly_score_calibrations(user_id,week_start,work_item_id,attribution_id,item_id,field,ai_value,human_value,delta,reason,rule_signals,engine_version,reviewer_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, userID, weekStart.Format("2006-01-02"), workItemID, attributionID, itemID,
			fieldComplexity, strconv.FormatFloat(aiComplexity, 'f', 2, 64), strconv.FormatFloat(newComplexity, 'f', 2, 64), newComplexity-oldComplexity, reason, ruleSignals, a.scoringEngineVersion(), reviewerID); err != nil {
			return nil, errors.New("写入校准样本失败")
		}
	}
	if newAllocation != oldAllocation {
		appendChange(fieldAllocation, strconv.FormatFloat(oldAllocation, 'f', 2, 64), strconv.FormatFloat(oldAllocation, 'f', 2, 64), strconv.FormatFloat(newAllocation, 'f', 2, 64), newAllocation-oldAllocation)
		if _, err := tx.Exec(ctx, `INSERT INTO weekly_score_calibrations(user_id,week_start,work_item_id,attribution_id,item_id,field,ai_value,human_value,delta,reason,rule_signals,engine_version,reviewer_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, userID, weekStart.Format("2006-01-02"), workItemID, attributionID, itemID,
			fieldAllocation, strconv.FormatFloat(oldAllocation, 'f', 2, 64), strconv.FormatFloat(newAllocation, 'f', 2, 64), newAllocation-oldAllocation, reason, ruleSignals, a.scoringEngineVersion(), reviewerID); err != nil {
			return nil, errors.New("写入校准样本失败")
		}
	}
	if len(changes) == 0 {
		return nil, errors.New("没有需要保存的变更")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, errors.New("提交失败")
	}
	committed = true

	recomputed := a.recomputeProjectScore(ctx, userID, weekStart, itemID)
	_ = a.auditEvent(ctx, reviewerID, "weekly_attribution_calibration", map[string]any{"attribution_id": attributionID, "item_id": itemID, "changes": len(changes)})
	return map[string]any{"attribution_id": attributionID, "work_item_id": workItemID, "item_id": itemID, "changes": changes, "recomputed": recomputed}, nil
}

// recomputeProjectScore 重算指定项目的建议分并回写周报 narrative。
func (a *app) recomputeProjectScore(ctx context.Context, userID int64, weekStart time.Time, itemID int) map[string]any {
	result := map[string]any{"item_id": itemID, "updated": false}
	if itemID <= 1 {
		result["reason"] = "计划兑现项目需重新生成周报后生效"
		return result
	}
	template, err := a.activeReportTemplate(ctx)
	if err != nil {
		result["reason"] = "缺少已发布模板"
		return result
	}
	item, ok := templateItemMap(template.Definition)[itemID]
	if !ok {
		result["reason"] = "评分项目不存在"
		return result
	}
	workItems, err := a.loadWeeklyWorkItems(ctx, userID, weekStart.Format("2006-01-02"))
	if err != nil {
		result["reason"] = "读取任务失败"
		return result
	}
	_, inputs := workItemsForProject(workItems, itemID)
	signals := make(map[int64]RuleSignalSet, len(workItems))
	confidences := make(map[int64]float64, len(workItems))
	for _, value := range workItems {
		signals[value.ID] = value.RuleSignals
		confidences[value.ID] = value.Confidence
	}
	score, breakdown := CalculateProjectScoreV5(ScoreContext{
		Item: item, Inputs: inputs, Signals: signals, Confidences: confidences,
		Calib: a.calibrationProfile(ctx), PrevScores: a.recentProjectScores(ctx, userID, itemID, weekStart, 8),
		Rules: a.scoringRuleSet(ctx), EngineVer: a.scoringEngineVersion(),
	})
	for _, input := range inputs {
		if input.AttributionID > 0 {
			_, _ = a.db.Exec(ctx, `UPDATE weekly_project_attributions SET score_component=$2,updated_at=now() WHERE id=$1`, input.AttributionID, scoreForWorkItem(breakdown, input.WorkItemID))
		}
	}
	updated := a.updateReportItemScore(ctx, userID, weekStart, itemID, score, breakdown)
	result["updated"] = updated
	result["score"] = score
	result["confidence"] = breakdown.Confidence
	return result
}

// scoreForWorkItem 从明细中取出单个任务对项目的贡献分。
func scoreForWorkItem(breakdown reportScoreBreakdown, workItemID int64) float64 {
	total := 0.0
	for _, component := range breakdown.Components {
		if component.WorkItemID == workItemID {
			total += component.Score
		}
	}
	return total
}

// updateReportItemScore 把重算结果写回周报 narrative 的对应项目。
func (a *app) updateReportItemScore(ctx context.Context, userID int64, weekStart time.Time, itemID int, score float64, breakdown reportScoreBreakdown) bool {
	var reportID int64
	var raw []byte
	err := a.db.QueryRow(ctx, `SELECT id,narrative FROM weekly_reports WHERE user_id=$1 AND week_start=$2`, userID, weekStart.Format("2006-01-02")).Scan(&reportID, &raw)
	if err != nil || len(raw) == 0 {
		return false
	}
	var narrative reportV3Narrative
	if err := json.Unmarshal(raw, &narrative); err != nil {
		return false
	}
	found := false
	for sectionIndex := range narrative.Sections {
		for itemIndex := range narrative.Sections[sectionIndex].Items {
			item := &narrative.Sections[sectionIndex].Items[itemIndex]
			if item.ItemID != itemID {
				continue
			}
			found = true
			value := score
			item.AIScore = &value
			item.ScoreBreakdown = &breakdown
			item.Confidence = ConfidenceLevel(breakdown.Confidence)
			item.ConfidenceScore = breakdown.Confidence
			item.AnomalyFlags = breakdown.AnomalyFlags
			item.RequiresManualReview = breakdown.RequiresManualReview
			item.ScoreBasis = fmt.Sprintf("%s；人工校准后由服务端公式重算为 %.1f 分。", firstNonEmpty(item.ScoreRule, ""), score)
		}
	}
	if !found {
		return false
	}
	if _, err := a.db.Exec(ctx, `UPDATE weekly_reports SET narrative=$2,updated_at=now() WHERE id=$1`, reportID, jsonBytes(narrative)); err != nil {
		return false
	}
	return true
}

// ---------------------------------------------------------------------------
// 复核队列
// ---------------------------------------------------------------------------

// adminScoringReviewQueue 返回需要人工复核的任务与归因。
func (a *app) adminScoringReviewQueue(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	week := strings.TrimSpace(r.URL.Query().Get("week"))
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	offset, _ := strconv.Atoi(r.URL.Query().Get("offset"))
	if offset < 0 {
		offset = 0
	}
	floor := a.cfg.ScoringConfidenceFloor
	if floor <= 0 {
		floor = 0.5
	}
	query := `SELECT w.id,w.user_id,w.week_start,w.title,w.stage,w.stage_score,w.evidence_quality,w.personal_contribution,w.complexity,
		w.confidence,w.anomaly_flags,w.requires_manual_review,w.classification_status,w.rule_signals,
		a.id,a.item_id,a.allocation_percent,a.milestone_code,a.unit_count,a.confidence,a.anomaly_flags,a.score_component
		FROM weekly_work_items w LEFT JOIN weekly_project_attributions a ON a.work_item_id=w.id
		WHERE (w.requires_manual_review OR w.confidence < $1)`
	args := []any{floor}
	if week != "" {
		if parsed, err := time.ParseInLocation("2006-01-02", week, a.cfg.Timezone); err == nil && parsed.Weekday() == time.Monday {
			query += fmt.Sprintf(" AND w.week_start=$%d", len(args)+1)
			args = append(args, week)
		}
	}
	query += fmt.Sprintf(" ORDER BY w.confidence ASC, w.week_start DESC LIMIT $%d OFFSET $%d", len(args)+1, len(args)+2)
	args = append(args, limit, offset)
	rows, err := a.db.Query(r.Context(), query, args...)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取复核队列失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0, limit)
	for rows.Next() {
		var workID, userID int64
		var itemWeekStart time.Time
		var title, stage, classification string
		var stageScore, quality, contribution, complexity, confidence float64
		var flags []string
		var requiresReview bool
		var ruleSignals []byte
		var attributionID *int64
		var itemID *int
		var allocation, unitCount, attributionConfidence, scoreComponent float64
		var milestone string
		var attributionFlags []string
		if err := rows.Scan(&workID, &userID, &itemWeekStart, &title, &stage, &stageScore, &quality, &contribution, &complexity,
			&confidence, &flags, &requiresReview, &classification, &ruleSignals,
			&attributionID, &itemID, &allocation, &milestone, &unitCount, &attributionConfidence, &attributionFlags, &scoreComponent); err != nil {
			continue
		}
		var signals RuleSignalSet
		_ = json.Unmarshal(ruleSignals, &signals)
		items = append(items, map[string]any{
			"work_item_id": workID, "user_id": userID, "week_start": itemWeekStart.Format("2006-01-02"), "title": title,
			"stage": stage, "stage_score": stageScore, "evidence_quality": quality, "personal_contribution": contribution, "complexity": complexity,
			"confidence": confidence, "confidence_level": ConfidenceLevel(confidence), "anomaly_flags": flags, "requires_manual_review": requiresReview,
			"classification_status": classification, "rule_signals": signals,
			"attribution_id": attributionID, "item_id": itemID, "allocation_percent": allocation, "milestone_code": milestone,
			"unit_count": unitCount, "attribution_confidence": attributionConfidence, "attribution_flags": attributionFlags, "score_component": scoreComponent,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset, "confidence_floor": floor})
}

// ---------------------------------------------------------------------------
// 校准统计与数据集导出
// ---------------------------------------------------------------------------

// adminScoringCalibrationStats 返回各字段的人工校准偏差统计。
func (a *app) adminScoringCalibrationStats(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	rows, err := a.db.Query(r.Context(), `SELECT field,COUNT(*),COALESCE(AVG(ABS(delta)),0),COALESCE(AVG(delta),0)
		FROM weekly_score_calibrations GROUP BY field ORDER BY field`)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取校准统计失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0, 8)
	for rows.Next() {
		var field string
		var count int64
		var meanAbs, meanSigned float64
		if err := rows.Scan(&field, &count, &meanAbs, &meanSigned); err != nil {
			continue
		}
		items = append(items, map[string]any{"field": field, "samples": count,
			"mae": math.Round(meanAbs*10000) / 10000, "mean_signed_error": math.Round(meanSigned*10000) / 10000})
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

// adminScoringCalibrationExport 导出脱敏后的微调数据集（SFT 或 DPO）。
func (a *app) adminScoringCalibrationExport(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	format := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("format")))
	if format == "" {
		format = "sft"
	}
	if format != "sft" && format != "dpo" {
		writeAPIError(w, http.StatusBadRequest, "format 只能是 sft 或 dpo")
		return
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit <= 0 || limit > 5000 {
		limit = 1000
	}
	rows, err := a.db.Query(r.Context(), `SELECT c.field,c.ai_value,c.human_value,c.delta,c.reason,c.rule_signals,c.engine_version,w.title,w.summary,w.stage,w.beneficiary,w.item_id
		FROM weekly_score_calibrations c
		LEFT JOIN LATERAL (
			SELECT w2.title,w2.summary,w2.stage,w2.beneficiary,a.item_id
			FROM weekly_work_items w2 LEFT JOIN weekly_project_attributions a ON a.work_item_id=w2.id
			WHERE w2.id=c.work_item_id AND (a.id=c.attribution_id OR c.attribution_id IS NULL) LIMIT 1
		) w ON TRUE
		ORDER BY c.created_at DESC LIMIT $1`, limit)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "导出数据集失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0, limit)
	for rows.Next() {
		var field, aiValue, humanValue, reason, engineVersion string
		var delta float64
		var ruleSignals []byte
		var title, summary, stage, beneficiary *string
		var itemID *int
		if err := rows.Scan(&field, &aiValue, &humanValue, &delta, &reason, &ruleSignals, &engineVersion,
			&title, &summary, &stage, &beneficiary, &itemID); err != nil {
			continue
		}
		var signals RuleSignalSet
		_ = json.Unmarshal(ruleSignals, &signals)
		features := map[string]any{
			"title":        truncateRunes(derefString(title), 240),
			"summary":      truncateRunes(derefString(summary), 800),
			"stage":        derefString(stage),
			"beneficiary":  derefString(beneficiary),
			"item_id":      derefInt(itemID),
			"rule_signals": signals,
		}
		promptText := buildCalibrationPrompt(field, features)
		if format == "dpo" {
			items = append(items, map[string]any{"field": field, "prompt": promptText, "chosen": humanValue, "rejected": aiValue})
			continue
		}
		items = append(items, map[string]any{"field": field, "prompt": promptText, "completion": humanValue,
			"engine_version": engineVersion, "delta": delta})
	}
	writeJSON(w, http.StatusOK, map[string]any{"format": format, "items": items})
}

// buildCalibrationPrompt 生成导出样本的提示文本（不含用户身份与原始对话）。
func buildCalibrationPrompt(field string, features map[string]any) string {
	return fmt.Sprintf("请依据以下脱敏特征判定 %s 的档位，只输出档位值。\n特征：%s", field, string(jsonBytes(features)))
}

func derefString(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func derefInt(value *int) int {
	if value == nil {
		return 0
	}
	return *value
}

// logWarningf 统一输出评分链路的可操作日志，严禁记录原始对话与密钥。
func logWarningf(format string, args ...any) {
	log.Printf("[scoring] "+format, args...)
}
