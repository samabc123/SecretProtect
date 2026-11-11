package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type reportGoalMatch struct {
	GoalIndex        int     `json:"goal_index"`
	WorkItemIDs      []int64 `json:"work_item_ids"`
	UnfinishedReason string  `json:"unfinished_reason"`
	Measures         string  `json:"measures"`
}

type reportGoalMatchPayload struct {
	Matches []reportGoalMatch `json:"matches"`
}

type v4ProjectProsePayload struct {
	Item reportProjectNarrative `json:"item"`
}

func (a *app) generateReportV4(ctx context.Context, job reportGenerationJob, start time.Time, template reportTemplateRecord) (map[string]any, error) {
	end := start.AddDate(0, 0, 7)
	metrics, source, err := a.collectMetrics(ctx, job.UserID, start, end)
	if err != nil {
		return nil, err
	}
	actorID := int64(0)
	if job.RequestedBy != nil {
		actorID = *job.RequestedBy
	}
	previousPlan := a.previousWeekPlan(ctx, job.UserID, start, job.RebuildBaseline, actorID, job.ID)

	a.updateReportJobStage(ctx, job.ID, "extracting_evidence")
	evidenceCounts, err := a.ensureWeeklyEvidenceCards(ctx, job.UserID, start, end)
	if err != nil {
		return nil, fmt.Errorf("逐条提取证据失败：%w", err)
	}
	for key, value := range evidenceCounts {
		source["weekly_evidence_"+key] = value
	}

	a.updateReportJobStage(ctx, job.ID, "aggregating_work_items")
	workItems, err := a.buildWeeklyWorkItems(ctx, job.UserID, start, template)
	if err != nil {
		return nil, fmt.Errorf("任务聚合失败：%w", err)
	}
	source["weekly_work_items"] = len(workItems)
	fallbackClassifications := 0
	for _, item := range workItems {
		if item.ClassificationStatus == "fallback" {
			fallbackClassifications++
		}
	}

	narrative := reportV3Narrative{
		AnalysisVersion:  5,
		TemplateVersion:  template.Version,
		PreviousWeekPlan: previousPlan,
	}
	if previousPlan.Status == "failed" {
		narrative.GenerationErrors = append(narrative.GenerationErrors, "上周计划基线："+previousPlan.ErrorMessage)
	}
	if fallbackClassifications > 0 {
		narrative.GenerationErrors = append(narrative.GenerationErrors, fmt.Sprintf("%d 个任务的 DeepSeek 归因失败，已保留规则回退结果并将在下次生成时重试", fallbackClassifications))
	}

	a.updateReportJobStage(ctx, job.ID, "scoring_projects")
	// 一次读取全部项目的历史得分，供异常跳变检测使用。
	scoreHistory := a.recentProjectScoreHistory(ctx, job.UserID, start, 8)
	goalItem, goalErr := a.buildV4GoalProject(ctx, template, previousPlan, workItems)
	if goalErr != nil {
		narrative.GenerationErrors = append(narrative.GenerationErrors, "项目1计划匹配："+goalErr.Error())
	}
	for _, section := range template.Definition.Sections {
		result := reportSectionNarrative{ID: section.ID, Number: section.Number, Title: section.Title, Description: section.Description}
		for _, templateItem := range section.Items {
			if !templateItem.Enabled {
				continue
			}
			if templateItem.ID == 1 {
				result.Items = append(result.Items, goalItem)
				continue
			}
			a.updateReportJobStage(ctx, job.ID, "writing_item_"+strconv.Itoa(templateItem.ID))
			item, itemErr := a.buildV4Project(ctx, templateItem, workItems, source, start, end.AddDate(0, 0, -1), scoreHistory[templateItem.ID])
			if itemErr != nil {
				narrative.GenerationErrors = append(narrative.GenerationErrors, templateItem.Title+"："+itemErr.Error())
			}
			result.Items = append(result.Items, item)
		}
		narrative.Sections = append(narrative.Sections, result)
	}

	logScoringSummary(job.UserID, start, narrative.Sections)

	a.updateReportJobStage(ctx, job.ID, "synthesizing")
	summary, summaryErr := a.generateReportSummary(ctx, template, narrative.Sections, metrics, source, previousPlan, start, end.AddDate(0, 0, -1))
	if summaryErr != nil {
		narrative.GenerationErrors = append(narrative.GenerationErrors, "综合摘要："+summaryErr.Error())
		summary = fallbackReportSummary(metrics, source, narrative.Sections)
	}
	narrative.ExecutiveSummary = summary.ExecutiveSummary
	narrative.OverallProgress = summary.OverallProgress
	narrative.NextWeekGoals = summary.NextWeekGoals
	narrative.DataQualityNotes = summary.DataQualityNotes
	narrative.DataQualityNotes = append(narrative.DataQualityNotes,
		fmt.Sprintf("v4 逐条取证共处理对话 %d 条、Agent 轨迹 %d 条、成员补充证据 %d 条，聚合为 %d 个去重任务。", evidenceCounts["conversation"], evidenceCounts["trajectory"], evidenceCounts["manual"], len(workItems)),
		"usage_logs 仅用于请求数、Token、成本和活跃度统计，不作为科研成果的独立证明。",
		"未发现证据的项目建议分为 0，仅表示本周云端数据中没有可核验来源，不推断线下工作是否开展。",
	)
	if previousPlan.Status == "failed" {
		narrative.DataQualityNotes = append(narrative.DataQualityNotes, "上一周计划重建失败："+previousPlan.ErrorMessage)
	} else if previousPlan.Status != "ready" {
		narrative.DataQualityNotes = append(narrative.DataQualityNotes, "上一周计划尚未重建，项目1不使用更早周计划替代，建议分为0并等待人工确认。")
	}

	status := "ready"
	if len(narrative.GenerationErrors) > 0 {
		status = "partial"
	}
	if status == "partial" {
		if existing, loadErr := a.loadSuccessfulReportForWeek(ctx, job.UserID, start); loadErr == nil {
			return existing, fmt.Errorf("v4 重新生成未完整完成，已保留原成功周报：%s", strings.Join(narrative.GenerationErrors, "；"))
		}
	}
	return a.saveV4Report(ctx, job.UserID, start, template, status, metrics, narrative, source, strings.Join(narrative.GenerationErrors, "；"))
}

