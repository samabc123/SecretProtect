package main

import (
	"context"
	"encoding/json"
	"log"
	"sync"
	"time"
)

// 服务端确定性信号引擎。
// 作用：从证据卡计算出 AI 打分的下限基线（server_stage / server_quality）与校验基准
// （证据条数、强信号、跨天跨度、连续性等）。全部为内存计算，不额外调用模型。

// 强信号类型。AI 若要把档位上调超过一档，至少需要命中其中一条。
const (
	signalFileChange           = "file_change"
	signalTestAssertion        = "test_assertion"
	signalExternalConfirmation = "external_confirmation"
	signalToolResult           = "tool_result"
)

// RuleSet 是版本化的评分规则集，默认取值与 v4 现网硬编码行为完全一致。
type RuleSet struct {
	Version               int                `json:"version"`
	StageScores           map[string]float64 `json:"stage_scores"`
	QualityRules          map[string]float64 `json:"quality_rules"`
	StrongSignals         []string           `json:"strong_signals"`
	VolumeSaturationCards int                `json:"volume_saturation_cards"`
	ComplexityThresholds  map[string]float64 `json:"complexity_thresholds"`
	ComplexityValues      map[string]float64 `json:"complexity_values"`
	// VolumeFactorWeight 为 0 时证据量微调因子恒为 1，不影响任何分数。
	VolumeFactorWeight float64 `json:"volume_factor_weight"`
	// ComplexityWeight 为上限分分支的复杂度权重，1 表示与 v4 一致（直接相乘）。
	ComplexityWeight float64 `json:"complexity_weight"`
	// MilestoneComplexityWeight 为里程碑/单位分分支的复杂度权重，0 表示与 v4 一致（不纳入复杂度）。
	MilestoneComplexityWeight float64 `json:"milestone_complexity_weight"`
	// SaturationKnee 为软饱和拐点占上限分的比例，1 表示与 v4 一致（硬截断）。
	SaturationKnee   float64           `json:"saturation_knee"`
	StagePatterns    map[string]string `json:"stage_patterns"`
	CandidatePattern string            `json:"candidate_pattern"`
	FailurePattern   string            `json:"failure_pattern"`
}

// DefaultRuleSet 返回与 migrations/007 种子数据一致的默认规则集。
// 任何加载失败场景都必须回退到这里，保证默认行为不劣化。
func DefaultRuleSet() RuleSet {
	return RuleSet{
		Version: 1,
		StageScores: map[string]float64{
			"external_confirmation": 1,
			"verification":          0.9,
			"deliverable":           0.7,
			"draft":                 0.45,
			"design":                0.2,
			"intent":                0,
		},
		QualityRules: map[string]float64{
			"strong":           1,
			"deliverable_only": 0.85,
			"stage_only":       0.6,
			"none":             0,
		},
		StrongSignals:             []string{signalFileChange, signalTestAssertion, signalExternalConfirmation, signalToolResult},
		VolumeSaturationCards:     3,
		ComplexityThresholds:      map[string]float64{"high": 18, "medium": 9, "low": 4},
		ComplexityValues:          map[string]float64{"high": 1, "medium": 0.75, "low": 0.5, "base": 0.25},
		VolumeFactorWeight:        0,
		ComplexityWeight:          1,
		MilestoneComplexityWeight: 0,
		SaturationKnee:            1,
		StagePatterns:             map[string]string{},
		CandidatePattern:          "",
		FailurePattern:            "",
	}
}

// RuleSignalSet 是单个任务簇的确定性信号快照，既喂给模型也用于校验。
type RuleSignalSet struct {
	CardCount          int      `json:"card_count"`
	SourceKinds        []string `json:"source_kinds"`
	SpanDays           int      `json:"span_days"`
	FileChangeCount    int      `json:"file_change_count"`
	DeliverableCount   int      `json:"deliverable_count"`
	VerificationCount  int      `json:"verification_count"`
	ExternalConfirm    bool     `json:"external_confirm"`
	ToolKinds          []string `json:"tool_kinds"`
	StrongSignals      []string `json:"strong_signals"`
	HasFailure         bool     `json:"has_failure"`
	ContinuesPrevWeek  bool     `json:"continues_previous_week"`
	ServerStage        string   `json:"server_stage"`
	ServerStageScore   float64  `json:"server_stage_score"`
	ServerQuality      float64  `json:"server_quality"`
	VolumeFactor       float64  `json:"volume_factor"`
	VolumeSaturation   int      `json:"volume_saturation"`
	ComplexityBaseline float64  `json:"complexity_baseline"`
}

