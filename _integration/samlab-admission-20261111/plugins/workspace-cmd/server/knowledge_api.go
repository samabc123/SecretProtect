package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const maxKnowledgeUploadBytes = 2 << 20

var allowedKnowledgeTypes = map[string]bool{"prompt_library": true, "weekly_report": true}

type promptRAGSettingsValue struct {
	GeneratePromptLibrary bool `json:"generate_prompt_library"`
	UsePromptLibrary      bool `json:"use_prompt_library"`
	IndexWeeklyReports    bool `json:"index_weekly_reports"`
	UseWeeklyReports      bool `json:"use_weekly_reports"`
}

func (a *app) ensurePromptRAGSettings(ctx context.Context, userID int64) (promptRAGSettingsValue, error) {
	_, err := a.db.Exec(ctx, `INSERT INTO prompt_memory_settings(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING`, userID)
	if err != nil {
		return promptRAGSettingsValue{}, err
	}
	var value promptRAGSettingsValue
	err = a.db.QueryRow(ctx, `SELECT generate_prompt_library,use_prompt_library,index_weekly_reports,use_weekly_reports
		FROM prompt_memory_settings WHERE user_id=$1`, userID).Scan(&value.GeneratePromptLibrary, &value.UsePromptLibrary,
		&value.IndexWeeklyReports, &value.UseWeeklyReports)
	return value, err
}