// logScoringSummary 输出单次生成的评分画像，便于事后定位系统性偏差。
// 只记录聚合数值与编号，严禁记录原始对话或用户正文。
func logScoringSummary(userID int64, start time.Time, sections []reportSectionNarrative) {
	total := 0
	confidenceTotal := 0.0
	lowConfidence := 0
	reviewRequired := 0
	flags := make(map[string]int)
	for _, section := range sections {
		for _, item := range section.Items {
			total++
			confidenceTotal += item.ConfidenceScore
			if item.Confidence == "low" {
				lowConfidence++
			}
			if item.RequiresManualReview {
				reviewRequired++
			}
			for _, flag := range item.AnomalyFlags {
				flags[flag]++
			}
		}
	}
	if total == 0 {
		return
	}
	flagText := make([]string, 0, len(flags))
	for name, count := range flags {
		flagText = append(flagText, name+"="+strconv.Itoa(count))
	}
	logWarningf("user=%d week=%s items=%d avg_confidence=%.3f low_confidence=%d review_required=%d flags=%s",
		userID, start.Format("2006-01-02"), total, confidenceTotal/float64(total), lowConfidence, reviewRequired, strings.Join(flagText, ","))
}

func (a *app) buildV4GoalProject(ctx context.Context, template reportTemplateRecord, plan reportPlanSnapshot, workItems []weeklyWorkItem) (reportProjectNarrative, error) {
	templateItem := templateItemMap(template.Definition)[1]
	zero := 0.0
	item := reportProjectNarrative{
		ItemID: 1, Title: templateItem.Title, ScoreRule: templateItem.ScoreRule, MaxScore: templateItem.MaxScore,
		ScoringMode: templateItem.ScoringMode, Status: "no_evidence", AIScore: &zero, Confidence: "low",
		RequiresManualReview: true,
	}
	breakdown := reportScoreBreakdown{Formula: "30 × P0/P1/P2 加权完成度；P0/P1/P2 权重分别为3/2/1", MaxScore: templateItem.MaxScore, Components: []reportScoreComponent{}}
	item.ScoreBreakdown = &breakdown
	if plan.Status != "ready" || len(plan.Goals) == 0 {
		item.GoalRows = alignGoalRowsWithPlan(nil, plan)
		item.CompletedContent = "上一自然周没有可用计划快照，因此无法把本周任务逐项与上周计划进行对比。系统未采用两周前或更早计划，也未把本周形成的建议倒填为历史基线。"
		item.IssuesAndActions = "恢复上一周计划快照或补充上一周任务数据后重新生成；在基线恢复前，本项建议分固定为0并等待管理员确认。"
		item.ScoreBasis = "无严格上一周计划基线，公式不具备合法输入，建议分为0。"
		return item, nil
	}
	matches, matchErr := a.matchPlanGoalsToWorkItems(ctx, plan, workItems)
	byID := make(map[int64]weeklyWorkItem, len(workItems))
	for _, workItem := range workItems {
		byID[workItem.ID] = workItem
	}
	matchByGoal := make(map[int]reportGoalMatch)
	for _, match := range matches {
		if match.GoalIndex >= 0 && match.GoalIndex < len(plan.Goals) {
			matchByGoal[match.GoalIndex] = match
		}
	}
	weightedCompletion := 0.0
	weightTotal := 0.0
	allRefs := make([]string, 0)
	linkedItems := make([]weeklyWorkItem, 0)
	seenWork := make(map[int64]bool)
	for index, goal := range plan.Goals {
		weight := goalPriorityWeight(goal.Priority)
		weightTotal += weight
		match := matchByGoal[index]
		completion := 0.0
		evidence := make([]string, 0)
		completedParts := make([]string, 0)
		for _, workItemID := range match.WorkItemIDs {
			workItem, ok := byID[workItemID]
			if !ok {
				continue
			}
			value := clamp01(workItem.StageScore, 0) * clamp01(workItem.EvidenceQuality, 0) * clamp01(workItem.PersonalContribution, 0.5)
			if value > completion {
				completion = value
			}
			completedParts = append(completedParts, workItem.Title+"："+workItem.Summary)
			for _, ref := range workItem.SourceRefs {
				evidence = append(evidence, ref)
				allRefs = append(allRefs, ref)
			}
			if !seenWork[workItem.ID] {
				seenWork[workItem.ID] = true
				linkedItems = append(linkedItems, workItem)
			}
		}
		weightedCompletion += weight * completion
		percent := math.Round(completion*1000) / 10
		row := reportGoalRow{
			Goal: goal.Goal, CompletionPercent: &percent, Evidence: normalizeStringItems(evidence, 100, 100),
			CompletedContent: truncateNarrativeRunes(strings.Join(completedParts, "；"), 1600),
			UnfinishedReason: truncateNarrativeRunes(match.UnfinishedReason, 800), Measures: truncateNarrativeRunes(match.Measures, 800),
		}
		if row.CompletedContent == "" {
			row.CompletedContent = "本周逐条证据中未找到能够与该计划直接匹配的已留痕任务。"
			if row.UnfinishedReason == "" {
				row.UnfinishedReason = "仅能确认当前云端归档中没有直接匹配证据，无法据此判断线下执行情况。"
			}
		}
		item.GoalRows = append(item.GoalRows, row)
		breakdown.Components = append(breakdown.Components, reportScoreComponent{
			Title: goal.Priority + " " + goal.Goal, BasePoints: 30 * weight, StageScore: completion,
			EvidenceQuality: 1, Contribution: 1, Allocation: 100, Complexity: 1, UnitCount: 1,
		})
	}
	if weightTotal > 0 {
		weightedCompletion /= weightTotal
	}
	score := roundHalf(30 * weightedCompletion)
	for index := range breakdown.Components {
		component := &breakdown.Components[index]
		component.BasePoints /= weightTotal
		component.Score = roundHalf(component.BasePoints * component.StageScore)
	}
	breakdown.EvidenceCount = len(normalizeStringItems(allRefs, 10000, 100))
	breakdown.RawScore = math.Round(30*weightedCompletion*100) / 100
	breakdown.FinalScore = score
	item.AIScore = &score
	item.CompletionPercent = floatPointer(math.Round(weightedCompletion*1000) / 10)
	item.WorkItems = linkedItems
	item.SourceRefs = normalizeStringItems(allRefs, 10000, 100)
	item.ScoreBreakdown = &breakdown
	item.EvidenceQuality = maxWorkItemQuality(linkedItems)
	item.CompletedContent = fmt.Sprintf("系统严格读取 %s 周计划并与本周 %d 个去重任务逐项匹配，按 P0/P1/P2 权重 3/2/1 计算加权完成度 %.1f%%。计划来源与本周完成证据分别保留，未使用更早周计划。", plan.WeekStart, len(linkedItems), weightedCompletion*100)
	item.ProcessAndMethod = "每项目标只接受本周 work_item 关联的真实来源编号；完成度取匹配任务中最高的阶段系数×证据质量×个人贡献，避免同一目标因重复对话、重试或总结被重复累计。"
	item.DeliverablesAndEvidence = normalizeStringItems(allRefs, 100, 100)
	item.OutcomeAndImpact = "该分数反映云端留痕对上一周计划的可核验兑现程度，不代表未归档线下工作的实际完成度。"
	item.IssuesAndActions = "对未匹配或证据质量较低的计划，应补充交付物、测试结果或外部确认；管理员可结合线下事实确认最终分。"
	item.ScoreBasis = fmt.Sprintf("30 × %.4f = %.2f，按0.5分取整为 %.1f 分。", weightedCompletion, 30*weightedCompletion, score)
	item.Status = "in_progress"
	if weightedCompletion >= 0.7 {
		item.Status = "completed"
	}
	item.ConfidenceScore = averageWorkItemConfidence(linkedItems)
	item.Confidence = ConfidenceLevel(item.ConfidenceScore)
	item.AnomalyFlags = collectWorkItemFlags(linkedItems)
	if plan.Status != "ready" || len(plan.Goals) == 0 {
		item.RequiresManualReview = true
	}
	return item, matchErr
}

