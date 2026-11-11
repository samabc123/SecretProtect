package main

import (
	"context"
	"math"
	"sort"
)

// 评分引擎 v5。
//
// 与 v4 的关系：公式骨架保持一致，默认规则参数下逐分支结果与 v4 完全相同
// （复杂度权重、饱和拐点等新增参数均以「v4 等价」为默认值）。
// 新增的能力是：档位吸附、偏离纠偏、异常检测、多因子置信度与 per-field 校准。

// 异常标记。
const (
	flagAllocSumMismatch     = "ALLOC_SUM_MISMATCH"
	flagStageQualityConflict = "STAGE_QUALITY_CONFLICT"
	flagStageDeliverable     = "STAGE_DELIVERABLE_CONFLICT"
	flagDeviationUnsupported = "AI_DEVIATION_UNSUPPORTED"
	flagMilestoneDup         = "MILESTONE_DUP_SUPPRESSED"
	flagScoreNearCap         = "SCORE_NEAR_CAP"
	flagCrossItemOverAlloc   = "CROSS_ITEM_OVER_ALLOC"
	flagHistoryOutlier       = "HISTORY_OUTLIER"
	flagLowEvidenceHighScore = "LOW_EVIDENCE_HIGH_SCORE"
	flagFallbackClassified   = "FALLBACK_CLASSIFICATION"
)

// 触发人工复核的异常标记集合。
var reviewTriggeringFlags = map[string]bool{
	flagDeviationUnsupported: true,
	flagHistoryOutlier:       true,
	flagLowEvidenceHighScore: true,
	flagFallbackClassified:   true,
}

const (
	confidenceHighThreshold   = 0.75
	confidenceMediumThreshold = 0.50
	historyOutlierZScore      = 2.5
	minHistoryForOutlier      = 3
)

// 置信度权重，合计为 1。
const (
	weightEvidenceVolume   = 0.25
	weightSourceDiversity  = 0.20
	weightSelfCheck        = 0.20
	weightRuleValidation   = 0.20
	weightHistoryAlignment = 0.15
)

// CalibrationField 是单个字段的校准参数：adjusted = value*Scale + Bias。
// 对 stage 字段，Bias 解释为档位序号偏移（取整数），Scale 不生效。
type CalibrationField struct {
	Bias    float64 `json:"bias"`
	Scale   float64 `json:"scale"`
	Enabled bool    `json:"enabled"`
}

// CalibrationProfile 是全部可校准字段的参数集合。
type CalibrationProfile struct {
	Fields map[string]CalibrationField `json:"fields"`
}

// DefaultCalibrationProfile 返回恒等校准（不改变任何取值）。
func DefaultCalibrationProfile() CalibrationProfile {
	fields := make(map[string]CalibrationField, len(validCalibrationFields()))
	for _, field := range validCalibrationFields() {
		fields[field] = CalibrationField{Scale: 1, Enabled: true}
	}
	return CalibrationProfile{Fields: fields}
}

// Get 返回字段的校准参数，未配置或已停用时返回恒等参数。
func (c CalibrationProfile) Get(field string) CalibrationField {
	if value, ok := c.Fields[field]; ok && value.Enabled {
		if value.Scale == 0 {
			value.Scale = 1
		}
		return value
	}
	return CalibrationField{Scale: 1, Enabled: true}
}

// ScoreContext 是计算单个项目分数所需的全部上下文。
type ScoreContext struct {
	Item    reportTemplateItem
	Inputs  []reportAttributionScoreInput
	Signals map[int64]RuleSignalSet
	// Confidences 为 work_item_id 到任务级置信度的映射，用于汇总项目级置信度。
	Confidences map[int64]float64
	Calib       CalibrationProfile
	PrevScores  []float64
	Rules       RuleSet
	EngineVer   string
}

