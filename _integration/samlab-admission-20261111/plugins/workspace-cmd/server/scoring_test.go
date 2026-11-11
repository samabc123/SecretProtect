package main

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// 档位表
// ---------------------------------------------------------------------------

func TestSnapToLadderSnapsAndFallsBack(t *testing.T) {
	rubric := evidenceQualityRubric()
	if value, _, ok := SnapToLadder(rubric, 0.72); !ok || math.Abs(value-0.8) > 1e-9 {
		t.Fatalf("snap 0.72 = %v ok=%v", value, ok)
	}
	if value, _, ok := SnapToLadder(rubric, 0); ok || math.Abs(value-rubric.Default) > 1e-9 {
		t.Fatalf("snap 0 must fall back to default, got %v ok=%v", value, ok)
	}
	if value, _, ok := SnapToLadder(rubric, math.NaN()); ok || math.Abs(value-rubric.Default) > 1e-9 {
		t.Fatalf("snap NaN must fall back, got %v ok=%v", value, ok)
	}
	if value, _, ok := SnapToLadder(rubric, 2); ok || math.Abs(value-1) > 1e-9 {
		t.Fatalf("out-of-range value must clamp to top rung and report !ok, got %v ok=%v", value, ok)
	}
}

func TestStageRankAndNormalization(t *testing.T) {
	if StageRank("intent") != 0 || StageRank("external_confirmation") != 5 {
		t.Fatal("stage ladder order changed")
	}
	if StageRank("bogus") != -1 {
		t.Fatal("unknown stage must return -1")
	}
	if NormalizeStageCode("DELIVERABLE ") != "deliverable" {
		t.Fatalf("normalize stage = %q", NormalizeStageCode("DELIVERABLE "))
	}
	if NormalizeStageCode("failure") != "intent" {
		t.Fatalf("failure must normalize to intent, got %q", NormalizeStageCode("failure"))
	}
	if !StageAtLeast("verification", "deliverable") || StageAtLeast("draft", "deliverable") {
		t.Fatal("StageAtLeast comparison is wrong")
	}
	if code, score := SortStageByScore([]string{"failure", "draft", "verification", "design"}); code != "verification" || math.Abs(score-0.9) > 1e-9 {
		t.Fatalf("SortStageByScore = %v %v", code, score)
	}
}

func TestLadderDistance(t *testing.T) {
	rubric := evidenceQualityRubric()
	if LadderDistance(rubric, 1, 0.6) != 2 {
		t.Fatalf("distance 1->0.6 = %d", LadderDistance(rubric, 1, 0.6))
	}
	if LadderDistance(rubric, 0.4, 0.6) != -1 {
		t.Fatalf("distance 0.4->0.6 = %d", LadderDistance(rubric, 0.4, 0.6))
	}
	if LadderDistance(rubric, 0.5, 0.6) != 0 {
		t.Fatalf("unknown value must yield 0 distance, got %d", LadderDistance(rubric, 0.5, 0.6))
	}
}

// ---------------------------------------------------------------------------
// 规则信号
// ---------------------------------------------------------------------------

func evidenceCard(kind string, stageScore, quality float64, tools, deliverables, verification, fileChanges []string) weeklyEvidenceCard {
	now := time.Date(2026, 8, 3, 9, 0, 0, 0, time.UTC)
	return weeklyEvidenceCard{
		SourceType: "conversation", EvidenceKind: kind, StageScore: stageScore, EvidenceQuality: quality,
		Tools: tools, Deliverables: deliverables, Verification: verification, FileChanges: fileChanges,
		OccurredAt: now, TaskGoal: "整理实验数据", Summary: "汇总实验结果",
	}
}