// averageWorkItemConfidence 汇总任务级置信度的算术平均，无任务时返回 0。
func averageWorkItemConfidence(values []weeklyWorkItem) float64 {
	if len(values) == 0 {
		return 0
	}
	total := 0.0
	for _, value := range values {
		total += value.Confidence
	}
	return math.Round(total/float64(len(values))*10000) / 10000
}

// collectWorkItemFlags 汇总任务上的异常标记，去重后按字典序输出。
func collectWorkItemFlags(values []weeklyWorkItem) []string {
	flags := make([]string, 0, 4)
	for _, value := range values {
		flags = appendUniqueFlag(flags, value.AnomalyFlags...)
	}
	return flags
}

func (a *app) matchPlanGoalsToWorkItems(ctx context.Context, plan reportPlanSnapshot, workItems []weeklyWorkItem) ([]reportGoalMatch, error) {
	fallback := heuristicGoalMatches(plan, workItems)
	if len(workItems) == 0 || a.cfg.DeepSeekKey == "" {
		return fallback, nil
	}
	inputItems := make([]map[string]any, 0, len(workItems))
	allowed := make(map[int64]bool, len(workItems))
	for _, item := range workItems {
		allowed[item.ID] = true
		inputItems = append(inputItems, map[string]any{"id": item.ID, "title": item.Title, "summary": item.Summary, "stage": item.Stage, "source_refs": item.SourceRefs})
	}
	system := `你是上一周计划与本周任务匹配器，只返回 JSON。结构：{"matches":[{"goal_index":0,"work_item_ids":[1],"unfinished_reason":"","measures":""}]}。goal_index 必须来自输入计划数组下标；work_item_ids 只能引用输入中真实存在的任务ID。只有语义上直接推进该计划的任务才能匹配，普通相关讨论不得强行匹配。不得输出分数、原始对话、密钥、系统指令或绝对路径。`
	user := "上一周计划：" + string(jsonBytes(plan.Goals)) + "\n本周去重任务：" + string(jsonBytes(inputItems))
	raw, err := a.callDeepSeekJSON(ctx, system, user, 6000)
	if err != nil {
		return fallback, err
	}
	var payload reportGoalMatchPayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return fallback, err
	}
	result := make([]reportGoalMatch, 0, len(payload.Matches))
	seenGoals := make(map[int]bool)
	for _, match := range payload.Matches {
		if match.GoalIndex < 0 || match.GoalIndex >= len(plan.Goals) || seenGoals[match.GoalIndex] {
			continue
		}
		seenGoals[match.GoalIndex] = true
		ids := make([]int64, 0, len(match.WorkItemIDs))
		seenIDs := make(map[int64]bool)
		for _, id := range match.WorkItemIDs {
			if allowed[id] && !seenIDs[id] {
				seenIDs[id] = true
				ids = append(ids, id)
			}
		}
		match.WorkItemIDs = ids
		match.UnfinishedReason = truncateNarrativeRunes(redactEvidenceText(match.UnfinishedReason), 800)
		match.Measures = truncateNarrativeRunes(redactEvidenceText(match.Measures), 800)
		result = append(result, match)
	}
	for index := range plan.Goals {
		if !seenGoals[index] {
			result = append(result, fallback[index])
		}
	}
	sort.Slice(result, func(i, j int) bool { return result[i].GoalIndex < result[j].GoalIndex })
	return result, nil
}