// workItemSignalMap 汇总任务 ID 到确定性信号的映射。
func workItemSignalMap(values []weeklyWorkItem) map[int64]RuleSignalSet {
	result := make(map[int64]RuleSignalSet, len(values))
	for _, value := range values {
		result[value.ID] = value.RuleSignals
	}
	return result
}

// workItemConfidenceMap 汇总任务 ID 到任务级置信度的映射。
func workItemConfidenceMap(values []weeklyWorkItem) map[int64]float64 {
	result := make(map[int64]float64, len(values))
	for _, value := range values {
		result[value.ID] = value.Confidence
	}
	return result
}

// calculateProjectScore 按配置的引擎版本选择算分实现。
// v4 走旧公式，v5 走档位化引擎；两者返回结构一致，调用方无需区分。
func (a *app) calculateProjectScore(ctx context.Context, item reportTemplateItem, inputs []reportAttributionScoreInput, signals map[int64]RuleSignalSet, confidences map[int64]float64, history []float64) (float64, reportScoreBreakdown) {
	if a.scoringEngineVersion() == "v4" {
		return calculateProjectScore(item, inputs)
	}
	return CalculateProjectScoreV5(ScoreContext{
		Item: item, Inputs: inputs, Signals: signals, Confidences: confidences,
		Calib: a.calibrationProfile(ctx), PrevScores: history, Rules: a.scoringRuleSet(ctx), EngineVer: "v5",
	})
}