// BuildRuleSignals 从证据卡集合构建确定性信号。
func BuildRuleSignals(cards []weeklyEvidenceCard, rules RuleSet, fingerprint string, prevFingerprints map[string]bool) RuleSignalSet {
	signal := RuleSignalSet{
		CardCount:     len(cards),
		SourceKinds:   make([]string, 0, 3),
		ToolKinds:     make([]string, 0),
		StrongSignals: make([]string, 0, 4),
		ServerStage:   "intent",
	}
	stageCodes := make([]string, 0, len(cards))
	seenSource := make(map[string]bool)
	seenTool := make(map[string]bool)
	var first, last time.Time
	for _, card := range cards {
		if !seenSource[card.SourceType] {
			seenSource[card.SourceType] = true
			signal.SourceKinds = append(signal.SourceKinds, card.SourceType)
		}
		for _, tool := range card.Tools {
			if tool != "" && !seenTool[tool] {
				seenTool[tool] = true
				signal.ToolKinds = append(signal.ToolKinds, tool)
			}
		}
		signal.FileChangeCount += len(card.FileChanges)
		signal.DeliverableCount += len(card.Deliverables)
		signal.VerificationCount += len(card.Verification)
		if card.EvidenceKind == "external_confirmation" {
			signal.ExternalConfirm = true
		}
		if card.EvidenceKind == "failure" {
			signal.HasFailure = true
		}
		stageCodes = append(stageCodes, card.EvidenceKind)
		if first.IsZero() || card.OccurredAt.Before(first) {
			first = card.OccurredAt
		}
		if last.IsZero() || card.OccurredAt.After(last) {
			last = card.OccurredAt
		}
	}
	if !first.IsZero() && !last.IsZero() {
		signal.SpanDays = int(last.Sub(first).Hours()/24) + 1
	}
	if signal.FileChangeCount > 0 {
		signal.StrongSignals = append(signal.StrongSignals, signalFileChange)
	}
	if signal.VerificationCount > 0 {
		signal.StrongSignals = append(signal.StrongSignals, signalTestAssertion)
	}
	if signal.ExternalConfirm {
		signal.StrongSignals = append(signal.StrongSignals, signalExternalConfirmation)
	}
	if len(signal.ToolKinds) > 0 {
		signal.StrongSignals = append(signal.StrongSignals, signalToolResult)
	}
	signal.ServerStage, signal.ServerStageScore = SortStageByScore(stageCodes)
	signal.ServerQuality = serverQualityFromCards(cards, rules)
	signal.VolumeFactor = volumeFactor(len(cards), rules)
	signal.VolumeSaturation = rules.VolumeSaturationCards
	signal.ComplexityBaseline = inferClusterComplexity(len(cards), signal.DeliverableCount, signal.VerificationCount, rules)
	if len(prevFingerprints) > 0 && fingerprint != "" {
		signal.ContinuesPrevWeek = prevFingerprints[fingerprint]
	}
	return signal
}

// serverQualityFromCards 复刻 v4 的证据质量规则：先按卡取最高，再吸附到五档阶梯。
func serverQualityFromCards(cards []weeklyEvidenceCard, rules RuleSet) float64 {
	best := 0.0
	for _, card := range cards {
		value := ruleValue(rules.QualityRules, "none", 0)
		switch {
		case len(card.Tools) > 0 || len(card.FileChanges) > 0 || len(card.Verification) > 0:
			value = ruleValue(rules.QualityRules, "strong", 1)
		case len(card.Deliverables) > 0:
			value = ruleValue(rules.QualityRules, "deliverable_only", 0.85)
		case card.StageScore > 0:
			value = ruleValue(rules.QualityRules, "stage_only", 0.6)
		}
		if value > best {
			best = value
		}
	}
	if best <= 0 {
		return 0
	}
	snapped, _, _ := SnapToLadder(evidenceQualityRubric(), best)
	return snapped
}

// inferClusterComplexity 复刻 v4 inferComplexity 的阈值逻辑，改为规则集驱动。
func inferClusterComplexity(cardCount, deliverableCount, verificationCount int, rules RuleSet) float64 {
	signals := float64(cardCount + deliverableCount*2 + verificationCount*2)
	thresholds := rules.ComplexityThresholds
	values := rules.ComplexityValues
	switch {
	case signals >= ruleValue(thresholds, "high", 18):
		return ruleValue(values, "high", 1)
	case signals >= ruleValue(thresholds, "medium", 9):
		return ruleValue(values, "medium", 0.75)
	case signals >= ruleValue(thresholds, "low", 4):
		return ruleValue(values, "low", 0.5)
	default:
		return ruleValue(values, "base", 0.25)
	}
}

// volumeFactor 证据量微调因子。权重为 0（默认）时恒为 1，不改变任何分数。
func volumeFactor(cardCount int, rules RuleSet) float64 {
	if rules.VolumeFactorWeight <= 0 {
		return 1
	}
	saturation := rules.VolumeSaturationCards
	if saturation <= 0 {
		saturation = 3
	}
	coverage := float64(cardCount) / float64(saturation)
	if coverage > 1 {
		coverage = 1
	}
	return 1 + rules.VolumeFactorWeight*(coverage-1)
}