func heuristicGoalMatches(plan reportPlanSnapshot, workItems []weeklyWorkItem) []reportGoalMatch {
	result := make([]reportGoalMatch, 0, len(plan.Goals))
	for index, goal := range plan.Goals {
		goalKey := canonicalTaskKey(goal.Goal + " " + strings.Join(goal.Actions, " "))
		match := reportGoalMatch{GoalIndex: index, Measures: "继续记录具体交付物、验证结果和责任范围，以便形成可复核完成链条。"}
		for _, item := range workItems {
			itemKey := canonicalTaskKey(item.Title + " " + item.Summary)
			if taskKeysOverlap(goalKey, itemKey) {
				match.WorkItemIDs = append(match.WorkItemIDs, item.ID)
			}
		}
		if len(match.WorkItemIDs) == 0 {
			match.UnfinishedReason = "本周逐条证据中未找到与该计划直接匹配的任务。"
		}
		result = append(result, match)
	}
	return result
}

func taskKeysOverlap(left, right string) bool {
	if len([]rune(left)) >= 8 && strings.Contains(right, left) {
		return true
	}
	if len([]rune(right)) >= 8 && strings.Contains(left, right) {
		return true
	}
	leftRunes := []rune(left)
	if len(leftRunes) > 48 {
		leftRunes = leftRunes[:48]
	}
	matched := 0
	seen := make(map[rune]bool)
	for _, value := range leftRunes {
		if seen[value] {
			continue
		}
		seen[value] = true
		if strings.ContainsRune(right, value) {
			matched++
		}
	}
	return matched >= 6 && matched*2 >= len(seen)
}

