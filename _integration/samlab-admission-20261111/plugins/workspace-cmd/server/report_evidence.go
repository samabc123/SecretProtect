package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

const weeklyEvidenceExtractorVersion = 2

type weeklyEvidenceCard struct {
	ID               int64          `json:"id"`
	UserID           int64          `json:"user_id"`
	WeekStart        string         `json:"week_start"`
	SourceType       string         `json:"source_type"`
	SourceID         int64          `json:"source_id"`
	SourceSHA256     string         `json:"source_sha256"`
	ExtractorVersion int            `json:"extractor_version"`
	OccurredAt       time.Time      `json:"occurred_at"`
	ProcessingStatus string         `json:"processing_status"`
	EvidenceKind     string         `json:"evidence_kind"`
	Beneficiary      string         `json:"beneficiary"`
	TaskGoal         string         `json:"task_goal"`
	UserAction       string         `json:"user_action"`
	Tools            []string       `json:"tools"`
	FileChanges      []string       `json:"file_changes"`
	Deliverables     []string       `json:"deliverables"`
	Verification     []string       `json:"verification"`
	FailureSummary   string         `json:"failure_summary"`
	Summary          string         `json:"summary"`
	StageScore       float64        `json:"stage_score"`
	EvidenceQuality  float64        `json:"evidence_quality"`
	RawMeta          map[string]any `json:"raw_meta,omitempty"`
	ErrorMessage     string         `json:"error_message,omitempty"`
}

type weeklyWorkItem struct {
	ID                     int64                `json:"id"`
	UserID                 int64                `json:"user_id"`
	WeekStart              string               `json:"week_start"`
	Fingerprint            string               `json:"fingerprint"`
	EvidenceDigest         string               `json:"evidence_digest"`
	ClassificationStatus   string               `json:"classification_status"`
	Title                  string               `json:"title"`
	Summary                string               `json:"summary"`
	Beneficiary            string               `json:"beneficiary"`
	Status                 string               `json:"status"`
	Stage                  string               `json:"stage"`
	StageScore             float64              `json:"stage_score"`
	EvidenceQuality        float64              `json:"evidence_quality"`
	AIPersonalContribution float64              `json:"ai_personal_contribution"`
	PersonalContribution   float64              `json:"personal_contribution"`
	AIComplexity           float64              `json:"ai_complexity"`
	Complexity             float64              `json:"complexity"`
	Deliverables           []string             `json:"deliverables"`
	Verification           []string             `json:"verification"`
	FirstSeenAt            time.Time            `json:"first_seen_at"`
	LastSeenAt             time.Time            `json:"last_seen_at"`
	ReviewedBy             *int64               `json:"reviewed_by,omitempty"`
	ReviewedAt             *time.Time           `json:"reviewed_at,omitempty"`
	SourceRefs             []string             `json:"source_refs"`
	Attributions           []projectAttribution `json:"attributions"`
	// v5 新增：模型原始档位、规则信号、置信度与异常标记
	AIStage              string             `json:"ai_stage,omitempty"`
	AIStageScore         float64            `json:"ai_stage_score,omitempty"`
	AIEvidenceQuality    float64            `json:"ai_evidence_quality,omitempty"`
	RuleSignals          RuleSignalSet      `json:"rule_signals,omitempty"`
	Confidence           float64            `json:"confidence,omitempty"`
	ConfidenceFactors    map[string]float64 `json:"confidence_factors,omitempty"`
	AnomalyFlags         []string           `json:"anomaly_flags,omitempty"`
	RequiresManualReview bool               `json:"requires_manual_review,omitempty"`
	EngineVersion        string             `json:"engine_version,omitempty"`
}

type projectAttribution struct {
	ID                  int64    `json:"id"`
	WorkItemID          int64    `json:"work_item_id"`
	ItemID              int      `json:"item_id"`
	AllocationPercent   float64  `json:"allocation_percent"`
	AIAllocationPercent float64  `json:"ai_allocation_percent"`
	MilestoneCode       string   `json:"milestone_code"`
	AIMilestoneCode     string   `json:"ai_milestone_code,omitempty"`
	UnitCount           float64  `json:"unit_count"`
	AIUnitCount         float64  `json:"ai_unit_count,omitempty"`
	ScoreComponent      float64  `json:"score_component"`
	Rationale           string   `json:"rationale"`
	Confidence          float64  `json:"confidence,omitempty"`
	AnomalyFlags        []string `json:"anomaly_flags,omitempty"`
	// RequiresManualReview 标记该归因需要人工复核。
	RequiresManualReview bool       `json:"requires_manual_review,omitempty"`
	EngineVersion        string     `json:"engine_version,omitempty"`
	ReviewedBy           *int64     `json:"reviewed_by,omitempty"`
	ReviewedAt           *time.Time `json:"reviewed_at,omitempty"`
}

type weeklyManualEvidence struct {
	ID                   int64      `json:"id"`
	UserID               int64      `json:"user_id"`
	WeekStart            string     `json:"week_start"`
	ItemID               int        `json:"item_id"`
	Title                string     `json:"title"`
	Description          string     `json:"description"`
	DeliverableURL       string     `json:"deliverable_url"`
	Stage                string     `json:"stage"`
	Complexity           float64    `json:"complexity"`
	PersonalContribution float64    `json:"personal_contribution"`
	Status               string     `json:"status"`
	SubmittedBy          int64      `json:"submitted_by"`
	ReviewedBy           *int64     `json:"reviewed_by,omitempty"`
	ReviewComment        string     `json:"review_comment"`
	ReviewedAt           *time.Time `json:"reviewed_at,omitempty"`
	CreatedAt            time.Time  `json:"created_at"`
	UpdatedAt            time.Time  `json:"updated_at"`
}

type reportAttributionScoreInput struct {
	AttributionID        int64   `json:"attribution_id,omitempty"`
	WorkItemID           int64   `json:"work_item_id,omitempty"`
	Title                string  `json:"title,omitempty"`
	Stage                string  `json:"stage,omitempty"`
	StageScore           float64 `json:"stage_score"`
	EvidenceQuality      float64 `json:"evidence_quality"`
	PersonalContribution float64 `json:"personal_contribution"`
	AllocationPercent    float64 `json:"allocation_percent"`
	Complexity           float64 `json:"complexity"`
	MilestoneCode        string  `json:"milestone_code,omitempty"`
	UnitCount            float64 `json:"unit_count,omitempty"`
}

type reportScoreComponent struct {
	WorkItemID      int64   `json:"work_item_id,omitempty"`
	Title           string  `json:"title,omitempty"`
	MilestoneCode   string  `json:"milestone_code,omitempty"`
	BasePoints      float64 `json:"base_points"`
	StageScore      float64 `json:"stage_score"`
	EvidenceQuality float64 `json:"evidence_quality"`
	Contribution    float64 `json:"personal_contribution"`
	Allocation      float64 `json:"allocation_percent"`
	Complexity      float64 `json:"complexity"`
	UnitCount       float64 `json:"unit_count"`
	Score           float64 `json:"score"`
	// v5 新增：可解释性与复核支撑
	VolumeFactor         float64  `json:"volume_factor,omitempty"`
	AnomalyFlags         []string `json:"anomaly_flags,omitempty"`
	Confidence           float64  `json:"confidence,omitempty"`
	RequiresManualReview bool     `json:"requires_manual_review,omitempty"`
}

type reportScoreBreakdown struct {
	Formula       string                 `json:"formula"`
	RawScore      float64                `json:"raw_score"`
	FinalScore    float64                `json:"final_score"`
	MaxScore      *float64               `json:"max_score,omitempty"`
	EvidenceCount int                    `json:"evidence_count"`
	Components    []reportScoreComponent `json:"components"`
	// v5 新增：可解释性与复核支撑
	AnomalyFlags         []string           `json:"anomaly_flags,omitempty"`
	Confidence           float64            `json:"confidence,omitempty"`
	ConfidenceFactors    map[string]float64 `json:"confidence_factors,omitempty"`
	RequiresManualReview bool               `json:"requires_manual_review,omitempty"`
	EngineVersion        string             `json:"engine_version,omitempty"`
}

type evidenceCluster struct {
	Key     string
	Cards   []weeklyEvidenceCard
	Title   string
	Summary string
	// SummaryParts 保存每张卡的摘要，收尾时按卡数动态分配截断预算。
	SummaryParts []string
	// CardTitles 保存全部卡的任务目标，避免大簇只保留首卡标题导致信息丢失。
	CardTitles   []string
	Beneficiary  string
	Stage        string
	StageScore   float64
	Quality      float64
	Contribution float64
	Complexity   float64
	SourceRefs   []string
	Deliverables []string
	Verification []string
	FirstSeenAt  time.Time
	LastSeenAt   time.Time
}

// clusterSummaryBudget 按卡数动态分配摘要预算，避免 v4 中超过 4 张卡后完全丢弃后续摘要。
const (
	clusterSummaryTotalBudget = 2400
	clusterSummaryMaxCards    = 8
	clusterSummaryMinPerCard  = 200
)

// joinClusterSummaries 汇总簇内摘要。
func joinClusterSummaries(parts []string) string {
	if len(parts) > clusterSummaryMaxCards {
		parts = parts[:clusterSummaryMaxCards]
	}
	if len(parts) == 0 {
		return ""
	}
	perCard := clusterSummaryTotalBudget / len(parts)
	if perCard < clusterSummaryMinPerCard {
		perCard = clusterSummaryMinPerCard
	}
	kept := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			kept = append(kept, truncateNarrativeRunes(part, perCard))
		}
	}
	return truncateNarrativeRunes(strings.TrimSpace(strings.Join(kept, "\n")), clusterSummaryTotalBudget)
}

type classifiedCluster struct {
	ClusterKey           string              `json:"cluster_key"`
	Title                string              `json:"title"`
	Summary              string              `json:"summary"`
	Beneficiary          string              `json:"beneficiary"`
	Stage                string              `json:"stage"`
	EvidenceQuality      float64             `json:"evidence_quality"`
	PersonalContribution float64             `json:"personal_contribution"`
	Complexity           float64             `json:"complexity"`
	Allocations          []projectAllocation `json:"allocations"`
	ClassificationStatus string              `json:"-"`
	// v5 新增：证据先行的中间产物与自检
	EvidenceSignals []string             `json:"evidence_signals"`
	StageReason     string               `json:"stage_reason"`
	SelfCheck       attributionSelfCheck `json:"self_check"`
	// v5 新增：模型原始档位（人工校准偏差统计的基准）。以下字段不参与反序列化。
	AIStage                string             `json:"-"`
	AIStageScore           float64            `json:"-"`
	AIEvidenceQuality      float64            `json:"-"`
	AIPersonalContribution float64            `json:"-"`
	AIComplexity           float64            `json:"-"`
	Signals                RuleSignalSet      `json:"-"`
	AnomalyFlags           []string           `json:"-"`
	Confidence             float64            `json:"-"`
	ConfidenceFactors      map[string]float64 `json:"-"`
	RequiresManualReview   bool               `json:"-"`
}

type projectAllocation struct {
	ItemID            int     `json:"item_id"`
	AllocationPercent float64 `json:"allocation_percent"`
	MilestoneCode     string  `json:"milestone_code"`
	UnitCount         float64 `json:"unit_count"`
	Rationale         string  `json:"rationale"`
}

type clusterClassificationPayload struct {
	Items []classifiedCluster `json:"items"`
}