func (a *app) promptRAGSettings(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method == http.MethodGet {
		value, err := a.ensurePromptRAGSettings(r.Context(), id.ID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取知识库设置失败")
			return
		}
		writeJSON(w, http.StatusOK, value)
		return
	}
	if r.Method != http.MethodPut {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		GeneratePromptLibrary *bool `json:"generate_prompt_library"`
		UsePromptLibrary      *bool `json:"use_prompt_library"`
		IndexWeeklyReports    *bool `json:"index_weekly_reports"`
		UseWeeklyReports      *bool `json:"use_weekly_reports"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil || body.GeneratePromptLibrary == nil && body.UsePromptLibrary == nil && body.IndexWeeklyReports == nil && body.UseWeeklyReports == nil {
		writeAPIError(w, http.StatusBadRequest, "知识库设置无效")
		return
	}
	current, err := a.ensurePromptRAGSettings(r.Context(), id.ID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取知识库设置失败")
		return
	}
	if body.GeneratePromptLibrary != nil {
		current.GeneratePromptLibrary = *body.GeneratePromptLibrary
	}
	if body.UsePromptLibrary != nil {
		current.UsePromptLibrary = *body.UsePromptLibrary
	}
	if body.IndexWeeklyReports != nil {
		current.IndexWeeklyReports = *body.IndexWeeklyReports
	}
	if body.UseWeeklyReports != nil {
		current.UseWeeklyReports = *body.UseWeeklyReports
	}
	_, err = a.db.Exec(r.Context(), `UPDATE prompt_memory_settings SET generate_prompt_library=$2,use_prompt_library=$3,
		index_weekly_reports=$4,use_weekly_reports=$5,ingest_not_before=GREATEST(ingest_not_before,now()-INTERVAL '30 days'),updated_at=now()
		WHERE user_id=$1`, id.ID, current.GeneratePromptLibrary, current.UsePromptLibrary, current.IndexWeeklyReports, current.UseWeeklyReports)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "更新知识库设置失败")
		return
	}
	if a.knowledgeWake != nil && (current.GeneratePromptLibrary || current.IndexWeeklyReports) {
		select {
		case a.knowledgeWake <- struct{}{}:
		default:
		}
	}
	writeJSON(w, http.StatusOK, current)
}

func (a *app) knowledgeDocuments(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	knowledgeType := strings.TrimSpace(r.URL.Query().Get("type"))
	if knowledgeType != "" && !allowedKnowledgeTypes[knowledgeType] {
		writeAPIError(w, http.StatusBadRequest, "知识类型无效")
		return
	}
	args := []any{id.ID}
	filter := "d.owner_user_id=$1 AND d.visibility='private'"
	if knowledgeType != "" {
		args = append(args, knowledgeType)
		filter += fmt.Sprintf(" AND d.knowledge_type=$%d", len(args))
	}
	rows, err := a.db.Query(r.Context(), `SELECT d.id,d.knowledge_type,d.title,d.source_kind,d.enabled,d.tags,d.created_at,d.updated_at,
		COALESCE(v.id,0),COALESCE(v.version,0),COALESCE(v.status,'queued'),COALESCE(v.chunk_count,0),
		COALESCE(left(v.normalized_content,600),''),COALESCE(shared.id,0),COALESCE(shared.review_status,'')
		FROM knowledge_documents d LEFT JOIN knowledge_document_versions v ON v.id=d.active_version_id
		LEFT JOIN LATERAL (SELECT id,review_status FROM knowledge_documents s WHERE s.source_document_id=d.id AND s.visibility='shared'
			AND s.review_status IN ('pending','approved','rejected') ORDER BY s.id DESC LIMIT 1) shared ON true
		WHERE `+filter+` ORDER BY d.updated_at DESC,d.id DESC LIMIT 500`, args...)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取私有知识失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var docID, versionID, sharedID int64
		var version, chunks int
		var kind, title, sourceKind, versionStatus, preview, shareStatus string
		var enabled bool
		var tags []string
		var created, updated time.Time
		if rows.Scan(&docID, &kind, &title, &sourceKind, &enabled, &tags, &created, &updated, &versionID, &version, &versionStatus,
			&chunks, &preview, &sharedID, &shareStatus) != nil {
			continue
		}
		items = append(items, map[string]any{"id": docID, "knowledge_type": kind, "title": title, "source_kind": sourceKind,
			"enabled": enabled, "tags": tags, "created_at": created, "updated_at": updated, "active_version_id": versionID,
			"version": version, "status": versionStatus, "chunk_count": chunks, "preview": preview,
			"shared_document_id": sharedID, "share_status": shareStatus})
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (a *app) knowledgeDocumentByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/knowledge-documents/"), "/"), "/")
	docID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || docID <= 0 || len(parts) != 2 || r.Method != http.MethodPost {
		writeAPIError(w, http.StatusBadRequest, "知识文档操作无效")
		return
	}
	switch parts[1] {
	case "submit":
		a.submitKnowledgeForReview(w, r, id, docID)
	case "withdraw":
		a.withdrawKnowledgeSubmission(w, r, id, docID)
	default:
		writeAPIError(w, http.StatusNotFound, "知识文档操作不存在")
	}
}

func (a *app) submitKnowledgeForReview(w http.ResponseWriter, r *http.Request, id identity, sourceDocID int64) {
	tx, err := a.db.Begin(r.Context())
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "创建共享审核失败")
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()
	var kind, title string
	var sourceVersionID int64
	var version int
	var contentHash, content string
	var tags []string
	err = tx.QueryRow(r.Context(), `SELECT d.knowledge_type,d.title,d.active_version_id,v.version,v.content_sha256,v.normalized_content,d.tags
		FROM knowledge_documents d JOIN knowledge_document_versions v ON v.id=d.active_version_id
		WHERE d.id=$1 AND d.owner_user_id=$2 AND d.visibility='private' AND d.enabled AND v.status='ready' FOR SHARE`, sourceDocID, id.ID).
		Scan(&kind, &title, &sourceVersionID, &version, &contentHash, &content, &tags)
	if errors.Is(err, pgx.ErrNoRows) {
		writeAPIError(w, http.StatusNotFound, "可提交的私有知识不存在")
		return
	}
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取私有知识失败")
		return
	}
	var existing int64
	_ = tx.QueryRow(r.Context(), `SELECT id FROM knowledge_documents WHERE source_document_id=$1 AND visibility='shared'
		AND review_status IN ('pending','approved') ORDER BY id DESC LIMIT 1`, sourceDocID).Scan(&existing)
	if existing > 0 {
		writeAPIError(w, http.StatusConflict, "该知识已经提交或共享")
		return
	}
	var sharedDocID, sharedVersionID int64
	err = tx.QueryRow(r.Context(), `INSERT INTO knowledge_documents(knowledge_type,owner_user_id,visibility,title,source_kind,source_id,
		source_document_id,submitted_by,authorization_confirmed,authorized_at,review_status,enabled,tags)
		VALUES($1,$2,'shared',$3,'share_submission',$4,$5,$2,true,now(),'pending',false,$6) RETURNING id`,
		kind, id.ID, title, sourceDocID, sourceDocID, tags).Scan(&sharedDocID)
	if err == nil {
		err = tx.QueryRow(r.Context(), `INSERT INTO knowledge_document_versions(document_id,version,content_sha256,normalized_content,status,chunk_count,source_metadata,created_by,processed_at)
			SELECT $1,1,content_sha256,normalized_content,'ready',chunk_count,
				jsonb_build_object('source_document_id',$2::bigint,'source_version',$3::integer),$4,now()
			FROM knowledge_document_versions WHERE id=$5 RETURNING id`, sharedDocID, sourceDocID, version, id.ID, sourceVersionID).Scan(&sharedVersionID)
	}
	if err == nil {
		_, err = tx.Exec(r.Context(), `INSERT INTO knowledge_chunks(document_version_id,chunk_index,heading,content,search_text,content_sha256,token_count,embedding,embedding_model)
			SELECT $1,chunk_index,heading,content,search_text,content_sha256,token_count,embedding,embedding_model FROM knowledge_chunks WHERE document_version_id=$2`, sharedVersionID, sourceVersionID)
	}
	if err == nil {
		_, err = tx.Exec(r.Context(), `UPDATE knowledge_documents SET active_version_id=$2,updated_at=now() WHERE id=$1`, sharedDocID, sharedVersionID)
	}
	if err != nil {
		log.Printf("knowledge share submission failed user_id=%d source_document_id=%d: %v", id.ID, sourceDocID, err)
		writeAPIError(w, http.StatusInternalServerError, "创建共享审核失败")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		log.Printf("knowledge share submission commit failed user_id=%d source_document_id=%d: %v", id.ID, sourceDocID, err)
		writeAPIError(w, http.StatusInternalServerError, "创建共享审核失败")
		return
	}
	_ = a.knowledgeAudit(r.Context(), id.ID, sharedDocID, "submit_for_review", map[string]any{"source_document_id": sourceDocID, "content_sha256": contentHash, "content_chars": len([]rune(content))})
	writeJSON(w, http.StatusCreated, map[string]any{"id": sharedDocID, "review_status": "pending"})
}

func (a *app) withdrawKnowledgeSubmission(w http.ResponseWriter, r *http.Request, id identity, sourceDocID int64) {
	result, err := a.db.Exec(r.Context(), `UPDATE knowledge_documents SET review_status='withdrawn',enabled=false,review_reason='',updated_at=now()
		WHERE source_document_id=$1 AND submitted_by=$2 AND visibility='shared' AND review_status IN ('pending','approved')`, sourceDocID, id.ID)
	if err != nil || result.RowsAffected() == 0 {
		writeAPIError(w, http.StatusNotFound, "可撤回的共享提交不存在")
		return
	}
	_ = a.knowledgeAudit(r.Context(), id.ID, sourceDocID, "withdraw_submission", nil)
	writeJSON(w, http.StatusOK, map[string]bool{"withdrawn": true})
}

func (a *app) adminKnowledgeDocuments(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		a.listAdminKnowledgeDocuments(w, r)
	case http.MethodPost:
		a.uploadKnowledgeDocument(w, r, id)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (a *app) listAdminKnowledgeDocuments(w http.ResponseWriter, r *http.Request) {
	rows, err := a.db.Query(r.Context(), `SELECT d.id,d.knowledge_type,d.title,d.source_kind,d.review_status,d.review_reason,d.enabled,d.tags,
		d.authorization_confirmed,COALESCE(d.submitted_by,0),d.created_at,d.updated_at,COALESCE(v.id,0),COALESCE(v.version,0),
		COALESCE(v.original_filename,''),COALESCE(v.status,'queued'),COALESCE(v.error_message,''),COALESCE(v.chunk_count,0),
		COALESCE(left(v.normalized_content,1000),'')
		FROM knowledge_documents d LEFT JOIN knowledge_document_versions v ON v.id=d.active_version_id
		WHERE d.visibility='shared' ORDER BY CASE d.review_status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,d.updated_at DESC,d.id DESC LIMIT 1000`)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取共享知识库失败")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var docID, submittedBy, versionID int64
		var version, chunkCount int
		var kind, title, sourceKind, review, reason, filename, status, lastError, preview string
		var enabled, authorized bool
		var tags []string
		var created, updated time.Time
		if rows.Scan(&docID, &kind, &title, &sourceKind, &review, &reason, &enabled, &tags, &authorized, &submittedBy,
			&created, &updated, &versionID, &version, &filename, &status, &lastError, &chunkCount, &preview) != nil {
			continue
		}
		items = append(items, map[string]any{"id": docID, "knowledge_type": kind, "title": title, "source_kind": sourceKind,
			"review_status": review, "review_reason": reason, "enabled": enabled, "tags": tags, "authorization_confirmed": authorized,
			"submitted_by": submittedBy, "created_at": created, "updated_at": updated, "active_version_id": versionID,
			"version": version, "original_filename": filename, "status": status, "error_message": lastError, "chunk_count": chunkCount, "preview": preview})
	}
	a.knowledgeMu.RLock()
	lastSuccess := any(nil)
	if !a.knowledgeLastSuccess.IsZero() {
		lastSuccess = a.knowledgeLastSuccess
	}
	health := map[string]any{
		"backlog": a.knowledgeBacklog, "processed": a.knowledgeProcessed, "failures": a.knowledgeFailures,
		"last_success_at": lastSuccess, "last_error": redactEvidenceText(a.knowledgeLastError),
	}
	a.knowledgeMu.RUnlock()
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "health": health})
}

func (a *app) uploadKnowledgeDocument(w http.ResponseWriter, r *http.Request, id identity) {
	r.Body = http.MaxBytesReader(w, r.Body, maxKnowledgeUploadBytes+(1<<20))
	if err := r.ParseMultipartForm(maxKnowledgeUploadBytes + (1 << 20)); err != nil {
		writeAPIError(w, http.StatusRequestEntityTooLarge, "知识文档不能超过 2 MB")
		return
	}
	title := truncateRunes(r.FormValue("title"), 240)
	kind := strings.TrimSpace(r.FormValue("knowledge_type"))
	if title == "" || !allowedKnowledgeTypes[kind] || !parseFormBool(r.FormValue("authorization_confirmed")) {
		writeAPIError(w, http.StatusBadRequest, "标题、知识类型和授权确认均为必填项")
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, "请选择 Markdown 文件")
		return
	}
	defer file.Close()
	name := safeFilename(header.Filename)
	if !strings.EqualFold(filepath.Ext(name), ".md") {
		writeAPIError(w, http.StatusUnsupportedMediaType, "只允许上传 .md 文件")
		return
	}
	data, err := io.ReadAll(io.LimitReader(file, maxKnowledgeUploadBytes+1))
	if err != nil || len(data) == 0 || len(data) > maxKnowledgeUploadBytes || !utf8.Valid(data) {
		writeAPIError(w, http.StatusBadRequest, "Markdown 必须是 2 MB 内的 UTF-8 文本")
		return
	}
	content := strings.ReplaceAll(string(data), "\x00", "")
	if knowledgeContainsInstructionInjection(content) {
		writeAPIError(w, http.StatusBadRequest, "文档包含疑似覆盖系统规则的指令，请先清理")
		return
	}
	redacted, entities, err := a.redactMemoryTexts(r.Context(), []string{title, content})
	if err != nil || len(redacted) != 2 {
		writeAPIError(w, http.StatusServiceUnavailable, "隐私过滤服务不可用")
		return
	}
	title, content = truncateRunes(redacted[0], 240), strings.TrimSpace(redacted[1])
	if title == "" || content == "" {
		writeAPIError(w, http.StatusBadRequest, "脱敏后文档为空")
		return
	}
	tags := normalizeKnowledgeTags(strings.FieldsFunc(r.FormValue("tags"), func(value rune) bool { return value == ',' || value == '，' || value == '\n' }))
	metadata := map[string]any{"source": truncateRunes(r.FormValue("source"), 300), "week_start": truncateRunes(r.FormValue("week_start"), 20), "redacted_entities": entities}
	docID, _ := strconv.ParseInt(r.FormValue("document_id"), 10, 64)
	storageKey := filepath.Join("knowledge", randomUUID()+".md")
	storagePath := filepath.Join(a.cfg.StorageDir, storageKey)
	if err := os.MkdirAll(filepath.Dir(storagePath), 0700); err != nil || os.WriteFile(storagePath, []byte(content), 0600) != nil {
		writeAPIError(w, http.StatusInternalServerError, "保存知识文档失败")
		return
	}
	createdDocID, versionID, err := a.createSharedKnowledgeUpload(r.Context(), id.ID, docID, kind, title, name, storageKey, content, tags, metadata)
	if err != nil {
		_ = os.Remove(storagePath)
		writeAPIError(w, http.StatusConflict, err.Error())
		return
	}
	_ = a.knowledgeAudit(r.Context(), id.ID, createdDocID, "upload_version", map[string]any{"version_id": versionID, "filename": name, "redacted_entities": entities})
	select {
	case a.knowledgeWake <- struct{}{}:
	default:
	}
	writeJSON(w, http.StatusCreated, map[string]any{"id": createdDocID, "version_id": versionID, "status": "queued", "redacted_entities": entities})
}

func (a *app) createSharedKnowledgeUpload(ctx context.Context, actorID, documentID int64, kind, title, filename, storageKey, content string, tags []string, metadata map[string]any) (int64, int64, error) {
	tx, err := a.db.Begin(ctx)
	if err != nil {
		return 0, 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	version := 1
	if documentID > 0 {
		var existingKind, sourceKind string
		if err := tx.QueryRow(ctx, `SELECT knowledge_type,source_kind FROM knowledge_documents WHERE id=$1 AND visibility='shared' FOR UPDATE`, documentID).Scan(&existingKind, &sourceKind); err != nil {
			return 0, 0, errors.New("共享知识文档不存在")
		}
		if existingKind != kind || sourceKind != "md_upload" {
			return 0, 0, errors.New("新版本的知识类型必须与原文档一致")
		}
		_ = tx.QueryRow(ctx, `SELECT COALESCE(MAX(version),0)+1 FROM knowledge_document_versions WHERE document_id=$1`, documentID).Scan(&version)
		_, err = tx.Exec(ctx, `UPDATE knowledge_documents SET title=$2,tags=$3,review_status='pending',review_reason='',reviewed_by=NULL,
			reviewed_at=NULL,enabled=false,authorization_confirmed=true,authorized_at=now(),updated_at=now() WHERE id=$1`, documentID, title, tags)
	} else {
		err = tx.QueryRow(ctx, `INSERT INTO knowledge_documents(knowledge_type,visibility,title,source_kind,submitted_by,authorization_confirmed,
			authorized_at,review_status,enabled,tags) VALUES($1,'shared',$2,'md_upload',$3,true,now(),'pending',false,$4) RETURNING id`,
			kind, title, actorID, tags).Scan(&documentID)
	}
	if err != nil {
		return 0, 0, err
	}
	var versionID int64
	err = tx.QueryRow(ctx, `INSERT INTO knowledge_document_versions(document_id,version,original_filename,storage_key,content_sha256,normalized_content,
		status,source_metadata,created_by) VALUES($1,$2,$3,$4,$5,$6,'queued',$7,$8) RETURNING id`, documentID, version, filename,
		storageKey, hashText(content), content, jsonBytes(metadata), actorID).Scan(&versionID)
	if err != nil {
		return 0, 0, err
	}
	if _, err = tx.Exec(ctx, `UPDATE knowledge_documents SET active_version_id=$2,updated_at=now() WHERE id=$1`, documentID, versionID); err != nil {
		return 0, 0, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO knowledge_ingestions(user_id,source_kind,source_id,source_hash,document_version_id)
		VALUES($1,'md_upload',$2,$3,$2) ON CONFLICT(user_id,source_kind,source_id) DO UPDATE SET status='queued',next_attempt_at=now(),last_error='',updated_at=now()`,
		actorID, versionID, hashText(content))
	if err != nil {
		return 0, 0, err
	}
	return documentID, versionID, tx.Commit(ctx)
}

func (a *app) adminKnowledgeDocumentByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/admin/knowledge-documents/"), "/"), "/")
	docID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || docID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "知识文档编号无效")
		return
	}
	if len(parts) == 1 && r.Method == http.MethodPatch {
		a.patchAdminKnowledgeDocument(w, r, id, docID)
		return
	}
	if len(parts) == 1 && r.Method == http.MethodGet {
		a.getAdminKnowledgeDocument(w, r, docID)
		return
	}
	if len(parts) != 2 || r.Method != http.MethodPost {
		writeAPIError(w, http.StatusNotFound, "知识库管理操作不存在")
		return
	}
	var body struct {
		Reason string `json:"reason"`
	}
	if parts[1] == "reject" {
		if err := readJSON(r, &body, 1<<20); err != nil || strings.TrimSpace(body.Reason) == "" {
			writeAPIError(w, http.StatusBadRequest, "拒绝原因不能为空")
			return
		}
	}
	var result pgconn.CommandTag
	switch parts[1] {
	case "approve":
		result, err = a.db.Exec(r.Context(), `UPDATE knowledge_documents d SET review_status='approved',review_reason='',reviewed_by=$2,
			reviewed_at=now(),enabled=true,updated_at=now() FROM knowledge_document_versions v
			WHERE d.id=$1 AND d.visibility='shared' AND d.authorization_confirmed AND d.active_version_id=v.id AND v.status='ready' AND d.review_status='pending'`, docID, id.ID)
	case "reject":
		result, err = a.db.Exec(r.Context(), `UPDATE knowledge_documents SET review_status='rejected',review_reason=$3,reviewed_by=$2,
			reviewed_at=now(),enabled=false,updated_at=now() WHERE id=$1 AND visibility='shared' AND review_status='pending'`, docID, id.ID, truncateRunes(body.Reason, 1000))
	case "enable":
		result, err = a.db.Exec(r.Context(), `UPDATE knowledge_documents d SET enabled=true,updated_at=now() FROM knowledge_document_versions v
			WHERE d.id=$1 AND d.visibility='shared' AND d.review_status='approved' AND d.active_version_id=v.id AND v.status='ready'`, docID)
	case "disable":
		result, err = a.db.Exec(r.Context(), `UPDATE knowledge_documents SET enabled=false,updated_at=now() WHERE id=$1 AND visibility='shared'`, docID)
	case "retry":
		result, err = a.db.Exec(r.Context(), `UPDATE knowledge_ingestions i SET status='queued',next_attempt_at=now(),last_error='',updated_at=now()
			FROM knowledge_documents d,knowledge_document_versions v WHERE d.id=$1 AND d.visibility='shared' AND d.active_version_id=v.id
			AND i.document_version_id=v.id AND i.status='failed'`, docID)
		if err == nil && result.RowsAffected() > 0 {
			_, _ = a.db.Exec(r.Context(), `UPDATE knowledge_document_versions SET status='queued',error_message='' WHERE id=(SELECT active_version_id FROM knowledge_documents WHERE id=$1)`, docID)
			select {
			case a.knowledgeWake <- struct{}{}:
			default:
			}
		}
	default:
		writeAPIError(w, http.StatusNotFound, "知识库管理操作不存在")
		return
	}
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "知识库操作失败")
		return
	}
	if result.RowsAffected() == 0 {
		writeAPIError(w, http.StatusConflict, "知识文档当前状态不允许该操作")
		return
	}
	_ = a.knowledgeAudit(r.Context(), id.ID, docID, parts[1], map[string]any{"reason": truncateRunes(body.Reason, 1000)})
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (a *app) getAdminKnowledgeDocument(w http.ResponseWriter, r *http.Request, docID int64) {
	var kind, title, sourceKind, reviewStatus, reviewReason, content string
	var sourceID, sourceDocumentID, submittedBy, reviewedBy, activeVersionID int64
	var authorized, enabled bool
	var tags []string
	var createdAt, updatedAt time.Time
	err := a.db.QueryRow(r.Context(), `SELECT d.knowledge_type,d.title,d.source_kind,COALESCE(d.source_id,0),
		COALESCE(d.source_document_id,0),COALESCE(d.submitted_by,0),d.authorization_confirmed,d.review_status,d.review_reason,
		COALESCE(d.reviewed_by,0),d.enabled,d.tags,d.created_at,d.updated_at,COALESCE(d.active_version_id,0),
		COALESCE(v.normalized_content,'') FROM knowledge_documents d
		LEFT JOIN knowledge_document_versions v ON v.id=d.active_version_id
		WHERE d.id=$1 AND d.visibility='shared'`, docID).Scan(&kind, &title, &sourceKind, &sourceID, &sourceDocumentID,
		&submittedBy, &authorized, &reviewStatus, &reviewReason, &reviewedBy, &enabled, &tags, &createdAt, &updatedAt,
		&activeVersionID, &content)
	if errors.Is(err, pgx.ErrNoRows) {
		writeAPIError(w, http.StatusNotFound, "共享知识文档不存在")
		return
	}
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取共享知识文档失败")
		return
	}
	rows, err := a.db.Query(r.Context(), `SELECT id,version,original_filename,content_sha256,status,error_message,chunk_count,
		source_metadata,COALESCE(created_by,0),created_at,processed_at FROM knowledge_document_versions
		WHERE document_id=$1 ORDER BY version DESC,id DESC`, docID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取知识文档版本失败")
		return
	}
	defer rows.Close()
	versions := make([]map[string]any, 0)
	for rows.Next() {
		var versionID, createdBy int64
		var version, chunkCount int
		var filename, contentHash, status, errorMessage string
		var metadata []byte
		var versionCreated time.Time
		var processedAt *time.Time
		if err := rows.Scan(&versionID, &version, &filename, &contentHash, &status, &errorMessage, &chunkCount,
			&metadata, &createdBy, &versionCreated, &processedAt); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "读取知识文档版本失败")
			return
		}
		var sourceMetadata map[string]any
		_ = json.Unmarshal(metadata, &sourceMetadata)
		versions = append(versions, map[string]any{"id": versionID, "version": version, "original_filename": filename,
			"content_sha256": contentHash, "status": status, "error_message": errorMessage, "chunk_count": chunkCount,
			"source_metadata": sourceMetadata, "created_by": createdBy, "created_at": versionCreated, "processed_at": processedAt})
	}
	if err := rows.Err(); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取知识文档版本失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"id": docID, "knowledge_type": kind, "title": title, "source_kind": sourceKind, "source_id": sourceID,
		"source_document_id": sourceDocumentID, "submitted_by": submittedBy, "authorization_confirmed": authorized,
		"review_status": reviewStatus, "review_reason": reviewReason, "reviewed_by": reviewedBy, "enabled": enabled,
		"tags": tags, "created_at": createdAt, "updated_at": updatedAt, "active_version_id": activeVersionID,
		"content": content, "versions": versions,
	})
}