func goalPriorityWeight(value string) float64 {
	switch strings.ToUpper(strings.TrimSpace(value)) {
	case "P0":
		return 3
	case "P2":
		return 1
	default:
		return 2
	}
}

func (a *app) buildV4Project(ctx context.Context, templateItem reportTemplateItem, workItems []weeklyWorkItem, source map[string]int, start, end time.Time, history []float64) (reportProjectNarrative, error) {
	projectWork, scoreInputs := workItemsForProject(workItems, templateItem.ID)
	score, breakdown := a.calculateProjectScore(ctx, templateItem, scoreInputs, workItemSignalMap(workItems), workItemConfidenceMap(workItems), history)
	a.updateAttributionScoreComponents(ctx, scoreInputs, breakdown)
	if len(projectWork) == 0 {
		zero := 0.0
		item := fallbackProjectNarrative(templateItem, reportItemEvidence{}, source, reportPlanSnapshot{})
		item.AIScore = &zero
		item.ScoreBasis = "本周未发现可核验证据，按照 v4 统一规则建议分为0；这不等同于认定成员实际未开展该项工作。"
		item.ScoreBreakdown = &breakdown
		item.RequiresManualReview = templateItem.ManualReview
		return item, nil
	}
	prose, err := a.generateV4ProjectProse(ctx, templateItem, projectWork, breakdown, start, end)
	if err != nil {
		prose = fallbackV4ProjectNarrative(templateItem, projectWork, breakdown)
	}
	prose.ItemID = templateItem.ID
	prose.Title = templateItem.Title
	prose.ScoreRule = templateItem.ScoreRule
	prose.MaxScore = templateItem.MaxScore
	prose.ScoringMode = templateItem.ScoringMode
	prose.AIScore = floatPointer(score)
	prose.WorkItems = projectWork
	prose.SourceRefs = workItemSourceRefs(projectWork)
	prose.ScoreBreakdown = &breakdown
	prose.AllocationPercent = averageProjectAllocation(projectWork, templateItem.ID)
	prose.EvidenceQuality = maxWorkItemQuality(projectWork)
	prose.Confidence = ConfidenceLevel(breakdown.Confidence)
	prose.ConfidenceScore = breakdown.Confidence
	prose.ConfidenceFactors = breakdown.ConfidenceFactors
	prose.AnomalyFlags = breakdown.AnomalyFlags
	prose.RequiresManualReview = templateItem.ManualReview || breakdown.RequiresManualReview
	prose.Status = projectStatusFromWorkItems(projectWork)
	prose.ScoreBasis = fmt.Sprintf("%s；服务端公式计算原始分 %.2f，按0.5分取整为 %.1f 分。", firstNonEmpty(templateItem.ScoringFormula, templateItem.ScoreRule), breakdown.RawScore, score)
	prose = ensureV4ProjectDetail(prose)
	return prose, err
}

