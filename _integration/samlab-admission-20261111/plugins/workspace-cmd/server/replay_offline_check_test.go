package main

// 离线对照：在 canary 阶段用生产真实样本核对 v5 与 v4 的分差。
// 依赖 /tmp/score-replay 下的导出文件，文件不存在时自动跳过，因此不影响常规门禁。

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"sort"
	"testing"
)

type replayRow struct {
	UserID               int64   `json:"user_id"`
	WeekStart            string  `json:"week_start"`
	WorkItemID           int64   `json:"work_item_id"`
	ItemID               int     `json:"item_id"`
	AllocationPercent    float64 `json:"allocation_percent"`
	MilestoneCode        string  `json:"milestone_code"`
	UnitCount            float64 `json:"unit_count"`
	ScoreComponent       float64 `json:"score_component"`
	StageScore           float64 `json:"stage_score"`
	EvidenceQuality      float64 `json:"evidence_quality"`
	PersonalContribution float64 `json:"personal_contribution"`
	Complexity           float64 `json:"complexity"`
}

func TestReplayOfflineV4VsV5(t *testing.T) {
	rawInputs, err1 := os.ReadFile("/tmp/score-replay/inputs.json")
	rawTemplate, err2 := os.ReadFile("/tmp/score-replay/template.json")
	if err1 != nil || err2 != nil {
		t.Skip("没有离线对照样本，跳过")
	}
	var rows []replayRow
	if err := json.Unmarshal(rawInputs, &rows); err != nil {
		t.Fatalf("解析样本失败: %v", err)
	}
	var def reportTemplateDefinition
	if err := json.Unmarshal(rawTemplate, &def); err != nil {
		t.Fatalf("解析模板失败: %v", err)
	}
	items := templateItemMap(def)

	grouped := make(map[string][]replayRow)
	for _, row := range rows {
		key := fmt.Sprintf("%s|u%d|item%d", row.WeekStart, row.UserID, row.ItemID)
		grouped[key] = append(grouped[key], row)
	}
	keys := make([]string, 0, len(grouped))
	for key := range grouped {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	rules := DefaultRuleSet()
	calib := DefaultCalibrationProfile()
	totalV4, totalV5, changed, maxDelta := 0.0, 0.0, 0, 0.0
	for _, key := range keys {
		group := grouped[key]
		item, ok := items[group[0].ItemID]
		if !ok {
			continue
		}
		inputs := make([]reportAttributionScoreInput, 0, len(group))
		for _, row := range group {
			inputs = append(inputs, reportAttributionScoreInput{
				WorkItemID: row.WorkItemID, StageScore: row.StageScore, EvidenceQuality: row.EvidenceQuality,
				PersonalContribution: row.PersonalContribution, Complexity: row.Complexity,
				AllocationPercent: row.AllocationPercent, MilestoneCode: row.MilestoneCode, UnitCount: row.UnitCount,
			})
		}
		v4, _ := calculateProjectScore(item, inputs)
		v5, v5Breakdown := CalculateProjectScoreV5(ScoreContext{Item: item, Inputs: inputs, Rules: rules, Calib: calib})
		totalV4 += v4
		totalV5 += v5
		if delta := math.Abs(v4 - v5); delta > 1e-9 {
			changed++
			if delta > maxDelta {
				maxDelta = delta
			}
			t.Logf("DIFF %s v4=%.2f v5=%.2f delta=%+.2f flags=%v components=%d",
				key, v4, v5, v5-v4, v5Breakdown.AnomalyFlags, len(v5Breakdown.Components))
		}
	}
	t.Logf("SUMMARY projects=%d rows=%d changed=%d v4_sum=%.2f v5_sum=%.2f sum_delta=%+.2f max_delta=%.2f",
		len(keys), len(rows), changed, totalV4, totalV5, totalV5-totalV4, maxDelta)
}