func TestBuildRuleSignalsMatchesLegacyQualityRules(t *testing.T) {
	rules := DefaultRuleSet()
	base := time.Date(2026, 8, 3, 9, 0, 0, 0, time.UTC)
	withTools := evidenceCard("deliverable", 0.7, 1, []string{"bash"}, nil, nil, nil)
	withDeliverable := evidenceCard("deliverable", 0.7, 0.85, nil, []string{"report.pdf"}, nil, nil)
	withStageOnly := evidenceCard("draft", 0.45, 0.6, nil, nil, nil, nil)
	withDeliverable.OccurredAt = base.AddDate(0, 0, 2)

	cases := []struct {
		name    string
		cards   []weeklyEvidenceCard
		quality float64
		stage   string
	}{
		{"strong signal", []weeklyEvidenceCard{withTools}, 1, "deliverable"},
		{"deliverable only", []weeklyEvidenceCard{withDeliverable}, 0.8, "deliverable"},
		{"stage only", []weeklyEvidenceCard{withStageOnly}, 0.6, "draft"},
		{"cluster takes max", []weeklyEvidenceCard{withStageOnly, withTools}, 1, "deliverable"},
	}
	for _, item := range cases {
		signal := BuildRuleSignals(item.cards, rules, "fp", nil)
		if math.Abs(signal.ServerQuality-item.quality) > 1e-9 {
			t.Fatalf("%s: server quality = %v want %v", item.name, signal.ServerQuality, item.quality)
		}
		if signal.ServerStage != item.stage {
			t.Fatalf("%s: server stage = %v want %v", item.name, signal.ServerStage, item.stage)
		}
	}
}

func TestBuildRuleSignalsStrongSignalsAndSpan(t *testing.T) {
	rules := DefaultRuleSet()
	base := time.Date(2026, 8, 3, 9, 0, 0, 0, time.UTC)
	first := evidenceCard("verification", 0.9, 1, []string{"bash"}, []string{"a.pdf"}, []string{"测试通过"}, []string{"main.go"})
	second := evidenceCard("external_confirmation", 1, 1, nil, nil, nil, nil)
	second.OccurredAt = base.AddDate(0, 0, 3)
	signal := BuildRuleSignals([]weeklyEvidenceCard{first, second}, rules, "fp", map[string]bool{"fp": true})
	for _, expected := range []string{signalFileChange, signalTestAssertion, signalExternalConfirmation, signalToolResult} {
		if !signal.HasStrongSignal(expected) {
			t.Fatalf("expected strong signal %s in %v", expected, signal.StrongSignals)
		}
	}
	if !signal.ExternalConfirm || signal.SpanDays != 4 || signal.CardCount != 2 {
		t.Fatalf("signal = %+v", signal)
	}
	if !signal.ContinuesPrevWeek {
		t.Fatal("expected continuation flag from previous week fingerprints")
	}
	if signal.VolumeFactor != 1 {
		t.Fatalf("volume factor must be neutral by default, got %v", signal.VolumeFactor)
	}
}

func TestVolumeFactorDisabledByDefault(t *testing.T) {
	rules := DefaultRuleSet()
	if factor := volumeFactor(1, rules); factor != 1 {
		t.Fatalf("default volume factor = %v", factor)
	}
	rules.VolumeFactorWeight = 0.2
	rules.VolumeSaturationCards = 3
	if factor := volumeFactor(3, rules); factor != 1 {
		t.Fatalf("saturated volume factor = %v", factor)
	}
	if factor := volumeFactor(1, rules); factor >= 1 || factor <= 0.8 {
		t.Fatalf("thin evidence must reduce factor, got %v", factor)
	}
}

func TestSanitizeRuleSetFillsDefaults(t *testing.T) {
	clean := sanitizeRuleSet(RuleSet{})
	base := DefaultRuleSet()
	if clean.ComplexityWeight != base.ComplexityWeight || clean.MilestoneComplexityWeight != base.MilestoneComplexityWeight {
		t.Fatalf("sanitized weights = %v / %v", clean.ComplexityWeight, clean.MilestoneComplexityWeight)
	}
	if clean.SaturationKnee != base.SaturationKnee || clean.VolumeSaturationCards != base.VolumeSaturationCards {
		t.Fatalf("sanitized knee/saturation = %v / %v", clean.SaturationKnee, clean.VolumeSaturationCards)
	}
	if len(clean.StageScores) != len(base.StageScores) || len(clean.QualityRules) != len(base.QualityRules) {
		t.Fatal("sanitized rule maps are incomplete")
	}
}