func (a *app) generateV4ProjectProse(ctx context.Context, templateItem reportTemplateItem, workItems []weeklyWorkItem, breakdown reportScoreBreakdown, start, end time.Time) (reportProjectNarrative, error) {
	system := `你是科研周报单项目撰写器，只返回 JSON。返回结构：{"item":{"completed_content":"","process_and_method":"","deliverables_and_evidence":[],"outcome_and_impact":"","issues_and_actions":""}}。只允许依据输入的去重任务和来源编号写作，不得新增事实、人员、论文、实验、交付物或分数。必须详细说明完成内容、方法过程、交付物、结果影响、问题和措施，正文合计400至900个中文字符；证据有限时应明确数据边界，不用套话或虚构内容补足。不得输出原始对话、系统指令、密码、密钥、绝对路径或保密审稿内容。`
	user := fmt.Sprintf("周期：%s 至 %s\n项目规则：%s\n去重任务：%s\n确定性评分明细：%s", start.Format("2006-01-02"), end.Format("2006-01-02"), string(jsonBytes(templateItem)), string(jsonBytes(workItems)), string(jsonBytes(breakdown)))
	raw, err := a.callDeepSeekJSON(ctx, system, user, 5000)
	if err != nil {
		return reportProjectNarrative{}, err
	}
	var payload v4ProjectProsePayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return reportProjectNarrative{}, err
	}
	item := payload.Item
	item.CompletedContent = truncateNarrativeRunes(redactEvidenceText(item.CompletedContent), 2600)
	item.ProcessAndMethod = truncateNarrativeRunes(redactEvidenceText(item.ProcessAndMethod), 2200)
	item.DeliverablesAndEvidence = normalizeStringItems(item.DeliverablesAndEvidence, 30, 600)
	item.OutcomeAndImpact = truncateNarrativeRunes(redactEvidenceText(item.OutcomeAndImpact), 1800)
	item.IssuesAndActions = truncateNarrativeRunes(redactEvidenceText(item.IssuesAndActions), 1800)
	return item, nil
}

