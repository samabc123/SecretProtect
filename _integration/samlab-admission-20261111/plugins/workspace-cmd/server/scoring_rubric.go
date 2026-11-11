package main

import (
	"fmt"
	"math"
	"strings"
)

// 字段级评分细则。把 AI 原先可自由输出的连续值收敛为有序档位，
// 让「打分」从回归问题变成分类问题，从而显著降低方差并支持人工校准。

// RubricLevel 描述一个字段的一个合法档位。
type RubricLevel struct {
	Value     float64  `json:"value"`
	Label     string   `json:"label"`
	Criteria  string   `json:"criteria"`
	Positives []string `json:"positives,omitempty"`
	Negatives []string `json:"negatives,omitempty"`
}

// FieldRubric 描述一个数值型字段的完整档位表（Levels 由低到高有序）。
type FieldRubric struct {
	Field   string        `json:"field"`
	Levels  []RubricLevel `json:"levels"`
	Default float64       `json:"default"`
}

// StageLevel 描述阶段字段的一个合法档位（阶段用字符串编码，附带折算分）。
type StageLevel struct {
	Code      string   `json:"code"`
	Score     float64  `json:"score"`
	Label     string   `json:"label"`
	Criteria  string   `json:"criteria"`
	Positives []string `json:"positives,omitempty"`
	Negatives []string `json:"negatives,omitempty"`
}

const (
	fieldEvidenceQuality      = "evidence_quality"
	fieldPersonalContribution = "personal_contribution"
	fieldComplexity           = "complexity"
	fieldAllocation           = "allocation"
)

// stageLadder 顺序与现网 normalizeEvidenceStage / 证据卡判定链一致，不可随意调整顺序。
func stageLadder() []StageLevel {
	return []StageLevel{
		{
			Code: "intent", Score: 0, Label: "仅提出意图",
			Criteria: "只有需求描述、提问、讨论或待办，没有任何本人产出痕迹。",
			Positives: []string{
				"用户提出了目标或需求，助手给出计划但没有执行结果",
				"只有问答、解释、咨询，没有文件、交付物或验证记录",
			},
			Negatives: []string{
				"助手声称会完成（计划不等于完成）",
				"出现了代码但没有文件变更或交付物",
			},
		},
		{
			Code: "design", Score: 0.2, Label: "分析设计",
			Criteria: "有方案、调研、架构、评估或设计结论，但没有进入实现。",
			Positives: []string{
				"给出方案对比、架构设计、可行性分析",
				"完成文献调研或技术选型结论",
			},
			Negatives: []string{
				"方案讨论中顺带写了几行示例代码，但没有形成可指名产物",
			},
		},
		{
			Code: "draft", Score: 0.45, Label: "草稿或部分实现",
			Criteria: "出现初稿、草稿、编写中、修改中或部分完成的明确痕迹，但尚未形成交付物。",
			Positives: []string{
				"明确出现初稿、草稿、正在修改、部分完成等表述",
				"产出了内容片段但未被指名为交付物",
			},
			Negatives: []string{
				"助手单方面声明『已完成』但 verification 与 file_changes 均为空",
			},
		},
		{
			Code: "deliverable", Score: 0.7, Label: "形成可指名交付物",
			Criteria: "出现可指名的交付物、文件变更或明确完成结果，但尚未验证。",
			Positives: []string{
				"出现具体文件名、文档、图表、脚本、模块等交付物名称",
				"出现文件变更记录或明确的完成结果描述",
			},
			Negatives: []string{
				"只有『已完成』字样但没有任何可指名产物",
				"有测试计划但没有测试输出或断言结果",
			},
		},
		{
			Code: "verification", Score: 0.9, Label: "已验证",
			Criteria: "交付物通过可核验的验证：测试通过、构建成功、编译通过、部署成功或检查点通过。",
			Positives: []string{
				"出现测试通过、全部通过、构建成功、编译通过、部署成功等明确输出",
				"出现退出码 0、健康检查通过等可核验信号",
			},
			Negatives: []string{
				"仅助手文字描述『已测试通过』而没有任何输出、日志或断言",
			},
		},
		{
			Code: "external_confirmation", Score: 1, Label: "外部确认",
			Criteria: "获得本系统之外的确认：验收通过、投稿成功、已发布、录用、获奖等。",
			Positives: []string{
				"出现验收通过、投稿成功、已发布、录用、获奖等外部结果",
				"出现第三方（期刊、会议、客户、导师）确认记录",
			},
			Negatives: []string{
				"自己声明已提交但没有系统或第三方确认",
			},
		},
	}
}