func (a *app) patchAdminKnowledgeDocument(w http.ResponseWriter, r *http.Request, id identity, docID int64) {
	var body struct {
		Title *string   `json:"title"`
		Tags  *[]string `json:"tags"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil || body.Title == nil && body.Tags == nil {
		writeAPIError(w, http.StatusBadRequest, "知识文档更新无效")
		return
	}
	var title any
	if body.Title != nil {
		value := truncateRunes(*body.Title, 240)
		if value == "" {
			writeAPIError(w, http.StatusBadRequest, "标题不能为空")
			return
		}
		title = value
	}
	var tags any
	if body.Tags != nil {
		tags = normalizeKnowledgeTags(*body.Tags)
	}
	result, err := a.db.Exec(r.Context(), `UPDATE knowledge_documents SET title=COALESCE($2,title),tags=COALESCE($3,tags),updated_at=now()
		WHERE id=$1 AND visibility='shared'`, docID, title, tags)
	if err != nil || result.RowsAffected() == 0 {
		writeAPIError(w, http.StatusNotFound, "共享知识文档不存在")
		return
	}
	_ = a.knowledgeAudit(r.Context(), id.ID, docID, "edit", nil)
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func normalizeKnowledgeTags(values []string) []string {
	seen := map[string]bool{}
	result := make([]string, 0, 12)
	for _, value := range values {
		value = truncateRunes(value, 64)
		key := strings.ToLower(value)
		if value == "" || seen[key] {
			continue
		}
		seen[key] = true
		result = append(result, value)
		if len(result) == 12 {
			break
		}
	}
	return result
}

func knowledgeContainsInstructionInjection(content string) bool {
	value := strings.ToLower(content)
	for _, marker := range []string{"ignore previous instructions", "ignore all previous", "reveal the system prompt", "<system>", "<developer>", "忽略以上指令", "忽略之前的指令", "输出系统提示词"} {
		if strings.Contains(value, marker) {
			return true
		}
	}
	return false
}

func parseFormBool(value string) bool {
	value = strings.ToLower(strings.TrimSpace(value))
	return value == "true" || value == "1" || value == "yes" || value == "on"
}

func (a *app) knowledgeAudit(ctx context.Context, actorID, documentID int64, action string, metadata map[string]any) error {
	_, err := a.db.Exec(ctx, `INSERT INTO knowledge_audit_logs(actor_id,document_id,action,metadata) VALUES($1,$2,$3,$4)`, actorID, documentID, action, jsonBytes(metadata))
	return err
}

func knowledgeMetadata(value map[string]any) json.RawMessage {
	return json.RawMessage(jsonBytes(value))
}