// ---------------------------------------------------------------------------
// V5 评分：与 v4 的一致性
// ---------------------------------------------------------------------------

func scoreInput(workItemID int64, stage, quality, contribution, allocation, complexity, units float64, milestone string) reportAttributionScoreInput {
	return reportAttributionScoreInput{WorkItemID: workItemID, StageScore: stage, EvidenceQuality: quality,
		PersonalContribution: contribution, AllocationPercent: allocation, Complexity: complexity,
		MilestoneCode: milestone, UnitCount: units}
}

func TestV5DefaultRulesMatchV4(t *testing.T) {
	maxScore := 20.0
	item := reportTemplateItem{ID: 13, MaxScore: &maxScore, UnitPoints: map[string]float64{"verified_module": 10},
		Milestones: []reportMilestone{{Code: "outline", Points: 2}}}
	inputs := []reportAttributionScoreInput{scoreInput(1, 0.9, 1, 0.75, 100, 0.5, 1, "verified_module")}
	rules := DefaultRuleSet()
	v4Score, _ := calculateProjectScore(item, inputs)
	v5Score, breakdown := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: inputs, Rules: rules, Calib: DefaultCalibrationProfile()})
	if math.Abs(v4Score-v5Score) > 1e-9 {
		t.Fatalf("v4=%v v5=%v must match under default rules", v4Score, v5Score)
	}
	if breakdown.EngineVersion != "v5" {
		t.Fatalf("engine version = %v", breakdown.EngineVersion)
	}

	maxBranch := reportTemplateItem{ID: 12, MaxScore: &maxScore}
	maxInputs := []reportAttributionScoreInput{scoreInput(2, 0.7, 0.8, 1, 100, 0.75, 1, "")}
	v4Max, _ := calculateProjectScore(maxBranch, maxInputs)
	v5Max, _ := CalculateProjectScoreV5(ScoreContext{Item: maxBranch, Inputs: maxInputs, Rules: rules, Calib: DefaultCalibrationProfile()})
	if math.Abs(v4Max-v5Max) > 1e-9 {
		t.Fatalf("max-score branch v4=%v v5=%v must match", v4Max, v5Max)
	}
}

func TestV5MultipleComponentsStayWithinRoundingTolerance(t *testing.T) {
	maxScore := 20.0
	item := reportTemplateItem{ID: 13, MaxScore: &maxScore, UnitPoints: map[string]float64{"verified_module": 10}}
	inputs := []reportAttributionScoreInput{
		scoreInput(1, 0.9, 1, 0.75, 60, 0.5, 1, "verified_module"),
		scoreInput(2, 0.7, 0.8, 0.5, 40, 0.5, 1, "verified_module"),
	}
	v4Score, _ := calculateProjectScore(item, inputs)
	v5Score, breakdown := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: inputs, Rules: DefaultRuleSet(), Calib: DefaultCalibrationProfile()})
	if math.Abs(v4Score-v5Score) > 0.5*float64(len(inputs))+1e-9 {
		t.Fatalf("v4=%v v5=%v deviates beyond rounding tolerance", v4Score, v5Score)
	}
	sum := 0.0
	for _, component := range breakdown.Components {
		sum += component.Score
	}
	if math.Abs(sum-breakdown.FinalScore) > 1e-9 {
		t.Fatalf("displayed components %v must add up to final score %v", sum, breakdown.FinalScore)
	}
}

