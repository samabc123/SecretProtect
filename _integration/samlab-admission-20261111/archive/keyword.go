package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"log"
	"math"
	"sort"
	"strings"
	"time"
	"unicode"
)

const (
	keywordSnapshotLookback   = 30 * 24 * time.Hour
	keywordSnapshotFirstDelay = 1 * time.Minute
	keywordSnapshotRetryDelay = 30 * time.Minute
	keywordSnapshotTimeout    = 10 * time.Minute
	keywordSnapshotHour       = 3
)

type archiveKeywordRule struct {
	Keyword  string
	Category string
	Aliases  []string
}

type extractedArchiveKeyword struct {
	Keyword  string
	Category string
	Count    int
}

type archiveKeywordCount struct {
	Keyword       string  `json:"keyword"`
	Category      string  `json:"category"`
	Occurrences   int64   `json:"occurrences"`
	Conversations int64   `json:"conversations"`
	Users         int64   `json:"users"`
	Weight        float64 `json:"weight"`
}

type archiveKeywordStats struct {
	Days               int                   `json:"days"`
	GeneratedAt        time.Time             `json:"generated_at"`
	ProcessedDocuments int64                 `json:"processed_documents"`
	EligibleDocuments  int64                 `json:"eligible_documents"`
	CoveredUsers       int64                 `json:"covered_users"`
	Items              []archiveKeywordCount `json:"items"`
}

type archiveKeywordStore interface {
	RefreshArchiveKeywordSnapshots(context.Context, time.Duration) (int, error)
	ListArchiveKeywordStats(context.Context, int, int) (archiveKeywordStats, error)
}