// HasStrongSignal 判断是否命中指定强信号。
func (s RuleSignalSet) HasStrongSignal(name string) bool {
	for _, value := range s.StrongSignals {
		if value == name {
			return true
		}
	}
	return false
}

// StrongSignalCount 返回命中的强信号数量。
func (s RuleSignalSet) StrongSignalCount() int {
	return len(s.StrongSignals)
}

// ruleValue 读取规则表中的数值，缺失或非法时使用回退值。
func ruleValue(values map[string]float64, key string, fallback float64) float64 {
	value, ok := values[key]
	if !ok || value < 0 {
		return fallback
	}
	return value
}

// ---------------------------------------------------------------------------
// 规则集加载与缓存
// ---------------------------------------------------------------------------

var (
	scoringRuleSetMu     sync.RWMutex
	scoringRuleSetCache  RuleSet
	scoringRuleSetLoaded bool
)

// scoringRuleSet 返回当前生效的规则集，加载失败时回退到默认规则集。
func (a *app) scoringRuleSet(ctx context.Context) RuleSet {
	scoringRuleSetMu.RLock()
	if scoringRuleSetLoaded {
		value := scoringRuleSetCache
		scoringRuleSetMu.RUnlock()
		return value
	}
	scoringRuleSetMu.RUnlock()

	scoringRuleSetMu.Lock()
	defer scoringRuleSetMu.Unlock()
	if scoringRuleSetLoaded {
		return scoringRuleSetCache
	}
	loaded, err := a.loadScoringRuleSet(ctx)
	if err != nil {
		log.Printf("scoring rule set load failed, fallback to defaults: %v", err)
		loaded = DefaultRuleSet()
	}
	scoringRuleSetCache = sanitizeRuleSet(loaded)
	scoringRuleSetLoaded = true
	return scoringRuleSetCache
}

// refreshScoringRuleSetCache 在管理员修改规则集后使缓存失效。
func refreshScoringRuleSetCache() {
	scoringRuleSetMu.Lock()
	scoringRuleSetLoaded = false
	scoringRuleSetMu.Unlock()
}

func (a *app) loadScoringRuleSet(ctx context.Context) (RuleSet, error) {
	var raw []byte
	err := a.db.QueryRow(ctx, `SELECT definition FROM weekly_scoring_rule_sets WHERE status='active' ORDER BY version DESC LIMIT 1`).Scan(&raw)
	if err != nil {
		return RuleSet{}, err
	}
	var parsed RuleSet
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return RuleSet{}, err
	}
	return parsed, nil
}

// sanitizeRuleSet 用默认值补齐缺失字段并做范围约束，避免脏数据导致评分异常。
func sanitizeRuleSet(value RuleSet) RuleSet {
	base := DefaultRuleSet()
	if value.Version <= 0 {
		value.Version = base.Version
	}
	if len(value.StageScores) == 0 {
		value.StageScores = base.StageScores
	} else {
		for _, code := range []string{"intent", "design", "draft", "deliverable", "verification", "external_confirmation"} {
			if _, ok := value.StageScores[code]; !ok {
				value.StageScores[code] = base.StageScores[code]
			}
		}
	}
	if len(value.QualityRules) == 0 {
		value.QualityRules = base.QualityRules
	} else {
		for _, key := range []string{"strong", "deliverable_only", "stage_only", "none"} {
			if _, ok := value.QualityRules[key]; !ok {
				value.QualityRules[key] = base.QualityRules[key]
			}
		}
	}
	if len(value.StrongSignals) == 0 {
		value.StrongSignals = base.StrongSignals
	}
	if value.VolumeSaturationCards <= 0 {
		value.VolumeSaturationCards = base.VolumeSaturationCards
	}
	if len(value.ComplexityThresholds) == 0 {
		value.ComplexityThresholds = base.ComplexityThresholds
	}
	if len(value.ComplexityValues) == 0 {
		value.ComplexityValues = base.ComplexityValues
	}
	if value.VolumeFactorWeight < 0 || value.VolumeFactorWeight > 1 {
		value.VolumeFactorWeight = base.VolumeFactorWeight
	}
	if value.ComplexityWeight <= 0 || value.ComplexityWeight > 1 {
		value.ComplexityWeight = base.ComplexityWeight
	}
	if value.MilestoneComplexityWeight < 0 || value.MilestoneComplexityWeight > 1 {
		value.MilestoneComplexityWeight = base.MilestoneComplexityWeight
	}
	if value.SaturationKnee <= 0 || value.SaturationKnee > 1 {
		value.SaturationKnee = base.SaturationKnee
	}
	return value
}