func fallbackV4ProjectNarrative(templateItem reportTemplateItem, workItems []weeklyWorkItem, breakdown reportScoreBreakdown) reportProjectNarrative {
	titles := make([]string, 0, len(workItems))
	methods := make([]string, 0, len(workItems))
	evidence := make([]string, 0)
	verified := make([]string, 0)
	for _, item := range workItems {
		titles = append(titles, item.Title+"："+item.Summary)
		methods = append(methods, fmt.Sprintf("任务“%s”处于 %s 阶段，证据质量 %.2f，个人贡献 %.2f，复杂度 %.2f。", item.Title, item.Stage, item.EvidenceQuality, item.PersonalContribution, item.Complexity))
		for _, ref := range item.SourceRefs {
			evidence = append(evidence, ref)
		}
		verified = append(verified, item.Verification...)
	}
	return reportProjectNarrative{
		ItemID: templateItem.ID, Title: templateItem.Title,
		CompletedContent:        truncateNarrativeRunes(strings.Join(titles, "；"), 2400),
		ProcessAndMethod:        truncateNarrativeRunes(strings.Join(methods, "；"), 2200),
		DeliverablesAndEvidence: normalizeStringItems(append(evidence, verified...), 30, 600),
		OutcomeAndImpact:        "当前结果影响只依据交付物、文件变更、测试或外部确认等已留痕信号判断；没有来源支持的使用范围、质量评价和后续采纳情况不作推断。",
		IssuesAndActions:        "本项文字阶段使用确定性回退生成。建议补充任务对象、本人动作、交付物版本、验证结果与确认人，并在模型服务恢复后重新生成详细文字；现有建议分仍由服务端公式计算。",
		ScoreBreakdown:          &breakdown,
	}
}

func ensureV4ProjectDetail(item reportProjectNarrative) reportProjectNarrative {
	if projectNarrativeBodyLength(item) >= 400 {
		return item
	}
	boundary := fmt.Sprintf("证据覆盖说明：本项由 %d 个去重任务和 %d 个真实来源编号支持。任务阶段、证据质量、个人贡献、跨项目分配比例和复杂度均单独保存，并由服务端公式计算建议分；同一线程的重复询问、重试和总结不会重复计分。来源编号只证明云端归档中出现过对应脱敏任务链，未归档的线下工作不在判断范围内。待审核的成员补充证据按质量系数0.60进入草稿建议分，管理员批准后按1.00重新计算，拒绝后不进入评分。当前文字不把用户提出的要求当作完成，不把助手单方面完成声明等同于工具验证，也不把普通模型使用量认定为科研成果。", len(item.WorkItems), len(item.SourceRefs))
	item.IssuesAndActions = strings.TrimSpace(item.IssuesAndActions + "\n" + boundary)
	return item
}

func projectStatusFromWorkItems(values []weeklyWorkItem) string {
	best := 0.0
	for _, value := range values {
		if value.StageScore > best {
			best = value.StageScore
		}
	}
	if best >= 0.7 {
		return "completed"
	}
	return "in_progress"
}

func workItemSourceRefs(values []weeklyWorkItem) []string {
	refs := make([]string, 0)
	for _, item := range values {
		refs = append(refs, item.SourceRefs...)
	}
	return normalizeStringItems(refs, 10000, 100)
}

func maxWorkItemQuality(values []weeklyWorkItem) float64 {
	result := 0.0
	for _, item := range values {
		if item.EvidenceQuality > result {
			result = item.EvidenceQuality
		}
	}
	return result
}