var archiveKeywordRules = []archiveKeywordRule{
	{Keyword: "基础概念", Category: "research", Aliases: []string{"基础知识", "概念理解", "基本原理"}},
	{Keyword: "文献检索", Category: "literature", Aliases: []string{"文献检索", "论文检索", "检索文献"}},
	{Keyword: "文献阅读", Category: "literature", Aliases: []string{"文献阅读", "论文阅读", "阅读论文"}},
	{Keyword: "参考文献", Category: "literature", Aliases: []string{"参考文献", "引用格式", "bibliography"}},
	{Keyword: "研究选题", Category: "research", Aliases: []string{"研究选题", "论文选题", "课题选择"}},
	{Keyword: "创新点", Category: "research", Aliases: []string{"创新点", "研究创新", "创新性"}},
	{Keyword: "研究方法", Category: "research", Aliases: []string{"研究方法", "研究思路", "技术路线"}},
	{Keyword: "实验设计", Category: "experiment", Aliases: []string{"实验设计", "实验方案", "试验设计"}},
	{Keyword: "实验结果", Category: "experiment", Aliases: []string{"实验结果", "试验结果"}},
	{Keyword: "结果分析", Category: "analysis", Aliases: []string{"结果分析", "结果解释", "分析结果"}},
	{Keyword: "消融实验", Category: "experiment", Aliases: []string{"消融实验", "ablation"}},
	{Keyword: "对比实验", Category: "experiment", Aliases: []string{"对比实验", "对照实验"}},
	{Keyword: "模型训练", Category: "model", Aliases: []string{"模型训练", "训练模型", "model training"}},
	{Keyword: "模型评估", Category: "model", Aliases: []string{"模型评估", "评估模型", "model evaluation"}},
	{Keyword: "模型优化", Category: "model", Aliases: []string{"模型优化", "优化模型", "model optimization"}},
	{Keyword: "大模型", Category: "model", Aliases: []string{"大语言模型", "大模型", "llm"}},
	{Keyword: "机器学习", Category: "model", Aliases: []string{"机器学习", "machine learning"}},
	{Keyword: "深度学习", Category: "model", Aliases: []string{"深度学习", "deep learning"}},
	{Keyword: "神经网络", Category: "model", Aliases: []string{"神经网络", "neural network"}},
	{Keyword: "Transformer", Category: "model", Aliases: []string{"transformer"}},
	{Keyword: "注意力机制", Category: "model", Aliases: []string{"注意力机制", "attention mechanism"}},
	{Keyword: "强化学习", Category: "model", Aliases: []string{"强化学习", "reinforcement learning"}},
	{Keyword: "计算机视觉", Category: "model", Aliases: []string{"计算机视觉", "computer vision"}},
	{Keyword: "自然语言处理", Category: "model", Aliases: []string{"自然语言处理", "nlp"}},
	{Keyword: "RAG", Category: "model", Aliases: []string{"rag", "检索增强生成"}},
	{Keyword: "知识库", Category: "data", Aliases: []string{"知识库", "知识图谱", "knowledge base"}},
	{Keyword: "数据集", Category: "data", Aliases: []string{"数据集", "dataset"}},
	{Keyword: "数据清洗", Category: "data", Aliases: []string{"数据清洗", "清洗数据", "data cleaning"}},
	{Keyword: "数据标注", Category: "data", Aliases: []string{"数据标注", "标注数据", "data annotation"}},
	{Keyword: "数据分析", Category: "analysis", Aliases: []string{"数据分析", "分析数据", "data analysis"}},
	{Keyword: "数据可视化", Category: "analysis", Aliases: []string{"数据可视化", "可视化分析", "data visualization"}},
	{Keyword: "统计分析", Category: "analysis", Aliases: []string{"统计分析", "统计检验", "statistical analysis"}},
	{Keyword: "特征工程", Category: "data", Aliases: []string{"特征工程", "feature engineering"}},
	{Keyword: "超参数", Category: "model", Aliases: []string{"超参数", "hyperparameter"}},
	{Keyword: "损失函数", Category: "model", Aliases: []string{"损失函数", "loss function"}},
	{Keyword: "过拟合", Category: "model", Aliases: []string{"过拟合", "overfitting"}},
	{Keyword: "交叉验证", Category: "analysis", Aliases: []string{"交叉验证", "cross validation"}},
	{Keyword: "分类", Category: "analysis", Aliases: []string{"分类模型", "分类算法", "classification"}},
	{Keyword: "回归", Category: "analysis", Aliases: []string{"回归模型", "回归分析", "regression"}},
	{Keyword: "聚类", Category: "analysis", Aliases: []string{"聚类分析", "聚类算法", "clustering"}},
	{Keyword: "时间序列", Category: "analysis", Aliases: []string{"时间序列", "time series"}},
	{Keyword: "论文写作", Category: "writing", Aliases: []string{"论文写作", "论文撰写", "学术写作"}},
	{Keyword: "论文修改", Category: "writing", Aliases: []string{"论文修改", "修改论文", "论文润色"}},
	{Keyword: "论文投稿", Category: "writing", Aliases: []string{"论文投稿", "投稿论文", "投稿期刊"}},
	{Keyword: "审稿意见", Category: "writing", Aliases: []string{"审稿意见", "审稿人意见", "reviewer comments"}},
	{Keyword: "论文返修", Category: "writing", Aliases: []string{"论文返修", "返修论文", "rebuttal"}},
	{Keyword: "代码报错", Category: "code", Aliases: []string{"代码报错", "程序报错", "运行报错"}},
	{Keyword: "调试", Category: "code", Aliases: []string{"调试代码", "代码调试", "debug"}},
	{Keyword: "Bug修复", Category: "code", Aliases: []string{"bug", "修复错误", "修复问题"}},
	{Keyword: "代码解释", Category: "code", Aliases: []string{"代码解释", "解释代码", "代码含义"}},
	{Keyword: "功能开发", Category: "code", Aliases: []string{"功能开发", "功能扩展", "新增功能"}},
	{Keyword: "代码重构", Category: "code", Aliases: []string{"代码重构", "重构代码", "refactor"}},
	{Keyword: "性能优化", Category: "code", Aliases: []string{"性能优化", "优化性能", "performance optimization"}},
	{Keyword: "单元测试", Category: "code", Aliases: []string{"单元测试", "unit test"}},
	{Keyword: "集成测试", Category: "code", Aliases: []string{"集成测试", "integration test"}},
	{Keyword: "部署", Category: "engineering", Aliases: []string{"部署服务", "部署项目", "deploy", "deployment"}},
	{Keyword: "Docker", Category: "engineering", Aliases: []string{"docker", "dockerfile"}},
	{Keyword: "Nginx", Category: "engineering", Aliases: []string{"nginx"}},
	{Keyword: "PostgreSQL", Category: "engineering", Aliases: []string{"postgresql", "postgres"}},
	{Keyword: "Redis", Category: "engineering", Aliases: []string{"redis"}},
	{Keyword: "数据库", Category: "engineering", Aliases: []string{"数据库", "database"}},
	{Keyword: "API接口", Category: "engineering", Aliases: []string{"api", "接口开发", "接口调用"}},
	{Keyword: "前端", Category: "code", Aliases: []string{"前端", "frontend"}},
	{Keyword: "后端", Category: "code", Aliases: []string{"后端", "backend"}},
	{Keyword: "Python", Category: "code", Aliases: []string{"python"}},
	{Keyword: "Go", Category: "code", Aliases: []string{"golang"}},
	{Keyword: "Java", Category: "code", Aliases: []string{"java"}},
	{Keyword: "JavaScript", Category: "code", Aliases: []string{"javascript"}},
	{Keyword: "TypeScript", Category: "code", Aliases: []string{"typescript"}},
	{Keyword: "Vue", Category: "code", Aliases: []string{"vue"}},
	{Keyword: "React", Category: "code", Aliases: []string{"react"}},
	{Keyword: "Linux", Category: "engineering", Aliases: []string{"linux", "ubuntu"}},
	{Keyword: "Windows", Category: "engineering", Aliases: []string{"windows", "powershell"}},
	{Keyword: "Git", Category: "engineering", Aliases: []string{"git", "github"}},
	{Keyword: "SSH", Category: "engineering", Aliases: []string{"ssh"}},
	{Keyword: "服务器", Category: "engineering", Aliases: []string{"服务器", "server"}},
	{Keyword: "Excel", Category: "data", Aliases: []string{"excel", "xlsx"}},
	{Keyword: "PDF", Category: "literature", Aliases: []string{"pdf"}},
}