// CalculateProjectScoreV5 计算项目建议分并返回完整明细。
func CalculateProjectScoreV5(ctx ScoreContext) (float64, reportScoreBreakdown) {
	item := ctx.Item
	rules := ctx.Rules
	breakdown := reportScoreBreakdown{
		Formula:       item.ScoringFormula,
		MaxScore:      item.MaxScore,
		EvidenceCount: len(ctx.Inputs),
		Components:    make([]reportScoreComponent, 0, len(ctx.Inputs)),
	}
	if len(ctx.Inputs) == 0 {
		breakdown.Formula = firstNonEmpty(breakdown.Formula, "无可核验证据，建议分为0")
		return 0, breakdown
	}

	milestones := make(map[string]float64, len(item.Milestones))
	for _, value := range item.Milestones {
		milestones[value.Code] = value.Points
	}

	type scoredComponent struct {
		component reportScoreComponent
		milestone bool
	}
	scored := make([]scoredComponent, 0, len(ctx.Inputs))
	flags := make([]string, 0, 4)

	for _, value := range ctx.Inputs {
		signal := ctx.Signals[value.WorkItemID]
		stage := clamp01(value.StageScore, 0)
		quality := clamp01(value.EvidenceQuality, 0)
		contribution := clamp01(value.PersonalContribution, 0.5)
		allocation := math.Max(0, math.Min(100, value.AllocationPercent)) / 100
		complexity := clamp01(value.Complexity, 0.5)
		units := value.UnitCount
		if units <= 0 {
			units = 1
		}
		volume := signal.VolumeFactor
		if volume <= 0 {
			volume = 1
		}

		base := 0.0
		componentScore := 0.0
		isMilestone := false
		switch {
		case value.MilestoneCode != "" && hasKey(milestones, value.MilestoneCode):
			base = milestones[value.MilestoneCode]
			isMilestone = true
			componentScore = base * stage * quality * contribution * allocation * milestoneComplexityFactor(complexity, rules) * volume
		case value.MilestoneCode != "" && hasKey(item.UnitPoints, value.MilestoneCode):
			base = item.UnitPoints[value.MilestoneCode]
			componentScore = base * units * stage * quality * contribution * allocation * milestoneComplexityFactor(complexity, rules) * volume
		case item.MaxScore != nil:
			base = *item.MaxScore
			componentScore = base * stage * quality * contribution * allocation * maxScoreComplexityFactor(complexity, rules) * volume
		}
		if componentScore < 0 {
			componentScore = 0
		}

		componentFlags := make([]string, 0, 2)
		if item.MaxScore != nil && *item.MaxScore > 0 && signal.CardCount > 0 &&
			signal.CardCount <= 1 && componentScore >= 0.7**item.MaxScore {
			componentFlags = append(componentFlags, flagLowEvidenceHighScore)
		}

		scored = append(scored, scoredComponent{
			component: reportScoreComponent{
				WorkItemID: value.WorkItemID, Title: value.Title, MilestoneCode: value.MilestoneCode, BasePoints: base,
				StageScore: stage, EvidenceQuality: quality, Contribution: contribution, Allocation: value.AllocationPercent,
				Complexity: complexity, UnitCount: units, Score: componentScore,
				VolumeFactor: volume, AnomalyFlags: componentFlags,
			},
			milestone: isMilestone,
		})
		flags = appendUniqueFlag(flags, componentFlags...)
	}

	// 同一里程碑被多个任务命中时取最高分，消除输入顺序依赖（v4 取首次出现）。
	bestIndex := make(map[string]int, len(scored))
	for index, value := range scored {
		if !value.milestone {
			continue
		}
		if current, ok := bestIndex[value.component.MilestoneCode]; !ok || value.component.Score > scored[current].component.Score {
			bestIndex[value.component.MilestoneCode] = index
		}
	}
	suppressed := make(map[int]bool, len(scored))
	for index, value := range scored {
		if !value.milestone {
			continue
		}
		if bestIndex[value.component.MilestoneCode] != index {
			suppressed[index] = true
			flags = appendUniqueFlag(flags, flagMilestoneDup)
		}
	}

	// 总分先按未取整的分量求和再一次性取整，与 v4 完全一致，避免逐项取整累积偏差。
	raw := 0.0
	raws := make([]float64, 0, len(scored))
	confidences := make([]float64, 0, len(scored))
	for index, value := range scored {
		if suppressed[index] {
			continue
		}
		component := value.component
		raw += component.Score
		raws = append(raws, component.Score)
		component.Confidence = ctx.Confidences[component.WorkItemID]
		if component.Confidence > 0 {
			confidences = append(confidences, component.Confidence)
		}
		component.RequiresManualReview = flagsRequireReview(component.AnomalyFlags) || component.Confidence < confidenceMediumThreshold
		breakdown.Components = append(breakdown.Components, component)
	}

	if item.MaxScore != nil && *item.MaxScore > 0 {
		if raw >= saturationKnee(*item.MaxScore, rules) {
			flags = appendUniqueFlag(flags, flagScoreNearCap)
		}
		raw = applySaturation(raw, *item.MaxScore, rules)
	}
	final := roundHalf(raw)
	// 展示用的分量分按最大余额法分配，保证明细之和严格等于最终分。
	displayed := allocateHalfPoints(raws, final)
	for index := range breakdown.Components {
		if index < len(displayed) {
			breakdown.Components[index].Score = displayed[index]
		}
	}

	z := 0.0
	if len(ctx.PrevScores) >= minHistoryForOutlier {
		z = zScore(final, ctx.PrevScores)
		if math.Abs(z) > historyOutlierZScore {
			flags = appendUniqueFlag(flags, flagHistoryOutlier)
		}
	}

	confidence := average(confidences)
	if confidence <= 0 && len(scored) > 0 {
		confidence = 0.3
	}
	breakdown.RawScore = math.Round(raw*100) / 100
	breakdown.FinalScore = final
	breakdown.AnomalyFlags = flags
	breakdown.Confidence = math.Round(confidence*10000) / 10000
	breakdown.ConfidenceFactors = map[string]float64{"history_z_score": math.Round(z*100) / 100}
	breakdown.RequiresManualReview = flagsRequireReview(flags) || breakdown.Confidence < confidenceMediumThreshold
	breakdown.EngineVersion = firstNonEmpty(ctx.EngineVer, "v5")
	if breakdown.Formula == "" {
		breakdown.Formula = "项目分值按阶段、证据质量、个人贡献、归因比例和复杂度计算"
	}
	return final, breakdown
}

