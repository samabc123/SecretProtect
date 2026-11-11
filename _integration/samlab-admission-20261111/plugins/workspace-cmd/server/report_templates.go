package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
)

type reportTemplateDefinition struct {
	AnalysisVersion int                     `json:"analysis_version"`
	Title           string                  `json:"title"`
	Sections        []reportTemplateSection `json:"sections"`
}

type reportTemplateSection struct {
	ID          string               `json:"id"`
	Number      string               `json:"number"`
	Title       string               `json:"title"`
	Description string               `json:"description"`
	Items       []reportTemplateItem `json:"items"`
}

type reportTemplateItem struct {
	ID                 int                `json:"id"`
	Title              string             `json:"title"`
	Description        string             `json:"description"`
	ScoreRule          string             `json:"score_rule"`
	MaxScore           *float64           `json:"max_score"`
	ScoringMode        string             `json:"scoring_mode"`
	ManualReview       bool               `json:"manual_review"`
	Enabled            bool               `json:"enabled"`
	SortOrder          int                `json:"sort_order"`
	PromptGuidance     string             `json:"prompt_guidance"`
	EvidenceRules      []string           `json:"evidence_rules,omitempty"`
	ScoringFormula     string             `json:"scoring_formula,omitempty"`
	Milestones         []reportMilestone  `json:"milestones,omitempty"`
	UnitPoints         map[string]float64 `json:"unit_points,omitempty"`
	DeduplicationRules []string           `json:"deduplication_rules,omitempty"`
}

type reportMilestone struct {
	Code   string  `json:"code"`
	Label  string  `json:"label"`
	Points float64 `json:"points"`
}

type reportTemplateRecord struct {
	ID          int64                    `json:"id"`
	Version     int                      `json:"version"`
	Name        string                   `json:"name"`
	Status      string                   `json:"status"`
	Definition  reportTemplateDefinition `json:"definition"`
	CreatedBy   *int64                   `json:"created_by,omitempty"`
	CreatedAt   time.Time                `json:"created_at"`
	UpdatedAt   time.Time                `json:"updated_at"`
	ActivatedAt *time.Time               `json:"activated_at,omitempty"`
}

func (a *app) activeReportTemplate(ctx context.Context) (reportTemplateRecord, error) {
	row := a.db.QueryRow(ctx, `SELECT id,version,name,status,definition,created_by,created_at,updated_at,activated_at
		FROM weekly_report_templates WHERE status='active' ORDER BY version DESC LIMIT 1`)
	return scanReportTemplate(row)
}

func scanReportTemplate(row rowScanner) (reportTemplateRecord, error) {
	var item reportTemplateRecord
	var raw []byte
	if err := row.Scan(&item.ID, &item.Version, &item.Name, &item.Status, &raw, &item.CreatedBy, &item.CreatedAt, &item.UpdatedAt, &item.ActivatedAt); err != nil {
		return reportTemplateRecord{}, err
	}
	if err := json.Unmarshal(raw, &item.Definition); err != nil {
		return reportTemplateRecord{}, err
	}
	return item, nil
}