func extractArchiveQuestion(raw json.RawMessage) string {
	var root map[string]any
	if json.Unmarshal(raw, &root) != nil {
		return ""
	}
	for _, key := range []string{"input", "messages", "contents"} {
		if value, ok := root[key]; ok {
			return strings.TrimSpace(visibleArchiveText(value))
		}
	}
	return ""
}

func visibleArchiveText(value any) string {
	switch item := value.(type) {
	case string:
		if item == omittedImage || item == omittedAudio || item == omittedFile {
			return ""
		}
		return item
	case []any:
		parts := make([]string, 0, len(item))
		for _, child := range item {
			if text := strings.TrimSpace(visibleArchiveText(child)); text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	case map[string]any:
		for _, key := range []string{"text", "input_text", "value"} {
			if text, ok := item[key].(string); ok && strings.TrimSpace(text) != "" {
				return text
			}
		}
		for _, key := range []string{"content", "parts"} {
			if child, ok := item[key]; ok {
				if text := strings.TrimSpace(visibleArchiveText(child)); text != "" {
					return text
				}
			}
		}
	}
	return ""
}

func archiveKeywordExclusion(question string) string {
	trimmed := strings.TrimSpace(question)
	if trimmed == "" {
		return "no_question"
	}
	if len([]rune(trimmed)) < 4 {
		return "too_short"
	}
	lower := strings.ToLower(trimmed)
	markers := []string{
		"you are a helpful assistant. you will be presented with a user prompt",
		"another language model started to solve this problem",
		"<environment_context>", "<skills_instructions>", "<app-context>",
		"developer instructions", "system instructions", "you are codex",
	}
	for _, marker := range markers {
		if strings.Contains(lower, marker) {
			return "internal_request"
		}
	}
	return ""
}

func extractArchiveKeywords(question string) []extractedArchiveKeyword {
	lower := strings.ToLower(question)
	tokens := archiveASCIITokens(lower)
	result := make([]extractedArchiveKeyword, 0, 18)
	for _, rule := range archiveKeywordRules {
		count := 0
		for _, alias := range rule.Aliases {
			aliasLower := strings.ToLower(alias)
			if isSingleASCIIToken(aliasLower) {
				count += tokens[aliasLower]
			} else {
				count += strings.Count(lower, aliasLower)
			}
		}
		if count > 0 {
			if count > 1000 {
				count = 1000
			}
			result = append(result, extractedArchiveKeyword{
				Keyword:  rule.Keyword,
				Category: rule.Category,
				Count:    count,
			})
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Count == result[j].Count {
			return result[i].Keyword < result[j].Keyword
		}
		return result[i].Count > result[j].Count
	})
	if len(result) > 18 {
		result = result[:18]
	}
	return result
}

func archiveASCIITokens(value string) map[string]int {
	result := make(map[string]int)
	current := make([]rune, 0, 24)
	flush := func() {
		if len(current) >= 2 {
			result[string(current)]++
		}
		current = current[:0]
	}
	for _, char := range value {
		if char <= unicode.MaxASCII && (unicode.IsLetter(char) || unicode.IsDigit(char) || char == '+' || char == '#' || char == '.') {
			current = append(current, char)
		} else {
			flush()
		}
	}
	flush()
	return result
}

func isSingleASCIIToken(value string) bool {
	if value == "" || strings.ContainsAny(value, " \t\r\n") {
		return false
	}
	for _, char := range value {
		if char > unicode.MaxASCII || !(unicode.IsLetter(char) || unicode.IsDigit(char) || char == '+' || char == '#' || char == '.') {
			return false
		}
	}
	return true
}

type archiveKeywordAggregate struct {
	Keyword       string
	Category      string
	Occurrences   int64
	Conversations int64
	Users         map[int64]struct{}
}

type archiveKeywordSnapshotBucket struct {
	Days      int
	Cutoff    time.Time
	Processed int64
	Eligible  int64
	Users     map[int64]struct{}
	Keywords  map[string]*archiveKeywordAggregate
}

func newArchiveKeywordSnapshotBucket(days int, cutoff time.Time) *archiveKeywordSnapshotBucket {
	return &archiveKeywordSnapshotBucket{
		Days:     days,
		Cutoff:   cutoff,
		Users:    make(map[int64]struct{}),
		Keywords: make(map[string]*archiveKeywordAggregate),
	}
}

func (s *postgresArchiveStore) RefreshArchiveKeywordSnapshots(ctx context.Context, lookback time.Duration) (int, error) {
	now := time.Now().UTC()
	if lookback <= 0 || lookback > keywordSnapshotLookback {
		lookback = keywordSnapshotLookback
	}
	cutoff := now.Add(-lookback)
	buckets := []*archiveKeywordSnapshotBucket{
		newArchiveKeywordSnapshotBucket(7, now.Add(-7*24*time.Hour)),
		newArchiveKeywordSnapshotBucket(30, now.Add(-30*24*time.Hour)),
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT user_id, completed_at, request_payload
FROM conversation_archives
WHERE completed_at >= $1
ORDER BY completed_at DESC`, cutoff)
	if err != nil {
		return 0, err
	}
	processedRows := 0
	for rows.Next() {
		var userID int64
		var completedAt time.Time
		var payload []byte
		if err := rows.Scan(&userID, &completedAt, &payload); err != nil {
			_ = rows.Close()
			return 0, err
		}
		processedRows++
		question := extractArchiveQuestion(payload)
		exclusion := archiveKeywordExclusion(question)
		for _, bucket := range buckets {
			if completedAt.Before(bucket.Cutoff) {
				continue
			}
			bucket.Processed++
			bucket.Users[userID] = struct{}{}
			if exclusion != "" {
				continue
			}
			bucket.Eligible++
			for _, keyword := range extractArchiveKeywords(question) {
				aggregate := bucket.Keywords[keyword.Keyword]
				if aggregate == nil {
					aggregate = &archiveKeywordAggregate{Keyword: keyword.Keyword, Category: keyword.Category, Users: make(map[int64]struct{})}
					bucket.Keywords[keyword.Keyword] = aggregate
				}
				aggregate.Occurrences += int64(keyword.Count)
				aggregate.Conversations++
				aggregate.Users[userID] = struct{}{}
			}
		}
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return 0, err
	}
	_ = rows.Close()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	for _, bucket := range buckets {
		items := make([]archiveKeywordCount, 0, len(bucket.Keywords))
		for _, aggregate := range bucket.Keywords {
			users := int64(len(aggregate.Users))
			if aggregate.Conversations < 3 || users < 2 {
				continue
			}
			item := archiveKeywordCount{
				Keyword: aggregate.Keyword, Category: aggregate.Category,
				Occurrences: aggregate.Occurrences, Conversations: aggregate.Conversations, Users: users,
			}
			item.Weight = math.Round((float64(item.Users)*4+float64(item.Conversations)*2+math.Log1p(float64(item.Occurrences)))*100) / 100
			items = append(items, item)
		}
		sort.Slice(items, func(i, j int) bool {
			if items[i].Users != items[j].Users {
				return items[i].Users > items[j].Users
			}
			if items[i].Conversations != items[j].Conversations {
				return items[i].Conversations > items[j].Conversations
			}
			if items[i].Occurrences != items[j].Occurrences {
				return items[i].Occurrences > items[j].Occurrences
			}
			return items[i].Keyword < items[j].Keyword
		})
		if len(items) > 80 {
			items = items[:80]
		}
		payload, err := json.Marshal(items)
		if err != nil {
			return 0, err
		}
		if _, err := tx.ExecContext(ctx, `
INSERT INTO conversation_archive_keyword_snapshots(days,generated_at,processed_documents,eligible_documents,covered_users,items)
VALUES($1,$2,$3,$4,$5,$6::jsonb)
ON CONFLICT(days) DO UPDATE SET generated_at=EXCLUDED.generated_at,processed_documents=EXCLUDED.processed_documents,
eligible_documents=EXCLUDED.eligible_documents,covered_users=EXCLUDED.covered_users,items=EXCLUDED.items`,
			bucket.Days, now, bucket.Processed, bucket.Eligible, len(bucket.Users), string(payload)); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return processedRows, nil
}

func (s *postgresArchiveStore) ListArchiveKeywordStats(ctx context.Context, days, limit int) (archiveKeywordStats, error) {
	result := archiveKeywordStats{Days: days, Items: []archiveKeywordCount{}}
	var itemsJSON []byte
	if err := s.db.QueryRowContext(ctx, `
SELECT generated_at,processed_documents,eligible_documents,covered_users,items
FROM conversation_archive_keyword_snapshots WHERE days=$1`, days).Scan(
		&result.GeneratedAt, &result.ProcessedDocuments, &result.EligibleDocuments, &result.CoveredUsers, &itemsJSON); err != nil {
		if err == sql.ErrNoRows {
			result.GeneratedAt = time.Time{}
			return result, nil
		}
		return result, err
	}
	if err := json.Unmarshal(itemsJSON, &result.Items); err != nil {
		return result, err
	}
	if limit > 0 && len(result.Items) > limit {
		result.Items = result.Items[:limit]
	}
	return result, nil
}

func nextArchiveKeywordSnapshotDelay(now, generatedAt time.Time) time.Duration {
	shanghai := time.FixedZone("Asia/Shanghai", 8*60*60)
	localNow := now.In(shanghai)
	todayRun := time.Date(localNow.Year(), localNow.Month(), localNow.Day(), keywordSnapshotHour, 0, 0, 0, shanghai)
	lastScheduledRun := todayRun
	if localNow.Before(todayRun) {
		lastScheduledRun = todayRun.Add(-24 * time.Hour)
	}
	if generatedAt.IsZero() || generatedAt.Before(lastScheduledRun) {
		return keywordSnapshotFirstDelay
	}
	nextRun := todayRun
	if !nextRun.After(localNow) {
		nextRun = nextRun.Add(24 * time.Hour)
	}
	return nextRun.Sub(localNow)
}

func (s *proxyServer) startKeywordWorker(ctx context.Context) {
	store, ok := s.store.(archiveKeywordStore)
	if !ok {
		log.Printf(`{"event":"archive_keyword_worker_unavailable"}`)
		return
	}
	go func() {
		stats, err := store.ListArchiveKeywordStats(ctx, 30, 1)
		delay := keywordSnapshotFirstDelay
		if err == nil {
			delay = nextArchiveKeywordSnapshotDelay(time.Now(), stats.GeneratedAt)
		}
		timer := time.NewTimer(delay)
		defer timer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-timer.C:
			}
			opCtx, cancel := context.WithTimeout(ctx, keywordSnapshotTimeout)
			processed, err := store.RefreshArchiveKeywordSnapshots(opCtx, keywordSnapshotLookback)
			cancel()
			if err != nil && ctx.Err() == nil {
				log.Printf(`{"event":"archive_keyword_snapshot_failed","error":%q}`, err.Error())
				timer.Reset(keywordSnapshotRetryDelay)
			} else if ctx.Err() == nil {
				log.Printf(`{"event":"archive_keyword_snapshot_refreshed","processed_documents":%d,"next_schedule":"03:00 Asia/Shanghai"}`, processed)
				timer.Reset(nextArchiveKeywordSnapshotDelay(time.Now(), time.Now()))
			}
		}
	}()
}