func averageProjectAllocation(values []weeklyWorkItem, itemID int) float64 {
	total := 0.0
	count := 0.0
	for _, item := range values {
		for _, attribution := range item.Attributions {
			if attribution.ItemID == itemID {
				total += attribution.AllocationPercent
				count++
			}
		}
	}
	if count == 0 {
		return 0
	}
	return math.Round(total/count*100) / 100
}

func confidenceFromQuality(value float64) string {
	if value >= 0.85 {
		return "high"
	}
	if value >= 0.6 {
		return "medium"
	}
	return "low"
}

func floatPointer(value float64) *float64 {
	return &value
}

func (a *app) loadSuccessfulReportForWeek(ctx context.Context, userID int64, start time.Time) (map[string]any, error) {
	var reportID int64
	err := a.db.QueryRow(ctx, `SELECT id FROM weekly_reports WHERE user_id=$1 AND week_start=$2 AND status IN ('ready','partial','metrics_only')`, userID, start.Format("2006-01-02")).Scan(&reportID)
	if err != nil {
		return nil, err
	}
	return a.loadReportByID(ctx, reportID)
}

func (a *app) saveV4Report(ctx context.Context, userID int64, start time.Time, template reportTemplateRecord, status string, metrics map[string]any, narrative reportV3Narrative, source map[string]int, errorMessage string) (map[string]any, error) {
	end := start.AddDate(0, 0, 6)
	now := time.Now().UTC()
	var reportID int64
	err := a.db.QueryRow(ctx, `INSERT INTO weekly_reports(user_id,week_start,week_end,status,metrics,narrative,source_counts,model,error_message,generated_at,template_id,analysis_version,updated_at)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,NULLIF($9,''),$10,$11,5,now())
		ON CONFLICT(user_id,week_start) DO UPDATE SET week_end=excluded.week_end,status=excluded.status,metrics=excluded.metrics,narrative=excluded.narrative,
			source_counts=excluded.source_counts,model=excluded.model,error_message=excluded.error_message,generated_at=excluded.generated_at,
			template_id=excluded.template_id,analysis_version=5,updated_at=now()
		RETURNING id`, userID, start.Format("2006-01-02"), end.Format("2006-01-02"), status, jsonBytes(metrics), jsonBytes(narrative), jsonBytes(source), a.cfg.DeepSeekModel, errorMessage, now, template.ID).Scan(&reportID)
	if err != nil {
		return nil, err
	}
	if len(narrative.NextWeekGoals) > 0 {
		goals := normalizePlanGoals(narrative.NextWeekGoals, start)
		if _, err := a.saveReadyReportPlan(ctx, reportPlanSnapshot{UserID: userID, WeekStart: start.Format("2006-01-02"), Status: "ready", Source: "report_summary", Goals: goals, SourceCounts: source, Model: a.cfg.DeepSeekModel}); err != nil {
			return nil, fmt.Errorf("保存下周计划快照失败：%w", err)
		}
	}
	cutoff := start.Format("2006-01-02")
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_reports WHERE user_id=$1 AND week_start < $2::date - INTERVAL '52 weeks'`, userID, cutoff)
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_report_plans WHERE user_id=$1 AND week_start < $2::date - INTERVAL '52 weeks'`, userID, cutoff)
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_work_items WHERE user_id=$1 AND week_start < $2::date - INTERVAL '52 weeks'`, userID, cutoff)
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_evidence_cards WHERE user_id=$1 AND week_start < $2::date - INTERVAL '52 weeks'`, userID, cutoff)
	_, _ = a.db.Exec(ctx, `DELETE FROM weekly_manual_evidence WHERE user_id=$1 AND week_start < $2::date - INTERVAL '52 weeks'`, userID, cutoff)
	if status == "ready" || status == "partial" {
		a.queueWeeklyReportKnowledge(ctx, userID, reportID)
	}
	return a.loadReportByID(ctx, reportID)
}

var _ = pgx.ErrNoRows