func validateReportTemplate(def *reportTemplateDefinition) error {
	if def == nil {
		return errors.New("模板内容不能为空")
	}
	def.Title = truncateRunes(def.Title, 160)
	if def.Title == "" {
		return errors.New("模板标题不能为空")
	}
	if def.AnalysisVersion < 3 {
		def.AnalysisVersion = 3
	}
	if len(def.Sections) == 0 || len(def.Sections) > 12 {
		return errors.New("模板分区数量无效")
	}
	sectionIDs := make(map[string]bool)
	itemIDs := make(map[int]bool)
	itemCount := 0
	for sectionIndex := range def.Sections {
		section := &def.Sections[sectionIndex]
		section.ID = truncateRunes(section.ID, 48)
		section.Number = truncateRunes(section.Number, 12)
		section.Title = truncateRunes(section.Title, 120)
		section.Description = truncateRunes(section.Description, 1000)
		if section.ID == "" || section.Number == "" || section.Title == "" || sectionIDs[section.ID] {
			return fmt.Errorf("模板分区 %d 的编号或标题无效", sectionIndex+1)
		}
		sectionIDs[section.ID] = true
		if len(section.Items) == 0 || len(section.Items) > 60 {
			return fmt.Errorf("分区 %s 的项目数量无效", section.Title)
		}
		for itemIndex := range section.Items {
			item := &section.Items[itemIndex]
			item.Title = truncateRunes(item.Title, 160)
			item.Description = truncateRunes(item.Description, 2000)
			item.ScoreRule = truncateRunes(item.ScoreRule, 600)
			item.PromptGuidance = truncateRunes(item.PromptGuidance, 2000)
			item.ScoringFormula = truncateRunes(item.ScoringFormula, 1200)
			item.EvidenceRules = normalizeStringItems(item.EvidenceRules, 24, 600)
			item.DeduplicationRules = normalizeStringItems(item.DeduplicationRules, 16, 600)
			if item.ID <= 0 || item.Title == "" || item.Description == "" || item.ScoreRule == "" || item.PromptGuidance == "" || itemIDs[item.ID] {
				return fmt.Errorf("项目 %d 的编号或标题无效", item.ID)
			}
			itemIDs[item.ID] = true
			itemCount++
			if item.SortOrder <= 0 {
				item.SortOrder = item.ID
			}
			switch item.ScoringMode {
			case "ai_suggest", "objective_count", "manual":
			default:
				return fmt.Errorf("项目 %d 的评分方式无效", item.ID)
			}
			if item.MaxScore != nil && (*item.MaxScore < 0 || *item.MaxScore > 10000) {
				return fmt.Errorf("项目 %d 的最高分无效", item.ID)
			}
			if item.ScoringMode == "ai_suggest" && item.MaxScore == nil {
				return fmt.Errorf("项目 %d 的 AI 建议分必须设置最高分", item.ID)
			}
			if item.ScoringMode == "manual" {
				item.ManualReview = true
			}
			if def.AnalysisVersion >= 4 {
				if len(item.EvidenceRules) == 0 || item.ScoringFormula == "" || len(item.DeduplicationRules) == 0 {
					return fmt.Errorf("项目 %d 缺少 v4 证据或评分规则", item.ID)
				}
				seenMilestones := make(map[string]bool)
				for milestoneIndex := range item.Milestones {
					milestone := &item.Milestones[milestoneIndex]
					milestone.Code = truncateRunes(strings.TrimSpace(milestone.Code), 64)
					milestone.Label = truncateRunes(strings.TrimSpace(milestone.Label), 160)
					if milestone.Code == "" || milestone.Label == "" || milestone.Points < 0 || milestone.Points > 10000 || seenMilestones[milestone.Code] {
						return fmt.Errorf("项目 %d 的里程碑规则无效", item.ID)
					}
					seenMilestones[milestone.Code] = true
				}
				for code, points := range item.UnitPoints {
					if strings.TrimSpace(code) == "" || points < 0 || points > 10000 {
						return fmt.Errorf("项目 %d 的单位分值无效", item.ID)
					}
				}
			}
		}
		sort.SliceStable(section.Items, func(i, j int) bool { return section.Items[i].SortOrder < section.Items[j].SortOrder })
	}
	if itemCount == 0 || itemCount > 200 {
		return errors.New("模板项目总数无效")
	}
	return nil
}