func TestV5MilestoneTakesHighestNotFirst(t *testing.T) {
	maxScore := 10.0
	item := reportTemplateItem{ID: 2, MaxScore: &maxScore, Milestones: []reportMilestone{{Code: "outline", Points: 4}}}
	weak := scoreInput(1, 0.45, 0.6, 0.5, 100, 0.5, 1, "outline")
	strong := scoreInput(2, 0.9, 1, 1, 100, 0.5, 1, "outline")
	lowFirst, first := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: []reportAttributionScoreInput{weak, strong}, Rules: DefaultRuleSet(), Calib: DefaultCalibrationProfile()})
	highFirst, second := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: []reportAttributionScoreInput{strong, weak}, Rules: DefaultRuleSet(), Calib: DefaultCalibrationProfile()})
	if math.Abs(lowFirst-highFirst) > 1e-9 {
		t.Fatalf("score must not depend on input order: %v vs %v", lowFirst, highFirst)
	}
	if !hasFlag(first.AnomalyFlags, flagMilestoneDup) || !hasFlag(second.AnomalyFlags, flagMilestoneDup) {
		t.Fatal("duplicate milestone must be flagged")
	}
	if math.Abs(lowFirst-roundHalf(4*0.9*1*1*1)) > 1e-9 {
		t.Fatalf("highest milestone component should win, got %v", lowFirst)
	}
}

func TestV5SaturationKeepsScoresLinearBelowKnee(t *testing.T) {
	maxScore := 20.0
	item := reportTemplateItem{ID: 13, MaxScore: &maxScore, UnitPoints: map[string]float64{"verified_module": 10}}
	inputs := []reportAttributionScoreInput{scoreInput(1, 0.9, 1, 1, 100, 0.5, 1, "verified_module")}
	score, _ := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: inputs, Rules: DefaultRuleSet(), Calib: DefaultCalibrationProfile()})
	if math.Abs(score-9) > 1e-9 {
		t.Fatalf("below knee score must stay linear, got %v", score)
	}
	inputs[0].UnitCount = 3
	capped, breakdown := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: inputs, Rules: DefaultRuleSet(), Calib: DefaultCalibrationProfile()})
	if !hasFlag(breakdown.AnomalyFlags, flagScoreNearCap) {
		t.Fatalf("near cap must be flagged, got %v", breakdown.AnomalyFlags)
	}
	if capped > maxScore {
		t.Fatalf("score %v exceeded max %v", capped, maxScore)
	}
}

func TestV5LowEvidenceHighScoreFlagsReview(t *testing.T) {
	maxScore := 10.0
	item := reportTemplateItem{ID: 13, MaxScore: &maxScore, UnitPoints: map[string]float64{"verified_module": 10}}
	inputs := []reportAttributionScoreInput{scoreInput(1, 1, 1, 1, 100, 0.5, 1, "verified_module")}
	_, breakdown := CalculateProjectScoreV5(ScoreContext{
		Item: item, Inputs: inputs, Rules: DefaultRuleSet(), Calib: DefaultCalibrationProfile(),
		Signals: map[int64]RuleSignalSet{1: {CardCount: 1}},
	})
	if !hasFlag(breakdown.AnomalyFlags, flagLowEvidenceHighScore) {
		t.Fatalf("expected low evidence high score flag, got %v", breakdown.AnomalyFlags)
	}
	if !breakdown.RequiresManualReview {
		t.Fatal("expected manual review to be required")
	}
}

func TestZScoreDetectsOutlier(t *testing.T) {
	history := []float64{10, 10, 11, 10, 9, 10, 10}
	if z := zScore(10, history); math.Abs(z) > 0.5 {
		t.Fatalf("inlier z = %v", z)
	}
	if z := zScore(30, history); math.Abs(z) <= historyOutlierZScore {
		t.Fatalf("outlier z = %v should exceed threshold", z)
	}
	if z := zScore(10, []float64{10}); z != 0 {
		t.Fatalf("insufficient history must return 0, got %v", z)
	}
	if z := zScore(12, []float64{10, 10, 10}); z != 0 {
		t.Fatalf("zero variance must return 0, got %v", z)
	}
}