// milestoneComplexityFactor 里程碑/单位分分支的复杂度因子，默认权重 0 即与 v4 完全一致。
func milestoneComplexityFactor(complexity float64, rules RuleSet) float64 {
	weight := rules.MilestoneComplexityWeight
	if weight <= 0 {
		return 1
	}
	if weight > 1 {
		weight = 1
	}
	return 1 - weight*(1-complexity)
}

// maxScoreComplexityFactor 上限分分支的复杂度因子，默认权重 1 即等于 v4 直接相乘。
func maxScoreComplexityFactor(complexity float64, rules RuleSet) float64 {
	weight := rules.ComplexityWeight
	if weight <= 0 {
		return 1
	}
	if weight > 1 {
		weight = 1
	}
	return 1 - weight*(1-complexity)
}

// saturationKnee 返回软饱和拐点，达到该值之前分数保持线性。
func saturationKnee(maxScore float64, rules RuleSet) float64 {
	knee := rules.SaturationKnee
	if knee <= 0 {
		return maxScore
	}
	if knee > 1 {
		knee = 1
	}
	return maxScore * knee
}

// allocateHalfPoints 用最大余额法把最终分拆分到各明细上。
// 保证两点：明细之和严格等于 final（便于人工核对），且每个明细仍是 0.5 的整数倍。
func allocateHalfPoints(scores []float64, final float64) []float64 {
	count := len(scores)
	result := make([]float64, count)
	if count == 0 {
		return result
	}
	units := int(math.Round(final * 2))
	if units < 0 {
		units = 0
	}
	order := make([]int, count)
	remainders := make([]float64, count)
	base := 0
	for index, score := range scores {
		scaled := score * 2
		if scaled < 0 {
			scaled = 0
		}
		floor := int(math.Floor(scaled))
		result[index] = float64(floor) / 2
		remainders[index] = scaled - float64(floor)
		base += floor
		order[index] = index
	}
	deficit := units - base
	if deficit > 0 {
		sort.SliceStable(order, func(i, j int) bool { return remainders[order[i]] > remainders[order[j]] })
		limit := deficit
		if limit > count {
			limit = count
		}
		for offset := 0; offset < limit; offset++ {
			result[order[offset]] += 0.5
		}
		return result
	}
	// 总分需要向下收敛时，从当前分值最大的明细开始扣，避免扣成负数。
	for deficit < 0 {
		target := -1
		for index := range result {
			if result[index] <= 0 {
				continue
			}
			if target < 0 || result[index] > result[target] {
				target = index
			}
		}
		if target < 0 {
			break
		}
		result[target] -= 0.5
		deficit++
	}
	return result
}

// applySaturation 在拐点之上做指数压缩，避免 v4 直接截断导致高产周失去区分度。
func applySaturation(raw, maxScore float64, rules RuleSet) float64 {
	knee := saturationKnee(maxScore, rules)
	if raw <= knee {
		return raw
	}
	headroom := maxScore - knee
	if headroom <= 0 {
		return maxScore
	}
	return knee + headroom*(1-math.Exp(-(raw-knee)/headroom))
}

// zScore 计算当前分相对历史分布的标准化偏离，样本不足时返回 0。
func zScore(current float64, history []float64) float64 {
	if len(history) < minHistoryForOutlier {
		return 0
	}
	mean := average(history)
	variance := 0.0
	for _, value := range history {
		variance += (value - mean) * (value - mean)
	}
	variance /= float64(len(history))
	stddev := math.Sqrt(variance)
	if stddev < 1e-6 {
		return 0
	}
	return (current - mean) / stddev
}

func average(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	total := 0.0
	for _, value := range values {
		total += value
	}
	return total / float64(len(values))
}

func hasKey(values map[string]float64, key string) bool {
	_, ok := values[key]
	return ok
}

// ---------------------------------------------------------------------------
// 归因结果校验与纠偏
// ---------------------------------------------------------------------------