func evidenceQualityRubric() FieldRubric {
	return FieldRubric{
		Field:   fieldEvidenceQuality,
		Default: 0.4,
		Levels: []RubricLevel{
			{
				Value: 0.2, Label: "仅意图，无实质痕迹",
				Criteria:  "只有提问或需求描述，既无工具执行、无文件变更，也无交付物或验证。",
				Positives: []string{"纯咨询或讨论", "只有需求描述"},
				Negatives: []string{"出现助手生成的长篇方案（方案不是证据）"},
			},
			{
				Value: 0.4, Label: "弱痕迹",
				Criteria:  "有对话过程或文字产出，但没有可核验的对象（无文件变更、无测试、无交付物名称）。",
				Positives: []string{"有设计讨论或草稿内容但无可指名产物", "仅助手单方面完成声明"},
				Negatives: []string{"出现任何文件变更（应升档）"},
			},
			{
				Value: 0.6, Label: "中等可核验",
				Criteria:  "存在可指名的交付物或明确的完成结果，但缺少独立验证。",
				Positives: []string{"出现交付物名称或完成结果描述", "仅靠文件变更支撑而无验证输出"},
				Negatives: []string{"同时存在测试通过或外部确认（应升档）"},
			},
			{
				Value: 0.8, Label: "强可核验",
				Criteria:  "同时具备交付物（或文件变更）与验证输出，证据链完整。",
				Positives: []string{"有文件变更且有测试通过", "有交付物且构建/部署成功"},
				Negatives: []string{"验证输出只是助手文字描述（应降档）"},
			},
			{
				Value: 1, Label: "外部确认级",
				Criteria:  "获得系统外第三方确认，或工具执行结果 + 验证 + 外部确认三者齐备。",
				Positives: []string{"验收通过、录用、已发布、获奖", "工具执行结果且有外部确认"},
				Negatives: []string{"仅内部测试通过（应降档）"},
			},
		},
	}
}

func personalContributionRubric() FieldRubric {
	return FieldRubric{
		Field:   fieldPersonalContribution,
		Default: 0.5,
		Levels: []RubricLevel{
			{
				Value: 0.25, Label: "主要由助手完成",
				Criteria:  "本人仅提出需求，实质性工作由模型完成，无法区分本人的独立贡献。",
				Positives: []string{"一句话需求 + 助手直接产出结果"},
				Negatives: []string{"本人随后做了修改或验证（应升档）"},
			},
			{
				Value: 0.5, Label: "本人参与",
				Criteria:  "本人提供了判断、选择、修改或补充，但关键步骤仍由助手完成。",
				Positives: []string{"本人多次追问并修正方向", "本人对结果做了筛选或编辑"},
				Negatives: []string{"本人独立完成核心步骤（应升档）"},
			},
			{
				Value: 0.75, Label: "本人主导",
				Criteria:  "本人确定方案与关键决策，助手承担执行性工作。",
				Positives: []string{"本人给出明确方案、参数或验收标准", "本人主导多轮迭代并定稿"},
				Negatives: []string{"本人还承担了他人工作的组织或审核（视情况升档）"},
			},
			{
				Value: 1, Label: "本人独立完成",
				Criteria:  "核心思路、实现与验证均由本人完成，助手仅辅助查证或格式处理。",
				Positives: []string{"本人提供代码/文本主体并自行验证", "助手仅做校对或格式整理"},
				Negatives: []string{"核心实现来自助手（应降档）"},
			},
		},
	}
}

func complexityRubric() FieldRubric {
	return FieldRubric{
		Field:   fieldComplexity,
		Default: 0.5,
		Levels: []RubricLevel{
			{
				Value: 0.25, Label: "简单",
				Criteria:  "单一动作即可完成，无跨系统协作，无调试成本。",
				Positives: []string{"单次格式转换、单文件小改", "一次性问答"},
				Negatives: []string{"出现多轮调试（应升档）"},
			},
			{
				Value: 0.5, Label: "中等",
				Criteria:  "需要多轮迭代或涉及 2 个以上步骤/文件。",
				Positives: []string{"多轮修改与验证", "涉及少量文件变更"},
				Negatives: []string{"跨模块、跨系统联调（应升档）"},
			},
			{
				Value: 0.75, Label: "复杂",
				Criteria:  "跨模块或跨系统，需要设计权衡、故障排查或性能调优。",
				Positives: []string{"跨服务联调、数据库迁移、性能优化", "多文件重构并附带验证"},
				Negatives: []string{"从零搭建整套系统（应升档）"},
			},
			{
				Value: 1, Label: "高复杂度",
				Criteria:  "从零构建系统/平台，或涉及架构级改造、长周期实验、算法实现与调优。",
				Positives: []string{"新系统从零搭建并部署", "完整实验流程设计、实现、运行与分析"},
				Negatives: []string{"只是在既有系统上加一个小功能（应降档）"},
			},
		},
	}
}