func hasFlag(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// 归因校验与纠偏
// ---------------------------------------------------------------------------

func TestValidateAttributionRollsBackUnsupportedUpgrade(t *testing.T) {
	rubrics := numericRubrics()
	signal := RuleSignalSet{ServerStage: "draft", ServerStageScore: 0.45, ServerQuality: 0.6}
	ai := classifiedCluster{Stage: "verification", EvidenceQuality: 1, PersonalContribution: 0.75, Complexity: 0.5,
		EvidenceSignals: []string{"助手说已完成"}}
	flags := ValidateAttribution(evidenceCluster{}, signal, &ai, rubrics)
	if !hasFlag(flags, flagDeviationUnsupported) {
		t.Fatalf("expected unsupported deviation flag, got %v", flags)
	}
	if ai.Stage != "draft" {
		t.Fatalf("stage must roll back to baseline, got %v", ai.Stage)
	}
	if ai.AIStage != "verification" {
		t.Fatalf("AI raw stage must be preserved, got %v", ai.AIStage)
	}
	if math.Abs(ai.EvidenceQuality-0.6) > 1e-9 {
		t.Fatalf("quality must roll back to baseline, got %v", ai.EvidenceQuality)
	}
}

func TestValidateAttributionAllowsSupportedUpgrade(t *testing.T) {
	rubrics := numericRubrics()
	signal := RuleSignalSet{ServerStage: "draft", ServerStageScore: 0.45, ServerQuality: 0.6,
		StrongSignals: []string{signalFileChange}}
	ai := classifiedCluster{Stage: "deliverable", EvidenceQuality: 0.8, PersonalContribution: 0.75, Complexity: 0.5,
		EvidenceSignals: []string{"出现 2 处文件变更"}}
	flags := ValidateAttribution(evidenceCluster{Deliverables: []string{"a.go"}}, signal, &ai, rubrics)
	if hasFlag(flags, flagDeviationUnsupported) {
		t.Fatalf("supported upgrade must not be flagged, got %v", flags)
	}
	if ai.Stage != "deliverable" || math.Abs(ai.EvidenceQuality-0.8) > 1e-9 {
		t.Fatalf("supported upgrade must be kept: stage=%v quality=%v", ai.Stage, ai.EvidenceQuality)
	}
}

func TestValidateAttributionStageQualityConflict(t *testing.T) {
	rubrics := numericRubrics()
	signal := RuleSignalSet{ServerStage: "intent", ServerQuality: 0}
	ai := classifiedCluster{Stage: "design", EvidenceQuality: 1}
	flags := ValidateAttribution(evidenceCluster{}, signal, &ai, rubrics)
	if !hasFlag(flags, flagStageQualityConflict) {
		t.Fatalf("expected stage/quality conflict flag, got %v", flags)
	}
	if math.Abs(ai.EvidenceQuality-0.6) > 1e-9 {
		t.Fatalf("quality must be downgraded to 0.6, got %v", ai.EvidenceQuality)
	}
}

func TestValidateAttributionStageDeliverableConflict(t *testing.T) {
	rubrics := numericRubrics()
	signal := RuleSignalSet{ServerStage: "verification", ServerStageScore: 0.9, ServerQuality: 0.8,
		StrongSignals: []string{signalTestAssertion}}
	ai := classifiedCluster{Stage: "verification", EvidenceQuality: 0.8}
	flags := ValidateAttribution(evidenceCluster{}, signal, &ai, rubrics)
	if !hasFlag(flags, flagStageDeliverable) {
		t.Fatalf("expected stage/deliverable conflict flag, got %v", flags)
	}
	if ai.Stage != "deliverable" {
		t.Fatalf("stage must downgrade to deliverable, got %v", ai.Stage)
	}
}

func TestValidateAttributionSkipsDeviationForManualEvidence(t *testing.T) {
	rubrics := numericRubrics()
	signal := RuleSignalSet{ServerStage: "draft", ServerStageScore: 0.45, ServerQuality: 0.6}
	ai := classifiedCluster{Stage: "verification", EvidenceQuality: 1, ClassificationStatus: "manual"}
	flags := ValidateAttribution(evidenceCluster{}, signal, &ai, rubrics)
	if hasFlag(flags, flagDeviationUnsupported) {
		t.Fatalf("human provided evidence must not be rolled back, got %v", flags)
	}
	if ai.Stage != "verification" {
		t.Fatalf("manual stage must be preserved, got %v", ai.Stage)
	}
}

// ---------------------------------------------------------------------------
// 置信度与校准
// ---------------------------------------------------------------------------

func TestEvaluateConfidenceFactorsAndLevels(t *testing.T) {
	signal := RuleSignalSet{CardCount: 3, SourceKinds: []string{"conversation", "trajectory", "manual"}, VolumeSaturation: 3}
	ai := classifiedCluster{SelfCheck: attributionSelfCheck{HasDirectEvidence: true}}
	score, factors := EvaluateConfidence(signal, ai, nil, 0)
	if score < confidenceHighThreshold {
		t.Fatalf("well supported item should be high confidence, got %v (%v)", score, factors)
	}
	if len(factors) != 5 {
		t.Fatalf("expected five confidence factors, got %v", factors)
	}
	low, _ := EvaluateConfidence(RuleSignalSet{CardCount: 1}, classifiedCluster{}, []string{"A", "B", "C", "D"}, 0)
	if low >= confidenceMediumThreshold {
		t.Fatalf("thin evidence with flags should be low confidence, got %v", low)
	}
	if ConfidenceLevel(0.8) != "high" || ConfidenceLevel(0.6) != "medium" || ConfidenceLevel(0.2) != "low" {
		t.Fatal("confidence level thresholds are wrong")
	}
}

func TestCalibrationProfileIsIdentityByDefault(t *testing.T) {
	profile := DefaultCalibrationProfile()
	rubric := evidenceQualityRubric()
	if value := ApplyCalibration(fieldEvidenceQuality, 0.6, profile, rubric); math.Abs(value-0.6) > 1e-9 {
		t.Fatalf("identity calibration changed value: %v", value)
	}
	if code := ApplyStageCalibration("draft", profile); code != "draft" {
		t.Fatalf("identity stage calibration changed code: %v", code)
	}
	profile.Fields[fieldEvidenceQuality] = CalibrationField{Bias: 0.2, Scale: 1, Enabled: true}
	if value := ApplyCalibration(fieldEvidenceQuality, 0.6, profile, rubric); math.Abs(value-0.8) > 1e-9 {
		t.Fatalf("bias calibration = %v", value)
	}
	profile.Fields["stage"] = CalibrationField{Bias: 1, Scale: 1, Enabled: true}
	if code := ApplyStageCalibration("draft", profile); code != "deliverable" {
		t.Fatalf("stage bias calibration = %v", code)
	}
	profile.Fields["stage"] = CalibrationField{Bias: 1, Scale: 1, Enabled: false}
	if code := ApplyStageCalibration("draft", profile); code != "draft" {
		t.Fatalf("disabled calibration must be inert, got %v", code)
	}
}

func TestFlagsRequireReview(t *testing.T) {
	if flagsRequireReview([]string{flagMilestoneDup}) {
		t.Fatal("informational flag must not require review")
	}
	if !flagsRequireReview([]string{flagHistoryOutlier}) {
		t.Fatal("history outlier must require review")
	}
	if !flagsRequireReview([]string{flagDeviationUnsupported}) {
		t.Fatal("unsupported deviation must require review")
	}
	merged := appendUniqueFlag([]string{"B"}, "A", "B")
	if len(merged) != 2 || merged[0] != "A" || merged[1] != "B" {
		t.Fatalf("appendUniqueFlag = %v", merged)
	}
}

// ---------------------------------------------------------------------------
// Prompt v5
// ---------------------------------------------------------------------------

func TestAttributionPromptContainsAnchorsAndConstraints(t *testing.T) {
	system := attributionSystemPrompt()
	for _, expected := range []string{
		"evidence_signals", "stage_reason", "self_check", "cluster_key",
		"0.2", "0.4", "0.6", "0.8", "1", "0.25", "0.5", "0.75",
		"server_stage", "server_quality", "强信号", "边界示例", "external_confirmation",
		"不得输出原始长文本", "只返回 JSON",
	} {
		if !strings.Contains(system, expected) {
			t.Fatalf("prompt missing %q", expected)
		}
	}
	if strings.Contains(system, "{{") {
		t.Fatal("prompt contains unresolved template placeholders")
	}
}

func TestAttributionUserPromptCarriesRuleSignals(t *testing.T) {
	template := reportTemplateRecord{Definition: reportTemplateDefinition{Title: "测试模板"}}
	cluster := evidenceCluster{Key: "k1", Title: "任务", Summary: "摘要", SourceRefs: []string{"conversation:1"}}
	signals := map[string]RuleSignalSet{"k1": {CardCount: 2, ServerStage: "deliverable", ServerQuality: 0.8,
		StrongSignals: []string{signalFileChange}, VolumeSaturation: 3}}
	user := buildAttributionUserPrompt(template, []evidenceCluster{cluster}, signals)
	for _, expected := range []string{"cluster_key", "rule_signals", "server_stage", "complexity_baseline", "strong_signals", "continues_previous_week"} {
		if !strings.Contains(user, expected) {
			t.Fatalf("user prompt missing %q", expected)
		}
	}
}

func TestDecodeAttributionPayloadRejectsGarbage(t *testing.T) {
	if _, err := decodeAttributionPayload([]byte("not json")); err == nil {
		t.Fatal("expected decode failure")
	}
	payload, err := decodeAttributionPayload([]byte(`{"items":[{"cluster_key":"k1","evidence_signals":["有文件变更"],"self_check":{"has_direct_evidence":true}}]}`))
	if err != nil || len(payload.Items) != 1 || len(payload.Items[0].EvidenceSignals) != 1 {
		t.Fatalf("decode = %+v err %v", payload, err)
	}
	if !payload.Items[0].SelfCheck.HasDirectEvidence {
		t.Fatal("self_check must round trip")
	}
}

// ---------------------------------------------------------------------------
// 迁移契约
// ---------------------------------------------------------------------------

func TestMigration007IsIdempotent(t *testing.T) {
	path := filepath.Join("..", "..", "migrations", "007_weekly_scoring_calibration.sql")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("migration file not reachable from test working dir: %v", err)
	}
	content := string(data)
	for _, table := range []string{
		"weekly_score_calibrations", "weekly_score_calibration_profiles",
		"weekly_score_anomalies", "weekly_scoring_rule_sets",
	} {
		if !strings.Contains(content, "CREATE TABLE IF NOT EXISTS "+table) {
			t.Fatalf("table %s must be created idempotently", table)
		}
	}
	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "ALTER TABLE") && !strings.Contains(trimmed, "IF NOT EXISTS") {
			t.Fatalf("ALTER TABLE must be idempotent: %s", trimmed)
		}
		if strings.HasPrefix(trimmed, "CREATE INDEX") && !strings.Contains(trimmed, "IF NOT EXISTS") {
			t.Fatalf("CREATE INDEX must be idempotent: %s", trimmed)
		}
	}
	if !strings.Contains(content, "GRANT") || !strings.Contains(content, "workspace_runtime") {
		t.Fatal("migration must grant runtime privileges")
	}
	if strings.Contains(content, "DROP TABLE") || strings.Contains(content, "DROP COLUMN") {
		t.Fatal("migration must not drop existing objects")
	}
}

func TestMigration007SeedRuleSetParses(t *testing.T) {
	path := filepath.Join("..", "..", "migrations", "007_weekly_scoring_calibration.sql")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("migration file not reachable: %v", err)
	}
	start := strings.Index(string(data), "$rules${")
	if start < 0 {
		t.Fatal("seed rule set block not found")
	}
	rest := string(data)[start+len("$rules$"):]
	end := strings.Index(rest, "}$rules$")
	if end < 0 {
		t.Fatal("seed rule set block not terminated")
	}
	var parsed RuleSet
	if err := json.Unmarshal([]byte(rest[:end+1]), &parsed); err != nil {
		t.Fatalf("seed rule set is not valid JSON: %v", err)
	}
	if parsed.VolumeSaturationCards != DefaultRuleSet().VolumeSaturationCards {
		t.Fatalf("seed saturation = %v", parsed.VolumeSaturationCards)
	}
	if len(parsed.QualityRules) != 4 || parsed.QualityRules["deliverable_only"] != 0.85 {
		t.Fatalf("seed quality rules = %v", parsed.QualityRules)
	}
}