// ValidateAttribution 把 AI 输出吸附到合法档位，并以规则基线为准做偏离校验。
// 函数会就地修正 ai 的档位字段，并返回异常标记列表。
func ValidateAttribution(cluster evidenceCluster, signal RuleSignalSet, ai *classifiedCluster, rubrics map[string]FieldRubric) []string {
	flags := make([]string, 0, 4)
	qualityRubric := rubrics[fieldEvidenceQuality]
	contributionRubric := rubrics[fieldPersonalContribution]
	complexityRubric := rubrics[fieldComplexity]

	serverStage := NormalizeStageCode(signal.ServerStage)
	ai.Stage = NormalizeStageCode(ai.Stage)
	if StageScore(ai.Stage) == 0 && StageScore(serverStage) > 0 {
		ai.Stage = serverStage
	}
	ai.EvidenceQuality = snapField(qualityRubric, ai.EvidenceQuality, signal.ServerQuality)
	ai.PersonalContribution = snapField(contributionRubric, ai.PersonalContribution, contributionRubric.Default)
	ai.Complexity = snapField(complexityRubric, ai.Complexity, signal.ComplexityBaseline)

	// 记录模型原始档位，用于人工校准偏差统计。
	ai.AIStage = ai.Stage
	ai.AIStageScore = StageScore(ai.Stage)
	ai.AIEvidenceQuality = ai.EvidenceQuality
	ai.AIPersonalContribution = ai.PersonalContribution
	ai.AIComplexity = ai.Complexity
	ai.Signals = signal

	// 成员人工补充证据的档位由人填写，不做偏离回滚与矛盾降级。
	if ai.ClassificationStatus != "manual" {
		// 阶段偏离校验：上调需强信号支撑，下调需模型给出可核验说明。
		stageDelta := StageRank(ai.Stage) - StageRank(serverStage)
		if stageDelta > 0 && signal.StrongSignalCount() < stageDelta {
			ai.Stage = serverStage
			flags = appendUniqueFlag(flags, flagDeviationUnsupported)
		} else if stageDelta < 0 && len(ai.EvidenceSignals) == 0 {
			ai.Stage = serverStage
			flags = appendUniqueFlag(flags, flagDeviationUnsupported)
		}

		// 证据质量偏离校验：每上调一档至少对应一条强信号。
		qualityDelta := LadderDistance(qualityRubric, ai.EvidenceQuality, signal.ServerQuality)
		if qualityDelta > 0 && signal.StrongSignalCount() < qualityDelta {
			ai.EvidenceQuality = signal.ServerQuality
			flags = appendUniqueFlag(flags, flagDeviationUnsupported)
		} else if qualityDelta < 0 && len(ai.EvidenceSignals) == 0 {
			ai.EvidenceQuality = signal.ServerQuality
			flags = appendUniqueFlag(flags, flagDeviationUnsupported)
		}

		// 内部矛盾检测。
		if (ai.Stage == "intent" || ai.Stage == "design") && ai.EvidenceQuality >= 0.8 {
			ai.EvidenceQuality = 0.6
			flags = appendUniqueFlag(flags, flagStageQualityConflict)
		}
		if StageAtLeast(ai.Stage, "verification") && len(cluster.Deliverables) == 0 && len(cluster.Verification) == 0 {
			ai.Stage = "deliverable"
			flags = appendUniqueFlag(flags, flagStageDeliverable)
		}
	}
	if ai.ClassificationStatus == "fallback" {
		flags = appendUniqueFlag(flags, flagFallbackClassified)
	}
	ai.AnomalyFlags = flags
	return flags
}

// snapField 把 AI 输出吸附到档位，非法值回退到 fallback。
func snapField(rubric FieldRubric, value, fallback float64) float64 {
	snapped, _, ok := SnapToLadder(rubric, value)
	if !ok {
		snapped, _, _ = SnapToLadder(rubric, fallback)
	}
	return snapped
}