func (a *app) adminWeeklyReportTemplates(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		rows, err := a.db.Query(r.Context(), `SELECT id,version,name,status,definition,created_by,created_at,updated_at,activated_at
			FROM weekly_report_templates ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, version DESC`)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取周报模板失败")
			return
		}
		defer rows.Close()
		items := make([]reportTemplateRecord, 0)
		for rows.Next() {
			item, scanErr := scanReportTemplate(rows)
			if scanErr != nil {
				writeAPIError(w, http.StatusInternalServerError, "读取周报模板失败")
				return
			}
			items = append(items, item)
		}
		writeJSON(w, http.StatusOK, map[string]any{"items": items})
	case http.MethodPost:
		var existingID int64
		if err := a.db.QueryRow(r.Context(), `SELECT id FROM weekly_report_templates WHERE status='draft' LIMIT 1`).Scan(&existingID); err == nil {
			item, loadErr := a.loadReportTemplate(r.Context(), existingID)
			if loadErr != nil {
				writeAPIError(w, http.StatusInternalServerError, "读取模板草稿失败")
				return
			}
			writeJSON(w, http.StatusOK, item)
			return
		}
		active, err := a.activeReportTemplate(r.Context())
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "缺少已发布模板")
			return
		}
		var draftID int64
		err = a.db.QueryRow(r.Context(), `INSERT INTO weekly_report_templates(version,name,status,definition,created_by)
			VALUES((SELECT COALESCE(MAX(version),0)+1 FROM weekly_report_templates),$1,'draft',$2,$3) RETURNING id`,
			active.Name, jsonBytes(active.Definition), id.ID).Scan(&draftID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "创建模板草稿失败")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_template_create_draft", map[string]any{"template_id": draftID})
		item, _ := a.loadReportTemplate(r.Context(), draftID)
		writeJSON(w, http.StatusCreated, item)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (a *app) adminWeeklyReportTemplateByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/admin/weekly-report-templates/"), "/"), "/")
	templateID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || templateID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "模板编号无效")
		return
	}
	if len(parts) == 1 && r.Method == http.MethodPatch {
		var body struct {
			Name       string                   `json:"name"`
			Definition reportTemplateDefinition `json:"definition"`
		}
		if err := readJSON(r, &body, 2<<20); err != nil {
			writeAPIError(w, http.StatusBadRequest, "模板内容无效")
			return
		}
		body.Name = truncateRunes(body.Name, 160)
		if body.Name == "" {
			writeAPIError(w, http.StatusBadRequest, "模板名称不能为空")
			return
		}
		if err := validateReportTemplate(&body.Definition); err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		result, err := a.db.Exec(r.Context(), `UPDATE weekly_report_templates SET name=$1,definition=$2,updated_at=now() WHERE id=$3 AND status='draft'`, body.Name, jsonBytes(body.Definition), templateID)
		if err != nil || result.RowsAffected() == 0 {
			writeAPIError(w, http.StatusConflict, "只能修改模板草稿")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_template_edit", map[string]any{"template_id": templateID})
		item, _ := a.loadReportTemplate(r.Context(), templateID)
		writeJSON(w, http.StatusOK, item)
		return
	}
	if len(parts) == 2 && parts[1] == "activate" && r.Method == http.MethodPost {
		item, err := a.loadReportTemplate(r.Context(), templateID)
		if err != nil || item.Status != "draft" {
			writeAPIError(w, http.StatusConflict, "只能发布模板草稿")
			return
		}
		if err := validateReportTemplate(&item.Definition); err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		tx, err := a.db.Begin(r.Context())
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "发布模板失败")
			return
		}
		if _, err = tx.Exec(r.Context(), `UPDATE weekly_report_templates SET status='archived',updated_at=now() WHERE status='active'`); err == nil {
			_, err = tx.Exec(r.Context(), `UPDATE weekly_report_templates SET status='active',activated_at=now(),updated_at=now() WHERE id=$1 AND status='draft'`, templateID)
		}
		if err != nil {
			_ = tx.Rollback(r.Context())
			writeAPIError(w, http.StatusInternalServerError, "发布模板失败")
			return
		}
		if err := tx.Commit(r.Context()); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "发布模板失败")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_template_activate", map[string]any{"template_id": templateID, "version": item.Version})
		item, _ = a.loadReportTemplate(r.Context(), templateID)
		writeJSON(w, http.StatusOK, item)
		return
	}
	writeAPIError(w, http.StatusNotFound, "模板接口不存在")
}

func (a *app) loadReportTemplate(ctx context.Context, templateID int64) (reportTemplateRecord, error) {
	return scanReportTemplate(a.db.QueryRow(ctx, `SELECT id,version,name,status,definition,created_by,created_at,updated_at,activated_at
		FROM weekly_report_templates WHERE id=$1`, templateID))
}

func (a *app) auditEvent(ctx context.Context, actorID int64, action string, metadata map[string]any) error {
	_, err := a.db.Exec(ctx, `INSERT INTO workspace_audit_logs(actor_id,action,document_id,metadata) VALUES($1,$2,NULL,$3)`, actorID, action, jsonBytes(metadata))
	return err
}

func templateItemMap(def reportTemplateDefinition) map[int]reportTemplateItem {
	items := make(map[int]reportTemplateItem)
	for _, section := range def.Sections {
		for _, item := range section.Items {
			if item.Enabled {
				items[item.ID] = item
			}
		}
	}
	return items
}