var evidenceCandidatePattern = regexp.MustCompile(`(?i)(论文|实验|消融|数据集|模型训练|投稿|返修|审稿|专利|组会|文献|网站|前端|后端|接口|数据库|部署|构建|测试|代码|程序|系统|工程|报告|ppt|基金|项目书|公众号|视频|会议|参会|出差|调研|竞赛|比赛|指导|开题|中期|答辩|学生|科研|paper|experiment|dataset|patent|review|deploy|build|test|code|website|frontend|backend|api|database|report)`)
var completionPattern = regexp.MustCompile(`(?i)(已完成|完成了|实现了|生成了|修复了|通过测试|测试通过|构建成功|部署成功|已发布|已提交|投稿成功|验收通过|交付完成|completed|implemented|fixed|tests? passed|build succeeded|deployed|submitted|published|accepted)`)
var verificationPattern = regexp.MustCompile(`(?i)(测试通过|全部通过|构建成功|编译通过|部署成功|验证通过|验收通过|检查点.*通过|退出码.?0|tests? passed|build succeeded|verified|healthy)`)
var externalConfirmationPattern = regexp.MustCompile(`(?i)(验收通过|投稿成功|已发布|获奖|录用|accepted|published|award)`)
var failurePattern = regexp.MustCompile(`(?i)(失败|报错|错误|超时|未通过|异常|failed|error|timeout|exception)`)
var designPattern = regexp.MustCompile(`(?i)(分析|调研|设计|方案|计划|规划|架构|评估|analysis|design|plan|architecture)`)
var draftPattern = regexp.MustCompile(`(?i)(初稿|草稿|编写|撰写|修改|实现中|部分完成|draft|writing|editing|in progress)`)
var artifactPattern = regexp.MustCompile(`(?i)([\p{L}\p{N}_-]{2,80}\.(?:pdf|docx?|xlsx?|pptx?|tex|md|csv|json|yaml|yml|go|py|js|ts|vue|java|png|jpg|jpeg|svg|zip))`)
var evidenceWindowsPathPattern = regexp.MustCompile(`(?i)[a-z]:\\(?:[^\s\\/:*?"<>|]+\\){1,12}[^\s\\/:*?"<>|]*`)
var evidenceUnixPathPattern = regexp.MustCompile(`(?i)/(?:home|users|root|opt|srv|var|tmp)/(?:[^\s/]+/){0,10}[^\s]*`)
var evidenceInstructionHeaderPattern = regexp.MustCompile(`(?i)(?:#{1,6}\s*)?(?:AGENTS\.md\s+instructions|system\s+instructions|developer\s+instructions)`)
var evidenceInstructionBlockPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?is)<recommended_plugins\b[^>]*>.*?</recommended_plugins\s*>`),
	regexp.MustCompile(`(?is)<skills_instructions\b[^>]*>.*?</skills_instructions\s*>`),
	regexp.MustCompile(`(?is)<app-context\b[^>]*>.*?</app-context\s*>`),
	regexp.MustCompile(`(?is)<environment_context\b[^>]*>.*?</environment_context\s*>`),
	regexp.MustCompile(`(?is)<permissions(?:\s+instructions)?\b[^>]*>.*?</permissions(?:\s+instructions)?\s*>`),
	regexp.MustCompile(`(?is)<apps_instructions\b[^>]*>.*?</apps_instructions\s*>`),
	regexp.MustCompile(`(?is)<collaboration_mode\b[^>]*>.*?</collaboration_mode\s*>`),
	regexp.MustCompile(`(?is)<INSTRUCTIONS\b[^>]*>.*?</INSTRUCTIONS\s*>`),
	regexp.MustCompile(`(?is)<(?:system|developer)\b[^>]*>.*?</(?:system|developer)\s*>`),
	regexp.MustCompile(`(?is)<(?:recommended_plugins|skills_instructions|app-context|environment_context|permissions(?:\s+instructions)?|apps_instructions|collaboration_mode|INSTRUCTIONS|system|developer)\b[^>]*>.*$`),
}

func roundHalf(value float64) float64 {
	return math.Round(value*2) / 2
}

func clamp01(value float64, fallback float64) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) || value <= 0 {
		return fallback
	}
	if value > 1 {
		return 1
	}
	return value
}

func calculateProjectScore(item reportTemplateItem, inputs []reportAttributionScoreInput) (float64, reportScoreBreakdown) {
	breakdown := reportScoreBreakdown{Formula: item.ScoringFormula, MaxScore: item.MaxScore, EvidenceCount: len(inputs), Components: make([]reportScoreComponent, 0, len(inputs))}
	if len(inputs) == 0 {
		breakdown.Formula = firstNonEmpty(breakdown.Formula, "无可核验证据，建议分为0")
		return 0, breakdown
	}
	milestones := make(map[string]float64)
	for _, value := range item.Milestones {
		milestones[value.Code] = value.Points
	}
	raw := 0.0
	seenMilestones := make(map[string]bool)
	for _, value := range inputs {
		stage := clamp01(value.StageScore, 0)
		quality := clamp01(value.EvidenceQuality, 0)
		contribution := clamp01(value.PersonalContribution, 0.5)
		allocation := math.Max(0, math.Min(100, value.AllocationPercent)) / 100
		complexity := clamp01(value.Complexity, 0.5)
		units := value.UnitCount
		if units <= 0 {
			units = 1
		}
		base := 0.0
		componentScore := 0.0
		if points, ok := milestones[value.MilestoneCode]; ok {
			if seenMilestones[value.MilestoneCode] {
				continue
			}
			seenMilestones[value.MilestoneCode] = true
			base = points
			componentScore = base * stage * quality * contribution * allocation
		} else if points, ok := item.UnitPoints[value.MilestoneCode]; ok {
			base = points
			componentScore = base * units * stage * quality * contribution * allocation
		} else if item.MaxScore != nil {
			base = *item.MaxScore
			componentScore = base * stage * quality * contribution * allocation * complexity
		}
		raw += componentScore
		breakdown.Components = append(breakdown.Components, reportScoreComponent{
			WorkItemID: value.WorkItemID, Title: value.Title, MilestoneCode: value.MilestoneCode, BasePoints: base,
			StageScore: stage, EvidenceQuality: quality, Contribution: contribution, Allocation: value.AllocationPercent,
			Complexity: complexity, UnitCount: units, Score: roundHalf(componentScore),
		})
	}
	if item.MaxScore != nil && raw > *item.MaxScore {
		raw = *item.MaxScore
	}
	final := roundHalf(raw)
	breakdown.RawScore = math.Round(raw*100) / 100
	breakdown.FinalScore = final
	if breakdown.Formula == "" {
		breakdown.Formula = "项目分值按阶段、证据质量、个人贡献、归因比例和复杂度计算"
	}
	return final, breakdown
}

func normalizeProjectAllocations(values []projectAllocation) []projectAllocation {
	merged := make(map[int]projectAllocation)
	order := make([]int, 0, len(values))
	for _, value := range values {
		if value.ItemID < 2 || value.ItemID > 21 || value.AllocationPercent <= 0 {
			continue
		}
		if existing, ok := merged[value.ItemID]; ok {
			existing.AllocationPercent += value.AllocationPercent
			if existing.MilestoneCode == "" {
				existing.MilestoneCode = value.MilestoneCode
			}
			existing.UnitCount = math.Max(existing.UnitCount, value.UnitCount)
			existing.Rationale = firstNonEmpty(existing.Rationale, value.Rationale)
			merged[value.ItemID] = existing
			continue
		}
		order = append(order, value.ItemID)
		merged[value.ItemID] = value
	}
	if len(order) == 0 {
		return nil
	}
	sort.Ints(order)
	total := 0.0
	for _, itemID := range order {
		total += merged[itemID].AllocationPercent
	}
	if total <= 0 {
		return nil
	}
	result := make([]projectAllocation, 0, len(order))
	assigned := 0.0
	for index, itemID := range order {
		value := merged[itemID]
		if index == len(order)-1 {
			value.AllocationPercent = math.Round((100-assigned)*100) / 100
		} else {
			value.AllocationPercent = math.Round(value.AllocationPercent/total*10000) / 100
			assigned += value.AllocationPercent
		}
		if value.UnitCount <= 0 {
			value.UnitCount = 1
		}
		value.Rationale = truncateNarrativeRunes(redactEvidenceText(value.Rationale), 800)
		result = append(result, value)
	}
	return result
}

func totalProjectAllocation(values []projectAllocation) float64 {
	total := 0.0
	for _, value := range values {
		total += value.AllocationPercent
	}
	return math.Round(total*100) / 100
}

func rebalanceAttributionPercentages(values []projectAttribution, targetID int64, targetPercent float64) (map[int64]float64, bool) {
	targetPercent = math.Round(math.Max(0, math.Min(100, targetPercent))*100) / 100
	found := false
	othersTotal := 0.0
	for _, value := range values {
		if value.ID == targetID {
			found = true
			continue
		}
		othersTotal += math.Max(0, value.AllocationPercent)
	}
	if !found || len(values) == 0 {
		return nil, false
	}
	result := map[int64]float64{targetID: targetPercent}
	if len(values) == 1 {
		result[targetID] = 100
		return result, true
	}
	remaining := 100 - targetPercent
	assigned := 0.0
	otherIndex := 0
	for _, value := range values {
		if value.ID == targetID {
			continue
		}
		otherIndex++
		percentage := 0.0
		if otherIndex == len(values)-1 {
			percentage = math.Round((remaining-assigned)*100) / 100
		} else if othersTotal > 0 {
			percentage = math.Round(remaining*math.Max(0, value.AllocationPercent)/othersTotal*100) / 100
		} else {
			percentage = math.Round(remaining/float64(len(values)-1)*100) / 100
		}
		assigned += percentage
		result[value.ID] = percentage
	}
	return result, true
}

func validEvidenceReferences(values []string, allowed map[string]weeklyEvidenceCard) []string {
	result := make([]string, 0, len(values))
	seen := make(map[string]bool)
	for _, value := range values {
		value = strings.TrimSpace(value)
		if _, ok := allowed[value]; ok && !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	return result
}

func evidenceReference(card weeklyEvidenceCard) string {
	return card.SourceType + ":" + strconv.FormatInt(card.SourceID, 10)
}

func evidenceHash(values ...string) string {
	hash := sha256.Sum256([]byte(strings.Join(values, "\x00")))
	return hex.EncodeToString(hash[:])
}

func redactEvidenceText(value string) string {
	for _, pattern := range evidenceInstructionBlockPatterns {
		value = pattern.ReplaceAllString(value, " [系统指令已移除] ")
	}
	value = evidenceInstructionHeaderPattern.ReplaceAllString(value, "")
	value = redactReportContent(value)
	value = evidenceWindowsPathPattern.ReplaceAllString(value, "[本地路径已脱敏]")
	value = evidenceUnixPathPattern.ReplaceAllString(value, "[服务器路径已脱敏]")
	value = strings.Join(strings.Fields(value), " ")
	return strings.TrimSpace(value)
}

func deriveEvidenceCard(sourceType string, sourceID, userID int64, start time.Time, occurredAt time.Time, userText, assistantText string, toolJSON, eventJSON []byte, responseStatus int, sourceHash string) weeklyEvidenceCard {
	userText = truncateRunes(redactEvidenceText(userText), 2400)
	assistantText = truncateRunes(redactEvidenceText(assistantText), 3000)
	tools := extractToolNames(toolJSON, eventJSON)
	combined := strings.TrimSpace(userText + "\n" + assistantText + "\n" + strings.Join(tools, " "))
	card := weeklyEvidenceCard{
		UserID: userID, WeekStart: start.Format("2006-01-02"), SourceType: sourceType, SourceID: sourceID,
		SourceSHA256: sourceHash, ExtractorVersion: weeklyEvidenceExtractorVersion, OccurredAt: occurredAt,
		ProcessingStatus: "ignored", EvidenceKind: "intent", Beneficiary: inferBeneficiary(combined),
		TaskGoal: truncateNarrativeRunes(firstNonEmpty(userText, assistantText), 1000), UserAction: truncateNarrativeRunes(userText, 1200),
		Tools: tools, Deliverables: extractArtifactLabels(combined), Verification: extractVerificationSignals(combined),
		Summary: truncateNarrativeRunes(firstNonEmpty(assistantText, userText), 1400), RawMeta: map[string]any{"response_status": responseStatus},
	}
	if sourceType == "trajectory" {
		card.FileChanges = extractFileChangeLabels(eventJSON)
	}
	if responseStatus >= 400 || failurePattern.MatchString(combined) {
		card.EvidenceKind = "failure"
		card.FailureSummary = truncateNarrativeRunes(firstNonEmpty(assistantText, "轨迹执行失败或未完整结束"), 800)
	}
	if externalConfirmationPattern.MatchString(combined) {
		card.EvidenceKind, card.StageScore = "external_confirmation", 1
	} else if len(card.Verification) > 0 || verificationPattern.MatchString(combined) {
		card.EvidenceKind, card.StageScore = "verification", 0.9
	} else if completionPattern.MatchString(combined) || len(card.Deliverables) > 0 || len(card.FileChanges) > 0 {
		card.EvidenceKind, card.StageScore = "deliverable", 0.7
	} else if draftPattern.MatchString(combined) {
		card.EvidenceKind, card.StageScore = "draft", 0.45
	} else if designPattern.MatchString(combined) {
		card.EvidenceKind, card.StageScore = "design", 0.2
	}
	if card.EvidenceKind == "failure" {
		card.StageScore = 0
	}
	if len(tools) > 0 || len(card.FileChanges) > 0 || len(card.Verification) > 0 {
		card.EvidenceQuality = 1
	} else if len(card.Deliverables) > 0 {
		card.EvidenceQuality = 0.85
	} else if card.StageScore > 0 {
		card.EvidenceQuality = 0.6
	}
	if evidenceCandidatePattern.MatchString(combined) || len(tools) > 0 || len(card.Deliverables) > 0 || len(card.Verification) > 0 || len(card.FileChanges) > 0 {
		card.ProcessingStatus = "candidate"
	}
	return card
}

func inferBeneficiary(value string) string {
	lower := strings.ToLower(value)
	if strings.Contains(lower, "公众号") || strings.Contains(lower, "公开发布") || strings.Contains(lower, "public") {
		return "public"
	}
	if strings.Contains(lower, "老师") || strings.Contains(lower, "课题组") || strings.Contains(lower, "实验室") || strings.Contains(lower, "博士") || strings.Contains(lower, "advisor") || strings.Contains(lower, "lab") {
		return "advisor_lab"
	}
	if strings.Contains(lower, "帮助") || strings.Contains(lower, "协助") || strings.Contains(lower, "指导") || strings.Contains(lower, "同学") || strings.Contains(lower, "他人") || strings.Contains(lower, "peer") {
		return "peer"
	}
	return "self"
}

func extractToolNames(values ...[]byte) []string {
	seen := make(map[string]bool)
	var walk func(any, string)
	walk = func(value any, parent string) {
		switch item := value.(type) {
		case []any:
			for _, child := range item {
				walk(child, parent)
			}
		case map[string]any:
			for key, child := range item {
				lower := strings.ToLower(key)
				if (lower == "tool_name" || lower == "name") && (parent == "function" || lower == "tool_name") {
					if name, ok := child.(string); ok && name != "" {
						seen[truncateRunes(name, 80)] = true
					}
				}
				walk(child, lower)
			}
		}
	}
	for _, raw := range values {
		if len(raw) == 0 {
			continue
		}
		var value any
		if json.Unmarshal(raw, &value) == nil {
			walk(value, "")
		}
	}
	result := make([]string, 0, len(seen))
	for value := range seen {
		result = append(result, value)
	}
	sort.Strings(result)
	if len(result) > 20 {
		result = result[:20]
	}
	return result
}

func extractArtifactLabels(value string) []string {
	matches := artifactPattern.FindAllString(value, 20)
	return normalizeStringItems(matches, 20, 120)
}

func extractVerificationSignals(value string) []string {
	matches := verificationPattern.FindAllString(value, 12)
	return normalizeStringItems(matches, 12, 200)
}

func extractFileChangeLabels(raw []byte) []string {
	if len(raw) == 0 {
		return nil
	}
	return extractArtifactLabels(redactEvidenceText(string(raw)))
}

func (a *app) ensureWeeklyEvidenceCards(ctx context.Context, userID int64, start, end time.Time) (map[string]int, error) {
	counts := map[string]int{"conversation": 0, "trajectory": 0, "manual": 0, "candidate": 0, "ignored": 0}
	lastID := int64(0)
	for {
		rows, err := a.db.Query(ctx, `SELECT id,request_payload,COALESCE(assistant_text,''),COALESCE(tool_calls,'[]'::jsonb),COALESCE(final_response,'{}'::jsonb),completed_at,COALESCE(response_status,0)
			FROM conversation_archives WHERE user_id=$1 AND completed_at >= $2 AND completed_at < $3 AND id>$4 ORDER BY id LIMIT 500`, userID, start, end, lastID)
		if err != nil {
			return counts, err
		}
		pageCount := 0
		for rows.Next() {
			var id int64
			var requestPayload, toolCalls, finalResponse []byte
			var assistant string
			var completed time.Time
			var responseStatus int
			if err := rows.Scan(&id, &requestPayload, &assistant, &toolCalls, &finalResponse, &completed, &responseStatus); err != nil {
				continue
			}
			lastID, pageCount = id, pageCount+1
			var payload any
			_ = json.Unmarshal(requestPayload, &payload)
			userText := extractUserText(payload)
			card := deriveEvidenceCard("conversation", id, userID, start, completed, userText, assistant, toolCalls, finalResponse, responseStatus,
				evidenceHash(string(requestPayload), assistant, string(toolCalls), string(finalResponse), strconv.Itoa(responseStatus)))
			if err := a.upsertEvidenceCard(ctx, card); err != nil {
				rows.Close()
				return counts, err
			}
			counts["conversation"]++
			counts[card.ProcessingStatus]++
		}
		rows.Close()
		if pageCount < 500 {
			break
		}
	}

	lastID = 0
	for {
		rows, err := a.db.Query(ctx, `SELECT t.id,COALESCE(t.title,''),COALESCE(t.final_text,''),COALESCE(t.status,''),t.completed_at,COALESCE(t.content_sha256,''),
			COALESCE(jsonb_agg(jsonb_build_object('event_type',e.event_type,'role',e.role,'tool_name',e.tool_name,'status',e.status,'content',e.content) ORDER BY e.sequence_no) FILTER (WHERE e.id IS NOT NULL),'[]'::jsonb)
			FROM agent_trajectories t LEFT JOIN agent_trajectory_events e ON e.trajectory_id=t.id
			WHERE t.user_id=$1 AND t.completed_at >= $2 AND t.completed_at < $3 AND t.id>$4
			GROUP BY t.id ORDER BY t.id LIMIT 100`, userID, start, end, lastID)
		if err != nil {
			return counts, err
		}
		pageCount := 0
		for rows.Next() {
			var id int64
			var title, finalText, status, storedHash string
			var completed time.Time
			var events []byte
			if err := rows.Scan(&id, &title, &finalText, &status, &completed, &storedHash, &events); err != nil {
				continue
			}
			lastID, pageCount = id, pageCount+1
			userText := title + "\n" + extractTrajectoryUserText(events)
			responseStatus := 200
			if status != "completed" {
				responseStatus = 500
			}
			hash := strings.TrimSpace(storedHash)
			if len(hash) != 64 {
				hash = evidenceHash(title, finalText, status, string(events))
			}
			card := deriveEvidenceCard("trajectory", id, userID, start, completed, userText, finalText, events, events, responseStatus, hash)
			if err := a.upsertEvidenceCard(ctx, card); err != nil {
				rows.Close()
				return counts, err
			}
			counts["trajectory"]++
			counts[card.ProcessingStatus]++
		}
		rows.Close()
		if pageCount < 100 {
			break
		}
	}

	manualRows, err := a.db.Query(ctx, `SELECT id,item_id,title,description,deliverable_url,stage,complexity,personal_contribution,status,created_at
		FROM weekly_manual_evidence WHERE user_id=$1 AND week_start=$2 ORDER BY id`, userID, start.Format("2006-01-02"))
	if err != nil {
		return counts, err
	}
	for manualRows.Next() {
		var id int64
		var itemID int
		var title, description, deliverableURL, stage, status string
		var complexity, contribution float64
		var createdAt time.Time
		if manualRows.Scan(&id, &itemID, &title, &description, &deliverableURL, &stage, &complexity, &contribution, &status, &createdAt) != nil {
			continue
		}
		kind, stageScore := normalizeEvidenceStage(stage)
		quality := 0.6
		processingStatus := "candidate"
		if status == "approved" {
			quality = 1
		}
		if status == "rejected" {
			quality, processingStatus = 0, "ignored"
		}
		card := weeklyEvidenceCard{UserID: userID, WeekStart: start.Format("2006-01-02"), SourceType: "manual", SourceID: id,
			SourceSHA256: evidenceHash(title, description, deliverableURL, stage, status), ExtractorVersion: weeklyEvidenceExtractorVersion,
			OccurredAt: createdAt, ProcessingStatus: processingStatus, EvidenceKind: kind, Beneficiary: inferBeneficiary(title + " " + description),
			TaskGoal: redactEvidenceText(title), UserAction: redactEvidenceText(description), Deliverables: normalizeStringItems([]string{deliverableURL}, 1, 500),
			Summary: redactEvidenceText(description), StageScore: stageScore, EvidenceQuality: quality,
			RawMeta: map[string]any{"manual_status": status, "item_id": itemID, "complexity": complexity, "personal_contribution": contribution}}
		if err := a.upsertEvidenceCard(ctx, card); err != nil {
			manualRows.Close()
			return counts, err
		}
		counts["manual"]++
		counts[processingStatus]++
	}
	manualRows.Close()
	return counts, manualRows.Err()
}

func extractTrajectoryUserText(raw []byte) string {
	var events []map[string]any
	if json.Unmarshal(raw, &events) != nil {
		return ""
	}
	parts := make([]string, 0, 12)
	for _, event := range events {
		if event["role"] != "user" {
			continue
		}
		content, _ := json.Marshal(event["content"])
		parts = append(parts, extractUserTextFromRawJSON(content))
		if len(parts) == 12 {
			break
		}
	}
	return strings.Join(parts, "\n")
}

func extractUserTextFromRawJSON(raw []byte) string {
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return ""
	}
	return extractUserText(value)
}

func normalizeEvidenceStage(value string) (string, float64) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "accepted", "external_confirmation", "published":
		return "external_confirmation", 1
	case "verified", "verification", "tested":
		return "verification", 0.9
	case "delivered", "deliverable", "completed":
		return "deliverable", 0.7
	case "draft", "partial":
		return "draft", 0.45
	case "design", "analysis":
		return "design", 0.2
	default:
		return "intent", 0
	}
}

func (a *app) upsertEvidenceCard(ctx context.Context, card weeklyEvidenceCard) error {
	_, err := a.db.Exec(ctx, `INSERT INTO weekly_evidence_cards(user_id,week_start,source_type,source_id,source_sha256,extractor_version,occurred_at,processing_status,evidence_kind,beneficiary,task_goal,user_action,tools,file_changes,deliverables,verification,failure_summary,summary,stage_score,evidence_quality,raw_meta,error_message)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
		ON CONFLICT(user_id,week_start,source_type,source_id,extractor_version) DO UPDATE SET source_sha256=excluded.source_sha256,occurred_at=excluded.occurred_at,
		processing_status=CASE WHEN weekly_evidence_cards.source_sha256=excluded.source_sha256 AND weekly_evidence_cards.processing_status='extracted' THEN 'extracted' ELSE excluded.processing_status END,
		evidence_kind=excluded.evidence_kind,beneficiary=excluded.beneficiary,task_goal=excluded.task_goal,user_action=excluded.user_action,tools=excluded.tools,file_changes=excluded.file_changes,
		deliverables=excluded.deliverables,verification=excluded.verification,failure_summary=excluded.failure_summary,summary=excluded.summary,stage_score=excluded.stage_score,
		evidence_quality=excluded.evidence_quality,raw_meta=excluded.raw_meta,error_message=excluded.error_message,updated_at=now()`,
		card.UserID, card.WeekStart, card.SourceType, card.SourceID, card.SourceSHA256, card.ExtractorVersion, card.OccurredAt, card.ProcessingStatus,
		card.EvidenceKind, card.Beneficiary, card.TaskGoal, card.UserAction, jsonBytes(card.Tools), jsonBytes(card.FileChanges), jsonBytes(card.Deliverables),
		jsonBytes(card.Verification), card.FailureSummary, card.Summary, card.StageScore, card.EvidenceQuality, jsonBytes(card.RawMeta), card.ErrorMessage)
	return err
}

func (a *app) loadWeeklyEvidenceCards(ctx context.Context, userID int64, weekStart string) ([]weeklyEvidenceCard, error) {
	rows, err := a.db.Query(ctx, `SELECT id,user_id,week_start,source_type,source_id,source_sha256,extractor_version,occurred_at,processing_status,evidence_kind,beneficiary,
		task_goal,user_action,tools,file_changes,deliverables,verification,failure_summary,summary,stage_score,evidence_quality,raw_meta,COALESCE(error_message,'')
		FROM (SELECT DISTINCT ON (source_type,source_id) * FROM weekly_evidence_cards
			WHERE user_id=$1 AND week_start=$2 ORDER BY source_type,source_id,extractor_version DESC,id DESC) latest
		ORDER BY occurred_at,id`, userID, weekStart)
	if err != nil {
		return nil, err
	}
	return scanWeeklyEvidenceCards(rows)
}

func (a *app) loadWeeklyEvidenceCardsForWorkItems(ctx context.Context, userID int64, weekStart string, workItemIDs []int64, limit, offset int) ([]weeklyEvidenceCard, int, error) {
	if len(workItemIDs) == 0 {
		return []weeklyEvidenceCard{}, 0, nil
	}
	var total int
	if err := a.db.QueryRow(ctx, `SELECT COUNT(*) FROM weekly_evidence_cards c
		WHERE c.user_id=$1 AND c.week_start=$2
		AND EXISTS (SELECT 1 FROM weekly_work_item_sources s WHERE s.evidence_card_id=c.id AND s.work_item_id=ANY($3::bigint[]))`, userID, weekStart, workItemIDs).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := a.db.Query(ctx, `SELECT c.id,c.user_id,c.week_start,c.source_type,c.source_id,c.source_sha256,c.extractor_version,c.occurred_at,c.processing_status,c.evidence_kind,c.beneficiary,
		c.task_goal,c.user_action,c.tools,c.file_changes,c.deliverables,c.verification,c.failure_summary,c.summary,c.stage_score,c.evidence_quality,c.raw_meta,COALESCE(c.error_message,'')
		FROM weekly_evidence_cards c
		WHERE c.user_id=$1 AND c.week_start=$2
		AND EXISTS (SELECT 1 FROM weekly_work_item_sources s WHERE s.evidence_card_id=c.id AND s.work_item_id=ANY($3::bigint[]))
		ORDER BY c.occurred_at,c.id LIMIT $4 OFFSET $5`, userID, weekStart, workItemIDs, limit, offset)
	if err != nil {
		return nil, 0, err
	}
	cards, err := scanWeeklyEvidenceCards(rows)
	return cards, total, err
}

func scanWeeklyEvidenceCards(rows pgx.Rows) ([]weeklyEvidenceCard, error) {
	defer rows.Close()
	items := make([]weeklyEvidenceCard, 0)
	for rows.Next() {
		var item weeklyEvidenceCard
		var start time.Time
		var tools, files, deliverables, verification, rawMeta []byte
		if err := rows.Scan(&item.ID, &item.UserID, &start, &item.SourceType, &item.SourceID, &item.SourceSHA256, &item.ExtractorVersion, &item.OccurredAt,
			&item.ProcessingStatus, &item.EvidenceKind, &item.Beneficiary, &item.TaskGoal, &item.UserAction, &tools, &files, &deliverables, &verification,
			&item.FailureSummary, &item.Summary, &item.StageScore, &item.EvidenceQuality, &rawMeta, &item.ErrorMessage); err != nil {
			return nil, err
		}
		item.WeekStart = start.Format("2006-01-02")
		_ = json.Unmarshal(tools, &item.Tools)
		_ = json.Unmarshal(files, &item.FileChanges)
		_ = json.Unmarshal(deliverables, &item.Deliverables)
		_ = json.Unmarshal(verification, &item.Verification)
		_ = json.Unmarshal(rawMeta, &item.RawMeta)
		sanitizeWeeklyEvidenceCard(&item)
		items = append(items, item)
	}
	return items, rows.Err()
}

func sanitizeEvidenceStringItems(values []string) []string {
	result := make([]string, 0, len(values))
	seen := make(map[string]struct{})
	for _, value := range values {
		value = redactEvidenceText(value)
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}

func sanitizeWeeklyEvidenceCard(item *weeklyEvidenceCard) {
	item.TaskGoal = redactEvidenceText(item.TaskGoal)
	item.UserAction = redactEvidenceText(item.UserAction)
	item.FileChanges = sanitizeEvidenceStringItems(item.FileChanges)
	item.Deliverables = sanitizeEvidenceStringItems(item.Deliverables)
	item.Verification = sanitizeEvidenceStringItems(item.Verification)
	item.FailureSummary = redactEvidenceText(item.FailureSummary)
	item.Summary = redactEvidenceText(item.Summary)
	item.ErrorMessage = redactEvidenceText(item.ErrorMessage)
	if item.TaskGoal == "" && item.UserAction == "" && item.Summary == "" && len(item.Tools) == 0 && len(item.FileChanges) == 0 && len(item.Deliverables) == 0 && len(item.Verification) == 0 {
		item.ProcessingStatus = "ignored"
		item.EvidenceKind = "intent"
		item.StageScore = 0
		item.EvidenceQuality = 0
	}
}

func (a *app) sanitizeStoredEvidence(ctx context.Context) (int64, error) {
	tx, err := a.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `SELECT id,user_id,week_start,source_type,source_id,source_sha256,extractor_version,occurred_at,processing_status,evidence_kind,beneficiary,
		task_goal,user_action,tools,file_changes,deliverables,verification,failure_summary,summary,stage_score,evidence_quality,raw_meta,COALESCE(error_message,'')
		FROM weekly_evidence_cards ORDER BY id FOR UPDATE`)
	if err != nil {
		return 0, err
	}
	items, err := scanWeeklyEvidenceCards(rows)
	if err != nil {
		return 0, err
	}
	var updated int64
	for _, item := range items {
		result, updateErr := tx.Exec(ctx, `UPDATE weekly_evidence_cards SET processing_status=$2,evidence_kind=$3,task_goal=$4,user_action=$5,file_changes=$6,deliverables=$7,
			verification=$8,failure_summary=$9,summary=$10,stage_score=$11,evidence_quality=$12,error_message=$13,updated_at=now()
			WHERE id=$1 AND (processing_status,evidence_kind,task_goal,user_action,file_changes,deliverables,verification,failure_summary,summary,stage_score,evidence_quality,COALESCE(error_message,''))
			IS DISTINCT FROM ($2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12,$13)`,
			item.ID, item.ProcessingStatus, item.EvidenceKind, item.TaskGoal, item.UserAction, jsonBytes(item.FileChanges), jsonBytes(item.Deliverables), jsonBytes(item.Verification),
			item.FailureSummary, item.Summary, item.StageScore, item.EvidenceQuality, item.ErrorMessage)
		if updateErr != nil {
			return 0, updateErr
		}
		updated += result.RowsAffected()
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return updated, nil
}

func clusterEvidenceCards(cards []weeklyEvidenceCard) []evidenceCluster {
	clusters := make(map[string]*evidenceCluster)
	for _, card := range cards {
		if card.ProcessingStatus == "ignored" || card.EvidenceQuality <= 0 {
			continue
		}
		keyText := canonicalTaskKey(firstNonEmpty(card.TaskGoal, card.Summary))
		if keyText == "" {
			continue
		}
		key := evidenceHash(keyText)
		if card.SourceType == "manual" {
			key = evidenceHash("manual", strconv.FormatInt(card.SourceID, 10))
		}
		cluster := clusters[key]
		if cluster == nil {
			cluster = &evidenceCluster{Key: key, Title: truncateNarrativeRunes(card.TaskGoal, 240), Beneficiary: card.Beneficiary,
				Stage: card.EvidenceKind, StageScore: card.StageScore, Quality: card.EvidenceQuality, Contribution: 0.5, Complexity: 0.5,
				FirstSeenAt: card.OccurredAt, LastSeenAt: card.OccurredAt}
			clusters[key] = cluster
		}
		cluster.Cards = append(cluster.Cards, card)
		cluster.SourceRefs = append(cluster.SourceRefs, evidenceReference(card))
		cluster.SummaryParts = append(cluster.SummaryParts, card.Summary)
		if title := truncateNarrativeRunes(card.TaskGoal, 120); title != "" {
			cluster.CardTitles = append(cluster.CardTitles, title)
		}
		cluster.Deliverables = append(cluster.Deliverables, card.Deliverables...)
		cluster.Deliverables = append(cluster.Deliverables, card.FileChanges...)
		cluster.Verification = append(cluster.Verification, card.Verification...)
		if card.StageScore > cluster.StageScore {
			cluster.Stage, cluster.StageScore = card.EvidenceKind, card.StageScore
		}
		if card.EvidenceQuality > cluster.Quality {
			cluster.Quality = card.EvidenceQuality
		}
		if card.OccurredAt.Before(cluster.FirstSeenAt) {
			cluster.FirstSeenAt = card.OccurredAt
		}
		if card.OccurredAt.After(cluster.LastSeenAt) {
			cluster.LastSeenAt = card.OccurredAt
		}
	}
	result := make([]evidenceCluster, 0, len(clusters))
	for _, value := range clusters {
		value.Summary = joinClusterSummaries(value.SummaryParts)
		value.CardTitles = normalizeStringItems(value.CardTitles, 12, 120)
		value.SourceRefs = normalizeStringItems(value.SourceRefs, 10000, 100)
		value.Deliverables = normalizeStringItems(value.Deliverables, 30, 180)
		value.Verification = normalizeStringItems(value.Verification, 20, 240)
		value.Complexity = inferComplexity(*value)
		result = append(result, *value)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].FirstSeenAt.Before(result[j].FirstSeenAt) })
	return result
}

func canonicalTaskKey(value string) string {
	value = strings.ToLower(redactEvidenceText(value))
	runes := make([]rune, 0, 180)
	for _, char := range value {
		if (char >= 'a' && char <= 'z') || (char >= '0' && char <= '9') || (char >= '\u4e00' && char <= '\u9fff') {
			runes = append(runes, char)
		}
		if len(runes) == 180 {
			break
		}
	}
	return string(runes)
}

func inferComplexity(cluster evidenceCluster) float64 {
	signals := len(cluster.Cards) + len(cluster.Deliverables)*2 + len(cluster.Verification)*2
	switch {
	case signals >= 18:
		return 1
	case signals >= 9:
		return 0.75
	case signals >= 4:
		return 0.5
	default:
		return 0.25
	}
}

func normalizeBeneficiary(value, fallback string) string {
	switch value {
	case "self", "peer", "advisor_lab", "public":
		return value
	default:
		return fallback
	}
}

func normalizeContribution(value float64) float64 {
	if value <= 0 || math.IsNaN(value) || math.IsInf(value, 0) {
		return 0.5
	}
	options := []float64{0.25, 0.5, 0.75, 1}
	best := 0.5
	for _, option := range options {
		if math.Abs(value-option) < math.Abs(value-best) {
			best = option
		}
	}
	return best
}

func normalizeComplexity(value float64) float64 {
	return normalizeContribution(value)
}

func (a *app) classifyEvidenceClusters(ctx context.Context, template reportTemplateRecord, clusters []evidenceCluster, signals map[string]RuleSignalSet, calib CalibrationProfile) []classifiedCluster {
	rubrics := numericRubrics()
	result := make([]classifiedCluster, 0, len(clusters))
	for offset := 0; offset < len(clusters); offset += 20 {
		end := offset + 20
		if end > len(clusters) {
			end = len(clusters)
		}
		batch := clusters[offset:end]
		classified, err := a.classifyEvidenceClusterBatch(ctx, template, batch, signals)
		if err != nil {
			for _, cluster := range batch {
				fallback := fallbackClassifiedCluster(cluster, template.Definition)
				if fallback.ClassificationStatus == "" {
					fallback.ClassificationStatus = "fallback"
				}
				result = append(result, a.finalizeClusterScoring(cluster, fallback, signals, rubrics, calib))
			}
			continue
		}
		byKey := make(map[string]classifiedCluster)
		for _, item := range classified {
			item.ClassificationStatus = "ai"
			byKey[item.ClusterKey] = item
		}
		for _, cluster := range batch {
			item, ok := byKey[cluster.Key]
			if !ok {
				item = fallbackClassifiedCluster(cluster, template.Definition)
				if item.ClassificationStatus == "" {
					item.ClassificationStatus = "fallback"
				}
			}
			item.ClusterKey = cluster.Key
			item.Title = truncateNarrativeRunes(redactEvidenceText(firstNonEmpty(item.Title, cluster.Title)), 240)
			item.Summary = truncateNarrativeRunes(redactEvidenceText(firstNonEmpty(item.Summary, cluster.Summary)), 1800)
			item.Beneficiary = normalizeBeneficiary(item.Beneficiary, cluster.Beneficiary)
			item.EvidenceSignals = normalizeEvidenceSignals(item.EvidenceSignals, 12)
			item.StageReason = normalizeAttributionText(item.StageReason, 600)
			item.SelfCheck.StageSupportedBy = normalizeAttributionText(item.SelfCheck.StageSupportedBy, 300)
			item.SelfCheck.Risk = normalizeAttributionText(item.SelfCheck.Risk, 400)
			item.Allocations = validateProjectAllocations(item.Allocations, template.Definition)
			if allocation, contribution, complexity, ok := manualClusterParameters(cluster, template.Definition); ok {
				item.Allocations = []projectAllocation{allocation}
				item.PersonalContribution = contribution
				item.Complexity = complexity
				item.ClassificationStatus = "manual"
			}
			if len(item.Allocations) == 0 {
				item.Allocations = inferProjectAllocations(item.Title+" "+item.Summary, item.Beneficiary, template.Definition)
			}
			result = append(result, a.finalizeClusterScoring(cluster, item, signals, rubrics, calib))
		}
	}
	return result
}

// finalizeClusterScoring 把单条归因结果规整为最终档位：吸附 → 偏离校验 → 校准 → 置信度。
func (a *app) finalizeClusterScoring(cluster evidenceCluster, item classifiedCluster, signals map[string]RuleSignalSet, rubrics map[string]FieldRubric, calib CalibrationProfile) classifiedCluster {
	signal := signals[cluster.Key]
	flags := ValidateAttribution(cluster, signal, &item, rubrics)
	item.EvidenceQuality = ApplyCalibration(fieldEvidenceQuality, item.EvidenceQuality, calib, rubrics[fieldEvidenceQuality])
	item.PersonalContribution = ApplyCalibration(fieldPersonalContribution, item.PersonalContribution, calib, rubrics[fieldPersonalContribution])
	item.Complexity = ApplyCalibration(fieldComplexity, item.Complexity, calib, rubrics[fieldComplexity])
	item.Stage = ApplyStageCalibration(item.Stage, calib)
	confidence, factors := EvaluateConfidence(signal, item, flags, 0)
	item.Confidence = confidence
	item.ConfidenceFactors = factors
	item.RequiresManualReview = flagsRequireReview(flags) || confidence < confidenceMediumThreshold
	item.Signals = signal
	return item
}

func (a *app) classifyEvidenceClusterBatch(ctx context.Context, template reportTemplateRecord, clusters []evidenceCluster, signals map[string]RuleSignalSet) ([]classifiedCluster, error) {
	user := buildAttributionUserPrompt(template, clusters, signals)
	raw, err := a.callDeepSeekJSON(ctx, attributionSystemPrompt(), user, 9000)
	if err != nil {
		return nil, err
	}
	payload, err := decodeAttributionPayload(raw)
	if err != nil {
		return nil, err
	}
	allowed := make(map[string]bool)
	for _, cluster := range clusters {
		allowed[cluster.Key] = true
	}
	result := make([]classifiedCluster, 0, len(payload.Items))
	seen := make(map[string]bool)
	for _, item := range payload.Items {
		if !allowed[item.ClusterKey] || seen[item.ClusterKey] {
			continue
		}
		seen[item.ClusterKey] = true
		result = append(result, item)
	}
	return result, nil
}

func uniqueCardTools(cards []weeklyEvidenceCard) []string {
	values := make([]string, 0)
	for _, card := range cards {
		values = append(values, card.Tools...)
	}
	return normalizeStringItems(values, 30, 80)
}

func fallbackClassifiedCluster(cluster evidenceCluster, def reportTemplateDefinition) classifiedCluster {
	item := classifiedCluster{ClusterKey: cluster.Key, Title: cluster.Title, Summary: cluster.Summary, Beneficiary: cluster.Beneficiary,
		Stage: cluster.Stage, EvidenceQuality: cluster.Quality, PersonalContribution: 0.5, Complexity: cluster.Complexity,
		Allocations: inferProjectAllocations(cluster.Title+" "+cluster.Summary, cluster.Beneficiary, def)}
	if allocation, contribution, complexity, ok := manualClusterParameters(cluster, def); ok {
		item.Allocations = []projectAllocation{allocation}
		item.PersonalContribution = contribution
		item.Complexity = complexity
		item.ClassificationStatus = "manual"
	}
	return item
}

func manualClusterParameters(cluster evidenceCluster, def reportTemplateDefinition) (projectAllocation, float64, float64, bool) {
	for _, card := range cluster.Cards {
		if card.SourceType != "manual" {
			continue
		}
		itemID, _ := numberFromAny(card.RawMeta["item_id"])
		item, ok := templateItemMap(def)[int(itemID)]
		if !ok || item.ID == 1 {
			return projectAllocation{}, 0, 0, false
		}
		contribution, _ := numberFromAny(card.RawMeta["personal_contribution"])
		complexity, _ := numberFromAny(card.RawMeta["complexity"])
		return projectAllocation{
			ItemID:            item.ID,
			AllocationPercent: 100,
			MilestoneCode:     inferMilestoneCode(item.ID, strings.ToLower(cluster.Title+" "+cluster.Summary), item),
			UnitCount:         1,
			Rationale:         "成员补充证据明确选择该评分项目",
		}, normalizeContribution(contribution), normalizeComplexity(complexity), true
	}
	return projectAllocation{}, 0, 0, false
}

func numberFromAny(value any) (float64, bool) {
	switch typed := value.(type) {
	case float64:
		return typed, true
	case float32:
		return float64(typed), true
	case int:
		return float64(typed), true
	case int64:
		return float64(typed), true
	case json.Number:
		parsed, err := typed.Float64()
		return parsed, err == nil
	default:
		return 0, false
	}
}

func validateProjectAllocations(values []projectAllocation, def reportTemplateDefinition) []projectAllocation {
	allowed := templateItemMap(def)
	result := make([]projectAllocation, 0, len(values))
	for _, value := range values {
		item, ok := allowed[value.ItemID]
		if !ok || value.ItemID == 1 {
			continue
		}
		value.MilestoneCode = validateMilestoneCode(item, value.MilestoneCode)
		result = append(result, value)
	}
	return normalizeProjectAllocations(result)
}

func validateMilestoneCode(item reportTemplateItem, value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	for _, milestone := range item.Milestones {
		if milestone.Code == value {
			return value
		}
	}
	if _, ok := item.UnitPoints[value]; ok {
		return value
	}
	return ""
}

func inferProjectAllocations(text, beneficiary string, defs ...reportTemplateDefinition) []projectAllocation {
	lower := strings.ToLower(text)
	itemIDs := make([]int, 0, 4)
	add := func(itemID int) {
		for _, current := range itemIDs {
			if current == itemID {
				return
			}
		}
		itemIDs = append(itemIDs, itemID)
	}
	assistance := beneficiary == "peer" || beneficiary == "advisor_lab"
	if assistance {
		if strings.Contains(lower, "网站") || strings.Contains(lower, "前端") || strings.Contains(lower, "后端") || strings.Contains(lower, "web") || strings.Contains(lower, "api") {
			add(7)
		}
		if strings.Contains(lower, "返修") || strings.Contains(lower, "审稿意见") {
			add(10)
		} else if strings.Contains(lower, "论文") || strings.Contains(lower, "图表") || strings.Contains(lower, "paper") {
			add(8)
		}
		if strings.Contains(lower, "代码") || strings.Contains(lower, "实验") || strings.Contains(lower, "code") || strings.Contains(lower, "experiment") {
			add(9)
		}
		if strings.Contains(lower, "博士") {
			add(11)
		}
	}
	if strings.Contains(lower, "论文") || strings.Contains(lower, "latex") || strings.Contains(lower, "overleaf") || strings.Contains(lower, "投稿") {
		add(2)
	}
	if strings.Contains(lower, "实验") || strings.Contains(lower, "消融") || strings.Contains(lower, "训练") || strings.Contains(lower, "数据集") || strings.Contains(lower, "experiment") {
		add(3)
	}
	if strings.Contains(lower, "返修") || strings.Contains(lower, "回复信") || strings.Contains(lower, "审稿意见") {
		add(4)
	}
	if strings.Contains(lower, "组会") || strings.Contains(lower, "论文分享") || strings.Contains(lower, "阅读分享") {
		add(5)
	}
	if strings.Contains(lower, "专利") || strings.Contains(lower, "权利要求") {
		add(6)
	}
	if strings.Contains(lower, "ppt") || strings.Contains(lower, "基金") || strings.Contains(lower, "项目书") || strings.Contains(lower, "材料撰写") || strings.Contains(lower, "报告书") {
		add(12)
	}
	if strings.Contains(lower, "系统") || strings.Contains(lower, "工程") || strings.Contains(lower, "部署") || strings.Contains(lower, "数据库") || strings.Contains(lower, "插件") || strings.Contains(lower, "升级") || strings.Contains(lower, "build") || strings.Contains(lower, "deploy") {
		add(13)
	}
	if strings.Contains(lower, "小组长") || strings.Contains(lower, "任务分解") || strings.Contains(lower, "协调") {
		add(14)
	}
	if strings.Contains(lower, "审稿") && !strings.Contains(lower, "意见") {
		add(15)
	}
	if strings.Contains(lower, "公众号") && !strings.Contains(lower, "视频") {
		add(16)
	}
	if strings.Contains(lower, "公众号视频") || strings.Contains(lower, "视频剪辑") {
		add(17)
	}
	if strings.Contains(lower, "参会") || strings.Contains(lower, "会议报告") || strings.Contains(lower, "作报告") {
		add(18)
	}
	if strings.Contains(lower, "出差") || strings.Contains(lower, "现场调研") || strings.Contains(lower, "数据采集") {
		add(19)
	}
	if strings.Contains(lower, "竞赛") || strings.Contains(lower, "比赛") || strings.Contains(lower, "获奖") {
		add(20)
	}
	if strings.Contains(lower, "指导学生") || strings.Contains(lower, "开题") || strings.Contains(lower, "中期") || strings.Contains(lower, "答辩") {
		add(21)
	}
	if len(itemIDs) == 0 && evidenceCandidatePattern.MatchString(lower) {
		add(13)
	}
	if len(itemIDs) == 0 {
		return nil
	}
	percent := 100 / float64(len(itemIDs))
	values := make([]projectAllocation, 0, len(itemIDs))
	var def reportTemplateDefinition
	if len(defs) > 0 {
		def = defs[0]
	}
	items := templateItemMap(def)
	for _, itemID := range itemIDs {
		code := inferMilestoneCode(itemID, lower, items[itemID])
		values = append(values, projectAllocation{ItemID: itemID, AllocationPercent: percent, MilestoneCode: code, UnitCount: 1, Rationale: "根据任务关键词和交付信号进行保守归因"})
	}
	return normalizeProjectAllocations(values)
}

func inferMilestoneCode(itemID int, text string, item reportTemplateItem) string {
	candidates := map[int][][2]string{
		2: {{"outline", "大纲"}, {"cn_draft", "中文初稿"}, {"key_figures", "图表"}, {"en_draft", "英文初稿"}, {"first_submission", "投稿"}, {"effective_communication", "沟通"}},
		3: {{"design", "实验设计"}, {"data_prep", "数据"}, {"implementation", "实现"}, {"run", "运行"}, {"analysis", "结果分析"}, {"ablation_reproduction", "消融"}},
		4: {{"review_decomposition", "意见"}, {"text_revision", "文字修改"}, {"figure_revision", "图表"}, {"extra_experiment", "补充实验"}, {"response_letter", "回复信"}, {"resubmission", "重投"}},
		5: {{"completed_share", "分享"}}, 6: {{"claims", "权利要求"}, {"submission_response", "提交"}},
		13: {{"verified_module", "测试通过"}}, 14: {{"completed_cycle", "推进"}}, 15: {{"submitted_review", "审稿"}},
		16: {{"published_article", "发布"}}, 17: {{"published_video", "发布"}}, 18: {{"presentation", "作报告"}, {"attendance", "参会"}},
		19: {{"technical_trip", "出差"}}, 21: {{"major_milestone", "答辩"}, {"general_guidance", "指导"}},
	}
	for _, candidate := range candidates[itemID] {
		if strings.Contains(text, candidate[1]) {
			return validateMilestoneCode(item, candidate[0])
		}
	}
	return ""
}

func (a *app) buildWeeklyWorkItems(ctx context.Context, userID int64, start time.Time, template reportTemplateRecord) ([]weeklyWorkItem, error) {
	cards, err := a.loadWeeklyEvidenceCards(ctx, userID, start.Format("2006-01-02"))
	if err != nil {
		return nil, err
	}
	clusters := clusterEvidenceCards(cards)
	cached, err := a.loadWorkItemDigests(ctx, userID, start.Format("2006-01-02"))
	if err != nil {
		return nil, err
	}
	rules := a.scoringRuleSet(ctx)
	calib := a.calibrationProfile(ctx)
	prevFingerprints := a.previousWeekFingerprints(ctx, userID, start)
	signals := make(map[string]RuleSignalSet, len(clusters))
	for _, cluster := range clusters {
		signals[cluster.Key] = BuildRuleSignals(cluster.Cards, rules, cluster.Key, prevFingerprints)
	}
	pending := make([]evidenceCluster, 0, len(clusters))
	digests := make(map[string]string, len(clusters))
	fingerprints := make([]string, 0, len(clusters))
	for _, cluster := range clusters {
		digest := clusterEvidenceDigest(cluster, template.ID)
		digests[cluster.Key] = digest
		if cached[cluster.Key] == digest {
			fingerprints = append(fingerprints, cluster.Key)
			continue
		}
		pending = append(pending, cluster)
	}
	classified := a.classifyEvidenceClusters(ctx, template, pending, signals, calib)
	clusterMap := make(map[string]evidenceCluster)
	for _, cluster := range clusters {
		clusterMap[cluster.Key] = cluster
	}
	for _, item := range classified {
		cluster, ok := clusterMap[item.ClusterKey]
		if !ok || len(item.Allocations) == 0 {
			continue
		}
		fingerprints = append(fingerprints, item.ClusterKey)
		if _, err := a.upsertWeeklyWorkItem(ctx, userID, start, cluster, digests[item.ClusterKey], item); err != nil {
			return nil, err
		}
	}
	if len(fingerprints) == 0 {
		_, _ = a.db.Exec(ctx, `DELETE FROM weekly_work_items WHERE user_id=$1 AND week_start=$2 AND reviewed_at IS NULL`, userID, start.Format("2006-01-02"))
	} else {
		_, _ = a.db.Exec(ctx, `DELETE FROM weekly_work_items WHERE user_id=$1 AND week_start=$2 AND reviewed_at IS NULL AND NOT(fingerprint=ANY($3))`, userID, start.Format("2006-01-02"), fingerprints)
	}
	return a.loadWeeklyWorkItems(ctx, userID, start.Format("2006-01-02"))
}

func clusterEvidenceDigest(cluster evidenceCluster, templateID int64) string {
	parts := []string{strconv.FormatInt(templateID, 10), cluster.Key}
	for _, card := range cluster.Cards {
		parts = append(parts, evidenceReference(card)+":"+card.SourceSHA256+":"+strconv.Itoa(card.ExtractorVersion))
	}
	sort.Strings(parts[2:])
	return evidenceHash(parts...)
}

func (a *app) loadWorkItemDigests(ctx context.Context, userID int64, weekStart string) (map[string]string, error) {
	rows, err := a.db.Query(ctx, `SELECT fingerprint,evidence_digest FROM weekly_work_items WHERE user_id=$1 AND week_start=$2 AND classification_status IN ('ai','manual')`, userID, weekStart)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make(map[string]string)
	for rows.Next() {
		var fingerprint, digest string
		if err := rows.Scan(&fingerprint, &digest); err != nil {
			return nil, err
		}
		result[fingerprint] = digest
	}
	return result, rows.Err()
}

func (a *app) upsertWeeklyWorkItem(ctx context.Context, userID int64, start time.Time, cluster evidenceCluster, evidenceDigest string, classified classifiedCluster) (int64, error) {
	kind, stageScore := normalizeEvidenceStage(classified.Stage)
	status := "in_progress"
	if stageScore >= 0.7 {
		status = "completed"
	} else if stageScore == 0 {
		status = "intent"
	}
	anomalyFlags := classified.AnomalyFlags
	if len(anomalyFlags) == 0 {
		anomalyFlags = []string{}
	}
	confidenceFactors := classified.ConfidenceFactors
	if confidenceFactors == nil {
		confidenceFactors = map[string]float64{}
	}
	var workItemID int64
	err := a.db.QueryRow(ctx, `INSERT INTO weekly_work_items(user_id,week_start,fingerprint,evidence_digest,classification_status,title,summary,beneficiary,status,stage,stage_score,evidence_quality,
		ai_stage,ai_stage_score,ai_evidence_quality,ai_personal_contribution,personal_contribution,ai_complexity,complexity,
		rule_signals,confidence,confidence_factors,anomaly_flags,requires_manual_review,engine_version,deliverables,verification,first_seen_at,last_seen_at)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29)
		ON CONFLICT(user_id,week_start,fingerprint) DO UPDATE SET evidence_digest=excluded.evidence_digest,classification_status=excluded.classification_status,title=excluded.title,summary=excluded.summary,beneficiary=excluded.beneficiary,status=excluded.status,
		stage=excluded.stage,stage_score=excluded.stage_score,evidence_quality=excluded.evidence_quality,
		ai_stage=excluded.ai_stage,ai_stage_score=excluded.ai_stage_score,ai_evidence_quality=excluded.ai_evidence_quality,ai_personal_contribution=excluded.ai_personal_contribution,
		personal_contribution=CASE WHEN weekly_work_items.reviewed_at IS NULL THEN excluded.personal_contribution ELSE weekly_work_items.personal_contribution END,
		ai_complexity=excluded.ai_complexity,complexity=CASE WHEN weekly_work_items.reviewed_at IS NULL THEN excluded.complexity ELSE weekly_work_items.complexity END,
		rule_signals=excluded.rule_signals,confidence=excluded.confidence,confidence_factors=excluded.confidence_factors,
		anomaly_flags=excluded.anomaly_flags,requires_manual_review=excluded.requires_manual_review,engine_version=excluded.engine_version,
		deliverables=excluded.deliverables,verification=excluded.verification,first_seen_at=LEAST(weekly_work_items.first_seen_at,excluded.first_seen_at),
		last_seen_at=GREATEST(weekly_work_items.last_seen_at,excluded.last_seen_at),updated_at=now() RETURNING id`,
		userID, start.Format("2006-01-02"), cluster.Key, evidenceDigest, firstNonEmpty(classified.ClassificationStatus, "fallback"), classified.Title, classified.Summary, classified.Beneficiary, status, kind, stageScore,
		classified.EvidenceQuality,
		classified.AIStage, classified.AIStageScore, classified.AIEvidenceQuality,
		classified.AIPersonalContribution, classified.PersonalContribution, classified.AIComplexity, classified.Complexity,
		jsonBytes(classified.Signals), classified.Confidence, jsonBytes(confidenceFactors), anomalyFlags, classified.RequiresManualReview, a.scoringEngineVersion(),
		jsonBytes(cluster.Deliverables), jsonBytes(cluster.Verification), cluster.FirstSeenAt, cluster.LastSeenAt).Scan(&workItemID)
	if err != nil {
		return 0, err
	}
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_work_item_sources WHERE work_item_id=$1`, workItemID)
	for _, card := range cluster.Cards {
		_, err = a.db.Exec(ctx, `INSERT INTO weekly_work_item_sources(work_item_id,evidence_card_id,is_primary) VALUES($1,$2,$3) ON CONFLICT DO NOTHING`, workItemID, card.ID, card.ID == cluster.Cards[0].ID)
		if err != nil {
			return 0, err
		}
		_, _ = a.db.Exec(ctx, `UPDATE weekly_evidence_cards SET processing_status='extracted',updated_at=now() WHERE id=$1`, card.ID)
	}
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_project_attributions WHERE work_item_id=$1 AND reviewed_at IS NULL`, workItemID)
	attributionFlags := classified.AnomalyFlags
	if len(attributionFlags) == 0 {
		attributionFlags = []string{}
	}
	for _, allocation := range classified.Allocations {
		_, err = a.db.Exec(ctx, `INSERT INTO weekly_project_attributions(work_item_id,item_id,allocation_percent,ai_allocation_percent,milestone_code,ai_milestone_code,unit_count,ai_unit_count,rationale,confidence,anomaly_flags,requires_manual_review,engine_version)
			VALUES($1,$2,$3,$3,$4,$4,$5,$5,$6,$7,$8,$9,$10)
			ON CONFLICT(work_item_id,item_id) DO UPDATE SET ai_allocation_percent=excluded.ai_allocation_percent,ai_milestone_code=excluded.ai_milestone_code,ai_unit_count=excluded.ai_unit_count,
			allocation_percent=CASE WHEN weekly_project_attributions.reviewed_at IS NULL THEN excluded.allocation_percent ELSE weekly_project_attributions.allocation_percent END,
			milestone_code=CASE WHEN weekly_project_attributions.reviewed_at IS NULL THEN excluded.milestone_code ELSE weekly_project_attributions.milestone_code END,
			unit_count=CASE WHEN weekly_project_attributions.reviewed_at IS NULL THEN excluded.unit_count ELSE weekly_project_attributions.unit_count END,
			rationale=excluded.rationale,confidence=excluded.confidence,anomaly_flags=excluded.anomaly_flags,
			requires_manual_review=excluded.requires_manual_review,engine_version=excluded.engine_version,updated_at=now()`,
			workItemID, allocation.ItemID, allocation.AllocationPercent, allocation.MilestoneCode, allocation.UnitCount, allocation.Rationale,
			classified.Confidence, attributionFlags, classified.RequiresManualReview, a.scoringEngineVersion())
		if err != nil {
			return 0, err
		}
	}
	return workItemID, nil
}

func (a *app) loadWeeklyWorkItems(ctx context.Context, userID int64, weekStart string) ([]weeklyWorkItem, error) {
	rows, err := a.db.Query(ctx, `SELECT id,user_id,week_start,fingerprint,evidence_digest,classification_status,title,summary,beneficiary,status,stage,stage_score,evidence_quality,ai_personal_contribution,
		personal_contribution,ai_complexity,complexity,deliverables,verification,first_seen_at,last_seen_at,reviewed_by,reviewed_at,
		ai_stage,ai_stage_score,ai_evidence_quality,rule_signals,confidence,confidence_factors,anomaly_flags,requires_manual_review,engine_version
		FROM weekly_work_items WHERE user_id=$1 AND week_start=$2 ORDER BY first_seen_at,id`, userID, weekStart)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]weeklyWorkItem, 0)
	for rows.Next() {
		var item weeklyWorkItem
		var start time.Time
		var deliverables, verification, ruleSignals, confidenceFactors []byte
		if err := rows.Scan(&item.ID, &item.UserID, &start, &item.Fingerprint, &item.EvidenceDigest, &item.ClassificationStatus, &item.Title, &item.Summary, &item.Beneficiary, &item.Status, &item.Stage,
			&item.StageScore, &item.EvidenceQuality, &item.AIPersonalContribution, &item.PersonalContribution, &item.AIComplexity, &item.Complexity,
			&deliverables, &verification, &item.FirstSeenAt, &item.LastSeenAt, &item.ReviewedBy, &item.ReviewedAt,
			&item.AIStage, &item.AIStageScore, &item.AIEvidenceQuality, &ruleSignals, &item.Confidence, &confidenceFactors,
			&item.AnomalyFlags, &item.RequiresManualReview, &item.EngineVersion); err != nil {
			return nil, err
		}
		item.WeekStart = start.Format("2006-01-02")
		_ = json.Unmarshal(deliverables, &item.Deliverables)
		_ = json.Unmarshal(verification, &item.Verification)
		_ = json.Unmarshal(ruleSignals, &item.RuleSignals)
		_ = json.Unmarshal(confidenceFactors, &item.ConfidenceFactors)
		item.SourceRefs, _ = a.loadWorkItemSourceRefs(ctx, item.ID)
		item.Attributions, _ = a.loadWorkItemAttributions(ctx, item.ID)
		items = append(items, item)
	}
	return items, rows.Err()
}

func (a *app) loadWorkItemSourceRefs(ctx context.Context, workItemID int64) ([]string, error) {
	rows, err := a.db.Query(ctx, `SELECT c.source_type,c.source_id FROM weekly_work_item_sources s JOIN weekly_evidence_cards c ON c.id=s.evidence_card_id WHERE s.work_item_id=$1 ORDER BY c.occurred_at,c.id`, workItemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]string, 0)
	for rows.Next() {
		var sourceType string
		var sourceID int64
		if rows.Scan(&sourceType, &sourceID) == nil {
			items = append(items, sourceType+":"+strconv.FormatInt(sourceID, 10))
		}
	}
	return items, rows.Err()
}

func (a *app) loadWorkItemAttributions(ctx context.Context, workItemID int64) ([]projectAttribution, error) {
	rows, err := a.db.Query(ctx, `SELECT id,work_item_id,item_id,allocation_percent,ai_allocation_percent,milestone_code,ai_milestone_code,unit_count,ai_unit_count,
		score_component,rationale,confidence,anomaly_flags,requires_manual_review,engine_version,reviewed_by,reviewed_at
		FROM weekly_project_attributions WHERE work_item_id=$1 ORDER BY item_id`, workItemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]projectAttribution, 0)
	for rows.Next() {
		var item projectAttribution
		if err := rows.Scan(&item.ID, &item.WorkItemID, &item.ItemID, &item.AllocationPercent, &item.AIAllocationPercent, &item.MilestoneCode, &item.AIMilestoneCode,
			&item.UnitCount, &item.AIUnitCount, &item.ScoreComponent, &item.Rationale, &item.Confidence, &item.AnomalyFlags,
			&item.RequiresManualReview, &item.EngineVersion, &item.ReviewedBy, &item.ReviewedAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func workItemsForProject(values []weeklyWorkItem, itemID int) ([]weeklyWorkItem, []reportAttributionScoreInput) {
	items := make([]weeklyWorkItem, 0)
	inputs := make([]reportAttributionScoreInput, 0)
	for _, value := range values {
		for _, attribution := range value.Attributions {
			if attribution.ItemID != itemID || attribution.AllocationPercent <= 0 {
				continue
			}
			items = append(items, value)
			inputs = append(inputs, reportAttributionScoreInput{AttributionID: attribution.ID, WorkItemID: value.ID, Title: value.Title, Stage: value.Stage,
				StageScore: value.StageScore, EvidenceQuality: value.EvidenceQuality, PersonalContribution: value.PersonalContribution,
				AllocationPercent: attribution.AllocationPercent, Complexity: value.Complexity, MilestoneCode: attribution.MilestoneCode, UnitCount: attribution.UnitCount})
		}
	}
	return items, inputs
}

func (a *app) updateAttributionScoreComponents(ctx context.Context, inputs []reportAttributionScoreInput, breakdown reportScoreBreakdown) {
	byWork := make(map[int64]float64)
	for _, component := range breakdown.Components {
		byWork[component.WorkItemID] += component.Score
	}
	for _, input := range inputs {
		if input.AttributionID > 0 {
			_, _ = a.db.Exec(ctx, `UPDATE weekly_project_attributions SET score_component=$2,updated_at=now() WHERE id=$1`, input.AttributionID, byWork[input.WorkItemID])
		}
	}
}

func scanManualEvidence(row rowScanner) (weeklyManualEvidence, error) {
	var item weeklyManualEvidence
	var start time.Time
	err := row.Scan(&item.ID, &item.UserID, &start, &item.ItemID, &item.Title, &item.Description, &item.DeliverableURL, &item.Stage,
		&item.Complexity, &item.PersonalContribution, &item.Status, &item.SubmittedBy, &item.ReviewedBy, &item.ReviewComment,
		&item.ReviewedAt, &item.CreatedAt, &item.UpdatedAt)
	item.WeekStart = start.Format("2006-01-02")
	return item, err
}

func validManualEvidenceURL(value string) bool {
	if strings.TrimSpace(value) == "" {
		return true
	}
	parsed, err := url.Parse(value)
	return err == nil && (parsed.Scheme == "https" || parsed.Scheme == "http") && parsed.Host != ""
}

func parseManualEvidenceBody(r *http.Request) (weeklyManualEvidence, error) {
	var body weeklyManualEvidence
	if err := readJSON(r, &body, 256<<10); err != nil {
		return body, errors.New("补充证据内容无效")
	}
	body.Title = truncateNarrativeRunes(redactEvidenceText(body.Title), 240)
	body.Description = truncateNarrativeRunes(redactEvidenceText(body.Description), 5000)
	body.DeliverableURL = truncateRunes(strings.TrimSpace(body.DeliverableURL), 1000)
	if body.ItemID < 2 || body.ItemID > 21 || body.Title == "" || body.Description == "" || !validManualEvidenceURL(body.DeliverableURL) {
		return body, errors.New("补充证据字段无效")
	}
	body.Stage, _ = normalizeEvidenceStage(body.Stage)
	body.Complexity = normalizeComplexity(body.Complexity)
	body.PersonalContribution = normalizeContribution(body.PersonalContribution)
	return body, nil
}

func evidenceWorkItemsForProject(values []weeklyWorkItem, itemID int, requestedWorkItemIDs []int64) []weeklyWorkItem {
	if itemID == 0 {
		return values
	}
	requested := make(map[int64]struct{}, len(requestedWorkItemIDs))
	for _, id := range requestedWorkItemIDs {
		if id > 0 {
			requested[id] = struct{}{}
		}
	}
	result := make([]weeklyWorkItem, 0)
	for _, value := range values {
		include := false
		if itemID == 1 {
			_, include = requested[value.ID]
		} else {
			for _, attribution := range value.Attributions {
				if attribution.ItemID == itemID && attribution.AllocationPercent > 0 {
					include = true
					break
				}
			}
		}
		if include {
			result = append(result, value)
		}
	}
	return result
}

func parseEvidenceBundleFilter(r *http.Request) (int, []int64, error) {
	itemValue := strings.TrimSpace(r.URL.Query().Get("item_id"))
	if itemValue == "" {
		return 0, nil, nil
	}
	itemID, err := strconv.Atoi(itemValue)
	if err != nil || itemID < 1 || itemID > 21 {
		return 0, nil, errors.New("评分项目编号无效")
	}
	requested := make([]int64, 0)
	seen := make(map[int64]struct{})
	for _, value := range strings.Split(r.URL.Query().Get("work_item_ids"), ",") {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		id, parseErr := strconv.ParseInt(value, 10, 64)
		if parseErr != nil || id <= 0 {
			return 0, nil, errors.New("任务编号无效")
		}
		if _, ok := seen[id]; !ok {
			seen[id] = struct{}{}
			requested = append(requested, id)
		}
	}
	return itemID, requested, nil
}

func parseEvidenceBundlePagination(r *http.Request, filtered bool) (int, int, error) {
	if !filtered {
		return 0, 0, nil
	}
	page, pageSize := 1, 100
	if value := strings.TrimSpace(r.URL.Query().Get("page")); value != "" {
		parsed, err := strconv.Atoi(value)
		if err != nil || parsed < 1 || parsed > 1000000 {
			return 0, 0, errors.New("证据页码无效")
		}
		page = parsed
	}
	if value := strings.TrimSpace(r.URL.Query().Get("page_size")); value != "" {
		parsed, err := strconv.Atoi(value)
		if err != nil || parsed < 1 || parsed > 200 {
			return 0, 0, errors.New("证据分页大小无效")
		}
		pageSize = parsed
	}
	return pageSize, (page - 1) * pageSize, nil
}

func (a *app) evidenceBundle(ctx context.Context, userID int64, weekStart string, itemID int, requestedWorkItemIDs []int64, cardLimit, cardOffset int) (map[string]any, error) {
	allWorkItems, err := a.loadWeeklyWorkItems(ctx, userID, weekStart)
	if err != nil {
		return nil, err
	}
	workItems := evidenceWorkItemsForProject(allWorkItems, itemID, requestedWorkItemIDs)
	workItemIDs := make([]int64, 0, len(workItems))
	for _, item := range workItems {
		workItemIDs = append(workItemIDs, item.ID)
	}
	var cards []weeklyEvidenceCard
	cardTotal := 0
	if itemID == 0 {
		cards, err = a.loadWeeklyEvidenceCards(ctx, userID, weekStart)
		cardTotal = len(cards)
	} else {
		cards, cardTotal, err = a.loadWeeklyEvidenceCardsForWorkItems(ctx, userID, weekStart, workItemIDs, cardLimit, cardOffset)
	}
	if err != nil {
		return nil, err
	}
	manualSQL := `SELECT id,user_id,week_start,item_id,title,description,deliverable_url,stage,complexity,personal_contribution,status,submitted_by,reviewed_by,review_comment,reviewed_at,created_at,updated_at
		FROM weekly_manual_evidence WHERE user_id=$1 AND week_start=$2`
	args := []any{userID, weekStart}
	if itemID > 0 {
		manualSQL += ` AND item_id=$3`
		args = append(args, itemID)
	}
	manualSQL += ` ORDER BY created_at DESC`
	rows, err := a.db.Query(ctx, manualSQL, args...)
	if err != nil {
		return nil, err
	}
	manual := make([]weeklyManualEvidence, 0)
	for rows.Next() {
		item, scanErr := scanManualEvidence(rows)
		if scanErr != nil {
			rows.Close()
			return nil, scanErr
		}
		manual = append(manual, item)
	}
	rows.Close()
	result := map[string]any{"cards": cards, "card_total": cardTotal, "card_offset": cardOffset, "work_items": workItems, "manual_evidence": manual}
	var definitionRaw []byte
	if err := a.db.QueryRow(ctx, `SELECT t.definition FROM weekly_reports r JOIN weekly_report_templates t ON t.id=r.template_id WHERE r.user_id=$1 AND r.week_start=$2`, userID, weekStart).Scan(&definitionRaw); err == nil {
		var definition reportTemplateDefinition
		if json.Unmarshal(definitionRaw, &definition) == nil {
			result["template_definition"] = definition
		}
	}
	return result, rows.Err()
}

func (a *app) weeklyEvidenceByWeek(w http.ResponseWriter, r *http.Request, id identity, start time.Time, parts []string) {
	weekStart := start.Format("2006-01-02")
	if len(parts) == 2 && parts[1] == "evidence" && r.Method == http.MethodGet {
		itemID, workItemIDs, err := parseEvidenceBundleFilter(r)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		cardLimit, cardOffset, err := parseEvidenceBundlePagination(r, itemID > 0)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		bundle, err := a.evidenceBundle(r.Context(), id.ID, weekStart, itemID, workItemIDs, cardLimit, cardOffset)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取评分依据失败")
			return
		}
		writeJSON(w, http.StatusOK, bundle)
		return
	}
	if len(parts) == 3 && parts[1] == "evidence" && parts[2] == "manual" && r.Method == http.MethodPost {
		body, err := parseManualEvidenceBody(r)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		var evidenceID int64
		err = a.db.QueryRow(r.Context(), `INSERT INTO weekly_manual_evidence(user_id,week_start,item_id,title,description,deliverable_url,stage,complexity,personal_contribution,submitted_by)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$1) RETURNING id`, id.ID, weekStart, body.ItemID, body.Title, body.Description, body.DeliverableURL, body.Stage, body.Complexity, body.PersonalContribution).Scan(&evidenceID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "保存补充证据失败")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_manual_evidence_create", map[string]any{"evidence_id": evidenceID, "week_start": weekStart, "item_id": body.ItemID})
		item, _ := scanManualEvidence(a.db.QueryRow(r.Context(), `SELECT id,user_id,week_start,item_id,title,description,deliverable_url,stage,complexity,personal_contribution,status,submitted_by,reviewed_by,review_comment,reviewed_at,created_at,updated_at FROM weekly_manual_evidence WHERE id=$1`, evidenceID))
		writeJSON(w, http.StatusCreated, item)
		return
	}
	if len(parts) == 4 && parts[1] == "evidence" && parts[2] == "manual" && r.Method == http.MethodPatch {
		evidenceID, err := strconv.ParseInt(parts[3], 10, 64)
		if err != nil || evidenceID <= 0 {
			writeAPIError(w, http.StatusBadRequest, "补充证据编号无效")
			return
		}
		body, err := parseManualEvidenceBody(r)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		result, err := a.db.Exec(r.Context(), `UPDATE weekly_manual_evidence SET item_id=$1,title=$2,description=$3,deliverable_url=$4,stage=$5,complexity=$6,personal_contribution=$7,updated_at=now()
			WHERE id=$8 AND user_id=$9 AND week_start=$10 AND status='pending'`, body.ItemID, body.Title, body.Description, body.DeliverableURL, body.Stage, body.Complexity, body.PersonalContribution, evidenceID, id.ID, weekStart)
		if err != nil || result.RowsAffected() == 0 {
			writeAPIError(w, http.StatusConflict, "只能修改本人待审核的补充证据")
			return
		}
		_ = a.auditEvent(r.Context(), id.ID, "weekly_manual_evidence_update", map[string]any{"evidence_id": evidenceID, "week_start": weekStart})
		item, _ := scanManualEvidence(a.db.QueryRow(r.Context(), `SELECT id,user_id,week_start,item_id,title,description,deliverable_url,stage,complexity,personal_contribution,status,submitted_by,reviewed_by,review_comment,reviewed_at,created_at,updated_at FROM weekly_manual_evidence WHERE id=$1`, evidenceID))
		writeJSON(w, http.StatusOK, item)
		return
	}
	writeAPIError(w, http.StatusNotFound, "评分依据接口不存在")
}

func (a *app) adminReportEvidence(w http.ResponseWriter, r *http.Request, actor identity, reportID int64, parts []string) bool {
	var userID int64
	var weekStart time.Time
	var templateID *int64
	if err := a.db.QueryRow(r.Context(), `SELECT user_id,week_start,template_id FROM weekly_reports WHERE id=$1`, reportID).Scan(&userID, &weekStart, &templateID); err != nil {
		writeAPIError(w, http.StatusNotFound, "周报不存在")
		return true
	}
	if len(parts) == 2 && parts[1] == "evidence" && r.Method == http.MethodGet {
		itemID, workItemIDs, err := parseEvidenceBundleFilter(r)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return true
		}
		cardLimit, cardOffset, err := parseEvidenceBundlePagination(r, itemID > 0)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return true
		}
		bundle, err := a.evidenceBundle(r.Context(), userID, weekStart.Format("2006-01-02"), itemID, workItemIDs, cardLimit, cardOffset)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取评分依据失败")
			return true
		}
		writeJSON(w, http.StatusOK, bundle)
		return true
	}
	if len(parts) == 3 && parts[1] == "attributions" && r.Method == http.MethodPatch {
		attributionID, err := strconv.ParseInt(parts[2], 10, 64)
		if err != nil || attributionID <= 0 {
			writeAPIError(w, http.StatusBadRequest, "归因编号无效")
			return true
		}
		var body struct {
			AllocationPercent    float64 `json:"allocation_percent"`
			PersonalContribution float64 `json:"personal_contribution"`
			Complexity           float64 `json:"complexity"`
			Stage                string  `json:"stage"`
			EvidenceQuality      float64 `json:"evidence_quality"`
			MilestoneCode        string  `json:"milestone_code"`
			UnitCount            float64 `json:"unit_count"`
			Rationale            string  `json:"rationale"`
		}
		if readJSON(r, &body, 64<<10) != nil || body.AllocationPercent < 0 || body.AllocationPercent > 100 || body.EvidenceQuality < 0 || body.EvidenceQuality > 1 || body.UnitCount < 0 || body.UnitCount > 1000 {
			writeAPIError(w, http.StatusBadRequest, "归因参数无效")
			return true
		}
		body.PersonalContribution = normalizeContribution(body.PersonalContribution)
		body.Complexity = normalizeComplexity(body.Complexity)
		body.Stage, _ = normalizeEvidenceStage(body.Stage)
		_, bodyStageScore := normalizeEvidenceStage(body.Stage)
		body.EvidenceQuality = math.Round(body.EvidenceQuality*100) / 100
		body.Rationale = truncateNarrativeRunes(redactEvidenceText(body.Rationale), 800)
		tx, err := a.db.Begin(r.Context())
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "保存归因失败")
			return true
		}
		defer func() { _ = tx.Rollback(r.Context()) }()
		var workItemID int64
		var attributionItemID int
		err = tx.QueryRow(r.Context(), `SELECT w.id,a.item_id FROM weekly_project_attributions a JOIN weekly_work_items w ON w.id=a.work_item_id
			WHERE a.id=$1 AND w.user_id=$2 AND w.week_start=$3 FOR UPDATE OF w,a`, attributionID, userID, weekStart.Format("2006-01-02")).Scan(&workItemID, &attributionItemID)
		if err != nil {
			writeAPIError(w, http.StatusNotFound, "归因记录不存在")
			return true
		}
		if templateID == nil {
			writeAPIError(w, http.StatusConflict, "该周报没有可用评分模板")
			return true
		}
		template, err := a.loadReportTemplate(r.Context(), *templateID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取评分模板失败")
			return true
		}
		templateItem, ok := templateItemMap(template.Definition)[attributionItemID]
		if !ok {
			writeAPIError(w, http.StatusConflict, "评分项目不存在")
			return true
		}
		body.MilestoneCode = validateMilestoneCode(templateItem, body.MilestoneCode)
		if body.UnitCount == 0 {
			body.UnitCount = 1
		}
		rows, err := tx.Query(r.Context(), `SELECT id,work_item_id,item_id,allocation_percent,ai_allocation_percent,milestone_code,unit_count,score_component,rationale,reviewed_by,reviewed_at
			FROM weekly_project_attributions WHERE work_item_id=$1 ORDER BY id FOR UPDATE`, workItemID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取归因比例失败")
			return true
		}
		attributions := make([]projectAttribution, 0)
		for rows.Next() {
			var value projectAttribution
			if err := rows.Scan(&value.ID, &value.WorkItemID, &value.ItemID, &value.AllocationPercent, &value.AIAllocationPercent, &value.MilestoneCode,
				&value.UnitCount, &value.ScoreComponent, &value.Rationale, &value.ReviewedBy, &value.ReviewedAt); err != nil {
				rows.Close()
				writeAPIError(w, http.StatusInternalServerError, "读取归因比例失败")
				return true
			}
			attributions = append(attributions, value)
		}
		rows.Close()
		balanced, ok := rebalanceAttributionPercentages(attributions, attributionID, body.AllocationPercent)
		if !ok {
			writeAPIError(w, http.StatusNotFound, "归因记录不存在")
			return true
		}
		for id, percentage := range balanced {
			rationale := ""
			if id == attributionID {
				rationale = body.Rationale
			}
			if id == attributionID {
				if _, err := tx.Exec(r.Context(), `UPDATE weekly_project_attributions SET allocation_percent=$1,milestone_code=$2,unit_count=$3,rationale=COALESCE(NULLIF($4,''),rationale),reviewed_by=$5,reviewed_at=now(),updated_at=now() WHERE id=$6`, percentage, body.MilestoneCode, body.UnitCount, rationale, actor.ID, id); err != nil {
					writeAPIError(w, http.StatusInternalServerError, "保存归因比例失败")
					return true
				}
				continue
			}
			if _, err := tx.Exec(r.Context(), `UPDATE weekly_project_attributions SET allocation_percent=$1,reviewed_by=$2,reviewed_at=now(),updated_at=now() WHERE id=$3`, percentage, actor.ID, id); err != nil {
				writeAPIError(w, http.StatusInternalServerError, "保存归因比例失败")
				return true
			}
		}
		if _, err := tx.Exec(r.Context(), `UPDATE weekly_work_items SET stage=$2,stage_score=$3,evidence_quality=$4,personal_contribution=$5,complexity=$6,reviewed_by=$7,reviewed_at=now(),updated_at=now() WHERE id=$1`, workItemID, body.Stage, bodyStageScore, body.EvidenceQuality, body.PersonalContribution, body.Complexity, actor.ID); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "保存任务评分参数失败")
			return true
		}
		if err := tx.Commit(r.Context()); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "保存归因失败")
			return true
		}
		_ = a.auditEvent(r.Context(), actor.ID, "weekly_attribution_review", map[string]any{"report_id": reportID, "attribution_id": attributionID, "work_item_id": workItemID})
		bundle, _ := a.evidenceBundle(r.Context(), userID, weekStart.Format("2006-01-02"), attributionItemID, nil, 100, 0)
		writeJSON(w, http.StatusOK, bundle)
		return true
	}
	if len(parts) == 4 && parts[1] == "evidence" && (parts[3] == "approve" || parts[3] == "reject") && r.Method == http.MethodPost {
		evidenceID, err := strconv.ParseInt(parts[2], 10, 64)
		if err != nil || evidenceID <= 0 {
			writeAPIError(w, http.StatusBadRequest, "补充证据编号无效")
			return true
		}
		var body struct {
			Comment string `json:"comment"`
		}
		_ = readJSON(r, &body, 32<<10)
		status := "approved"
		if parts[3] == "reject" {
			status = "rejected"
		}
		var evidenceItemID int
		err = a.db.QueryRow(r.Context(), `UPDATE weekly_manual_evidence SET status=$1,reviewed_by=$2,review_comment=$3,reviewed_at=now(),updated_at=now()
			WHERE id=$4 AND user_id=$5 AND week_start=$6 RETURNING item_id`, status, actor.ID, truncateNarrativeRunes(redactEvidenceText(body.Comment), 1000), evidenceID, userID, weekStart.Format("2006-01-02")).Scan(&evidenceItemID)
		if err != nil {
			writeAPIError(w, http.StatusNotFound, "补充证据不存在")
			return true
		}
		_ = a.auditEvent(r.Context(), actor.ID, "weekly_manual_evidence_"+status, map[string]any{"report_id": reportID, "evidence_id": evidenceID})
		bundle, _ := a.evidenceBundle(r.Context(), userID, weekStart.Format("2006-01-02"), evidenceItemID, nil, 100, 0)
		writeJSON(w, http.StatusOK, bundle)
		return true
	}
	if len(parts) == 2 && parts[1] == "rescore" && r.Method == http.MethodPost {
		job, err := a.enqueueReportJob(r.Context(), userID, weekStart, actor.ID, false)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "重新评分任务创建失败")
			return true
		}
		_ = a.auditEvent(r.Context(), actor.ID, "weekly_report_rescore_requested", map[string]any{"report_id": reportID, "job_id": job.ID})
		writeJSON(w, http.StatusAccepted, map[string]any{"job": job})
		return true
	}
	return false
}

func ensureMonday(value time.Time) error {
	if value.Weekday() != time.Monday {
		return fmt.Errorf("周次必须从周一开始")
	}
	return nil
}

func reportWeekAndUser(ctx context.Context, scanner rowScanner) (int64, time.Time, error) {
	var userID int64
	var weekStart time.Time
	err := scanner.Scan(&userID, &weekStart)
	return userID, weekStart, err
}

var _ = pgx.ErrNoRows