// ApplyCalibration 应用字段级校准参数，输出仍吸附回合法档位。
func ApplyCalibration(field string, value float64, calib CalibrationProfile, rubric FieldRubric) float64 {
	param := calib.Get(field)
	adjusted := value*param.Scale + param.Bias
	if math.IsNaN(adjusted) || math.IsInf(adjusted, 0) {
		return value
	}
	snapped, _, _ := SnapToLadder(rubric, adjusted)
	return snapped
}

// ApplyStageCalibration 对阶段字段应用档位偏移（Bias 取整为档位序号偏移）。
func ApplyStageCalibration(code string, calib CalibrationProfile) string {
	param := calib.Get("stage")
	if param.Bias == 0 {
		return code
	}
	rank := StageRank(code)
	if rank < 0 {
		return code
	}
	ladder := stageLadder()
	target := rank + int(math.Round(param.Bias))
	if target < 0 {
		target = 0
	}
	if target >= len(ladder) {
		target = len(ladder) - 1
	}
	return ladder[target].Code
}

// ---------------------------------------------------------------------------
// 多因子置信度
// ---------------------------------------------------------------------------

// EvaluateConfidence 计算 0–1 的评分置信度与各因子明细。
func EvaluateConfidence(signal RuleSignalSet, ai classifiedCluster, flags []string, historyZ float64) (float64, map[string]float64) {
	saturation := 3.0
	if signal.VolumeSaturation > 0 {
		saturation = float64(signal.VolumeSaturation)
	}
	volumeScore := float64(signal.CardCount) / saturation
	if volumeScore > 1 {
		volumeScore = 1
	}
	diversity := float64(len(signal.SourceKinds)) / 3
	if diversity > 1 {
		diversity = 1
	}
	selfCheck := 0.3
	if ai.SelfCheck.HasDirectEvidence {
		selfCheck = 1
		if ai.SelfCheck.Risk != "" {
			selfCheck = 0.5
		}
	} else if ai.SelfCheck.Risk != "" {
		selfCheck = 0.5
	}
	validation := 1 - 0.25*float64(len(flags))
	if validation < 0 {
		validation = 0
	}
	history := 1 - math.Min(1, math.Abs(historyZ)/3)
	if historyZ == 0 {
		// 无足够历史样本时取中性值，既不奖励也不惩罚。
		history = 0.7
	}
	total := weightEvidenceVolume*volumeScore +
		weightSourceDiversity*diversity +
		weightSelfCheck*selfCheck +
		weightRuleValidation*validation +
		weightHistoryAlignment*history
	factors := map[string]float64{
		"evidence_volume":   math.Round(volumeScore*1000) / 1000,
		"source_diversity":  math.Round(diversity*1000) / 1000,
		"self_check":        math.Round(selfCheck*1000) / 1000,
		"rule_validation":   math.Round(validation*1000) / 1000,
		"history_alignment": math.Round(history*1000) / 1000,
	}
	return math.Round(total*10000) / 10000, factors
}

// ConfidenceLevel 把数值置信度映射为展示等级。
func ConfidenceLevel(value float64) string {
	switch {
	case value >= confidenceHighThreshold:
		return "high"
	case value >= confidenceMediumThreshold:
		return "medium"
	default:
		return "low"
	}
}

// flagsRequireReview 判断异常标记是否触发人工复核。
func flagsRequireReview(flags []string) bool {
	for _, flag := range flags {
		if reviewTriggeringFlags[flag] {
			return true
		}
	}
	return false
}

// appendUniqueFlag 追加不重复的异常标记，结果按字典序稳定输出。
func appendUniqueFlag(values []string, additions ...string) []string {
	for _, addition := range additions {
		exists := false
		for _, value := range values {
			if value == addition {
				exists = true
				break
			}
		}
		if !exists {
			values = append(values, addition)
		}
	}
	sort.Strings(values)
	return values
}