// numericRubrics 返回所有数值型字段的档位表。
func numericRubrics() map[string]FieldRubric {
	return map[string]FieldRubric{
		fieldEvidenceQuality:      evidenceQualityRubric(),
		fieldPersonalContribution: personalContributionRubric(),
		fieldComplexity:           complexityRubric(),
	}
}

// validCalibrationFields 列出允许在线校准的字段。
func validCalibrationFields() []string {
	return []string{"stage", fieldEvidenceQuality, fieldPersonalContribution, fieldComplexity, fieldAllocation}
}

// validCalibrationField 判断字段名是否合法。
func validCalibrationField(value string) bool {
	for _, field := range validCalibrationFields() {
		if field == value {
			return true
		}
	}
	return false
}

// SnapToLadder 把任意浮点吸附到最近的合法档位。
// 非法值（NaN/Inf/非正）返回字段默认档位，ok=false。
func SnapToLadder(r FieldRubric, value float64) (float64, RubricLevel, bool) {
	if math.IsNaN(value) || math.IsInf(value, 0) || value <= 0 {
		return r.Default, rubricLevelOf(r, r.Default), false
	}
	best := r.Levels[0]
	for _, level := range r.Levels {
		if math.Abs(value-level.Value) < math.Abs(value-best.Value) {
			best = level
		}
	}
	ok := math.Abs(value-best.Value) <= 0.125
	return best.Value, best, ok
}

// rubricLevelOf 返回最接近给定值的档位定义，找不到时返回零值档位。
func rubricLevelOf(r FieldRubric, value float64) RubricLevel {
	for _, level := range r.Levels {
		if level.Value == value {
			return level
		}
	}
	best := r.Levels[0]
	for _, level := range r.Levels {
		if math.Abs(value-level.Value) < math.Abs(value-best.Value) {
			best = level
		}
	}
	return best
}

// LadderIndex 返回档位在表中的序号（0 起），非法值返回 -1。
func LadderIndex(r FieldRubric, value float64) int {
	for index, level := range r.Levels {
		if math.Abs(level.Value-value) < 1e-9 {
			return index
		}
	}
	return -1
}

// LadderDistance 返回两个档位之间相差的档数，任一非法则返回 0。
func LadderDistance(r FieldRubric, left, right float64) int {
	li, ri := LadderIndex(r, left), LadderIndex(r, right)
	if li < 0 || ri < 0 {
		return 0
	}
	return li - ri
}

// RenderAnchorCard 渲染为 prompt 中的锚点卡片文本。
func (r FieldRubric) RenderAnchorCard() string {
	parts := make([]string, 0, len(r.Levels))
	for _, level := range r.Levels {
		parts = append(parts, fmt.Sprintf("- %s（%s）：%s", formatLadderValue(level.Value), level.Label, level.Criteria))
	}
	return strings.Join(parts, "\n")
}

// RenderStageAnchorCard 渲染阶段字段的锚点卡片文本。
func RenderStageAnchorCard() string {
	parts := make([]string, 0, len(stageLadder()))
	for _, level := range stageLadder() {
		parts = append(parts, fmt.Sprintf("- %s（折算分 %s）：%s", level.Code, formatLadderValue(level.Score), level.Criteria))
	}
	return strings.Join(parts, "\n")
}

func formatLadderValue(value float64) string {
	if value == math.Trunc(value) {
		return fmt.Sprintf("%.0f", value)
	}
	return strings.TrimRight(strings.TrimRight(fmt.Sprintf("%.2f", value), "0"), ".")
}

// StageScore 返回阶段编码对应的折算分，非法编码返回 0。
func StageScore(code string) float64 {
	for _, level := range stageLadder() {
		if level.Code == code {
			return level.Score
		}
	}
	return 0
}

// StageRank 返回阶段在阶梯中的序号，非法编码返回 -1。
func StageRank(code string) int {
	for index, level := range stageLadder() {
		if level.Code == code {
			return index
		}
	}
	return -1
}

// NormalizeStageCode 把阶段编码规整到合法集合，未知值归为 intent。
func NormalizeStageCode(code string) string {
	normalized := strings.ToLower(strings.TrimSpace(code))
	if StageRank(normalized) >= 0 {
		return normalized
	}
	return "intent"
}

// StageAtLeast 判断 left 阶段不低于 right 阶段。
func StageAtLeast(left, right string) bool {
	li, ri := StageRank(NormalizeStageCode(left)), StageRank(NormalizeStageCode(right))
	if li < 0 || ri < 0 {
		return false
	}
	return li >= ri
}

// SortStageByScore 返回证据卡集合中排名最高的阶段（与现网 cluster 取最高分行为一致）。
func SortStageByScore(codes []string) (string, float64) {
	best := "intent"
	bestScore := 0.0
	for _, code := range codes {
		normalized := NormalizeStageCode(code)
		if score := StageScore(normalized); score > bestScore {
			best, bestScore = normalized, score
		}
	}
	return best, bestScore
}
