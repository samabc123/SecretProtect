package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

var blockedExtensions = map[string]bool{
	".exe": true, ".dll": true, ".bat": true, ".cmd": true, ".ps1": true,
	".js": true, ".mjs": true, ".html": true, ".htm": true, ".svg": true,
	".msi": true, ".scr": true, ".com": true, ".jar": true,
	".docm": true, ".xlsm": true, ".pptm": true, ".vbs": true, ".sh": true,
}

var allowedExtensions = map[string]bool{
	".pdf": true, ".png": true, ".jpg": true, ".jpeg": true, ".webp": true,
	".txt": true, ".md": true, ".csv": true, ".docx": true, ".xlsx": true,
	".pptx": true, ".zip": true,
}

func (a *app) documents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	items, err := a.listDocuments(r, id)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取文件失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (a *app) listDocuments(r *http.Request, id identity) ([]map[string]any, error) {
	where := "d.status='published'"
	args := []any{id.ID}
	if id.Role == "admin" {
		where = "TRUE"
	}
	if q := strings.TrimSpace(r.URL.Query().Get("q")); q != "" {
		pattern := "%" + truncateRunes(q, 120) + "%"
		args = append(args, pattern)
		placeholder := strconv.Itoa(len(args))
		where += " AND (d.title ILIKE $" + placeholder + " OR d.description ILIKE $" + placeholder + " OR d.category ILIKE $" + placeholder + ")"
	}
	if category := strings.TrimSpace(r.URL.Query().Get("category")); category != "" {
		args = append(args, truncateRunes(category, 120))
		where += " AND d.category=$" + strconv.Itoa(len(args))
	}
	query := `SELECT d.id,d.title,d.description,d.category,d.original_filename,d.mime_type,d.size_bytes,d.status,
		d.created_at,d.published_at,
		EXISTS(SELECT 1 FROM document_reads dr WHERE dr.document_id=d.id AND dr.user_id=$1),
		(SELECT read_at FROM document_reads dr WHERE dr.document_id=d.id AND dr.user_id=$1),
		(SELECT COUNT(*) FROM document_reads dr WHERE dr.document_id=d.id),
		(SELECT COUNT(*) FROM users u WHERE u.status='active' AND u.deleted_at IS NULL)
		FROM distributed_documents d WHERE ` + where + `
		ORDER BY COALESCE(d.published_at,d.created_at) DESC LIMIT 200`
	rows, err := a.db.Query(r.Context(), query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		item, scanErr := scanDocument(rows)
		if scanErr != nil {
			return nil, scanErr
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (a *app) documentByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	suffix := strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/documents/")
	parts := strings.Split(strings.Trim(suffix, "/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		writeAPIError(w, http.StatusBadRequest, "文件编号无效")
		return
	}
	docID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || docID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "文件编号无效")
		return
	}
	switch {
	case len(parts) == 1 && r.Method == http.MethodGet:
		a.documentDetail(w, r, id, docID)
	case len(parts) == 2 && parts[1] == "content":
		a.documentContent(w, r, id, docID)
	case len(parts) == 2 && parts[1] == "read" && r.Method == http.MethodPost:
		a.documentRead(w, r, id, docID)
	default:
		writeAPIError(w, http.StatusNotFound, "文件接口不存在")
	}
}

func (a *app) documentDetail(w http.ResponseWriter, r *http.Request, id identity, docID int64) {
	item, err := a.loadDocument(r, id, docID)
	if err != nil {
		writeAPIError(w, http.StatusNotFound, "文件不存在")
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (a *app) loadDocument(r *http.Request, id identity, docID int64) (map[string]any, error) {
	where := "d.id=$2 AND d.status='published'"
	if id.Role == "admin" {
		where = "d.id=$2"
	}
	query := `SELECT d.id,d.title,d.description,d.category,d.original_filename,d.mime_type,d.size_bytes,d.status,
		d.created_at,d.published_at,
		EXISTS(SELECT 1 FROM document_reads dr WHERE dr.document_id=d.id AND dr.user_id=$1),
		(SELECT read_at FROM document_reads dr WHERE dr.document_id=d.id AND dr.user_id=$1),
		(SELECT COUNT(*) FROM document_reads dr WHERE dr.document_id=d.id),
		(SELECT COUNT(*) FROM users u WHERE u.status='active' AND u.deleted_at IS NULL)
		FROM distributed_documents d WHERE ` + where
	row := a.db.QueryRow(r.Context(), query, id.ID, docID)
	return scanDocument(row)
}

func (a *app) documentRead(w http.ResponseWriter, r *http.Request, id identity, docID int64) {
	var exists bool
	if err := a.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM distributed_documents WHERE id=$1 AND (status='published' OR $2='admin'))`, docID, id.Role).Scan(&exists); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "记录已读失败")
		return
	}
	if !exists {
		writeAPIError(w, http.StatusNotFound, "文件不存在")
		return
	}
	result, err := a.db.Exec(r.Context(), `INSERT INTO document_reads(document_id,user_id) VALUES($1,$2) ON CONFLICT(document_id,user_id) DO NOTHING`, docID, id.ID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "记录已读失败")
		return
	}
	if result.RowsAffected() > 0 {
		_ = a.audit(r.Context(), id.ID, "read", docID, nil)
	}
	writeJSON(w, http.StatusOK, map[string]any{"read": true, "idempotent": result.RowsAffected() == 0})
}

func (a *app) documentContent(w http.ResponseWriter, r *http.Request, id identity, docID int64) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var key, name, mimeType, status string
	if err := a.db.QueryRow(r.Context(), `SELECT storage_key,original_filename,mime_type,status FROM distributed_documents WHERE id=$1`, docID).Scan(&key, &name, &mimeType, &status); err != nil {
		http.NotFound(w, r)
		return
	}
	if status != "published" && id.Role != "admin" {
		http.NotFound(w, r)
		return
	}
	path, ok := safeStoragePath(a.cfg.StorageDir, key)
	if !ok {
		http.NotFound(w, r)
		return
	}
	if _, err := os.Stat(path); err != nil {
		http.NotFound(w, r)
		return
	}
	disposition := "inline"
	if r.URL.Query().Get("disposition") == "attachment" {
		disposition = "attachment"
	}
	w.Header().Set("Content-Type", mimeType)
	w.Header().Set("Content-Disposition", contentDisposition(disposition, safeFilename(name)))
	w.Header().Set("Cache-Control", "private, no-store")
	_ = a.audit(r.Context(), id.ID, disposition, docID, map[string]any{"filename": safeFilename(name)})
	http.ServeFile(w, r, path)
}

func (a *app) adminDocuments(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxUploadBytes+2*1024*1024)
	if err := r.ParseMultipartForm(2 * 1024 * 1024); err != nil {
		writeAPIError(w, http.StatusBadRequest, "上传表单无效")
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, "请选择文件")
		return
	}
	defer file.Close()
	name := safeFilename(header.Filename)
	title := truncateRunes(strings.TrimSpace(r.FormValue("title")), 240)
	if title == "" {
		title = truncateRunes(strings.TrimSuffix(name, filepath.Ext(name)), 240)
	}
	if title == "" {
		writeAPIError(w, http.StatusBadRequest, "文件标题不能为空")
		return
	}
	description := truncateRunes(r.FormValue("description"), 4000)
	category := truncateRunes(r.FormValue("category"), 120)
	key := randomID()
	dir := filepath.Join(a.cfg.StorageDir, key[:2])
	if err := os.MkdirAll(dir, 0700); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "创建文件目录失败")
		return
	}
	tmp := filepath.Join(dir, ".partial-"+key)
	dst, err := os.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "创建文件失败")
		return
	}
	hash := sha256.New()
	first := make([]byte, 512)
	n, readErr := io.ReadFull(file, first)
	if readErr != nil && readErr != io.EOF && readErr != io.ErrUnexpectedEOF {
		_ = dst.Close()
		_ = os.Remove(tmp)
		writeAPIError(w, http.StatusBadRequest, "读取文件失败")
		return
	}
	first = first[:n]
	mimeType, valid := detectFileType(name, first)
	if !valid {
		_ = dst.Close()
		_ = os.Remove(tmp)
		writeAPIError(w, http.StatusUnsupportedMediaType, "文件类型不允许或内容格式无效")
		return
	}
	if _, err := io.MultiWriter(dst, hash).Write(first); err != nil {
		_ = dst.Close()
		_ = os.Remove(tmp)
		writeAPIError(w, http.StatusInternalServerError, "写入文件失败")
		return
	}
	remaining := io.LimitReader(file, maxUploadBytes-int64(n)+1)
	rest, copyErr := io.Copy(io.MultiWriter(dst, hash), remaining)
	size := int64(n) + rest
	_ = dst.Close()
	if copyErr != nil || size <= 0 || size > maxUploadBytes {
		_ = os.Remove(tmp)
		writeAPIError(w, http.StatusRequestEntityTooLarge, "文件不能超过 100 MB")
		return
	}
	if strings.EqualFold(filepath.Ext(name), ".zip") || isOfficeExtension(filepath.Ext(name)) {
		if err := validateZipFile(tmp, filepath.Ext(name)); err != nil {
			_ = os.Remove(tmp)
			writeAPIError(w, http.StatusUnsupportedMediaType, "压缩包包含不安全内容")
			return
		}
	}
	final := filepath.Join(dir, key)
	if err := os.Rename(tmp, final); err != nil {
		_ = os.Remove(tmp)
		writeAPIError(w, http.StatusInternalServerError, "保存文件失败")
		return
	}
	status := "draft"
	if strings.EqualFold(r.FormValue("publish"), "true") || r.FormValue("publish") == "1" {
		status = "published"
	}
	var publishedAt *time.Time
	if status == "published" {
		now := time.Now().UTC()
		publishedAt = &now
	}
	var docID int64
	err = a.db.QueryRow(r.Context(), `
		INSERT INTO distributed_documents(title,description,category,original_filename,storage_key,mime_type,size_bytes,sha256,status,uploader_id,published_at)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id`,
		title, description, category, name, key, mimeType, size, hex.EncodeToString(hash.Sum(nil)), status, id.ID, publishedAt).Scan(&docID)
	if err != nil {
		_ = os.Remove(final)
		writeAPIError(w, http.StatusInternalServerError, "保存文件信息失败")
		return
	}
	_ = a.audit(r.Context(), id.ID, "upload", docID, map[string]any{"filename": name, "size": size, "status": status})
	writeJSON(w, http.StatusCreated, map[string]any{"id": docID, "status": status})
}

func (a *app) adminDocumentByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok || !requireAdmin(w, id) {
		return
	}
	suffix := strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/admin/documents/")
	parts := strings.Split(strings.Trim(suffix, "/"), "/")
	docID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || docID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "文件编号无效")
		return
	}
	if len(parts) == 1 {
		if r.Method == http.MethodPatch {
			a.adminDocumentPatch(w, r, id, docID)
			return
		}
		writeAPIError(w, http.StatusNotFound, "管理接口不存在")
		return
	}
	switch parts[1] {
	case "publish":
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		result, err := a.db.Exec(r.Context(), `UPDATE distributed_documents SET status='published',published_at=now(),withdrawn_at=NULL,updated_at=now() WHERE id=$1 AND status IN ('draft','withdrawn')`, docID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "发布失败")
			return
		}
		if result.RowsAffected() == 0 {
			writeAPIError(w, http.StatusConflict, "文件当前状态不能发布")
			return
		}
		_ = a.audit(r.Context(), id.ID, "publish", docID, nil)
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	case "withdraw":
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		result, err := a.db.Exec(r.Context(), `UPDATE distributed_documents SET status='withdrawn',withdrawn_at=now(),updated_at=now() WHERE id=$1 AND status='published'`, docID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "撤回失败")
			return
		}
		if result.RowsAffected() == 0 {
			writeAPIError(w, http.StatusConflict, "文件当前状态不能撤回")
			return
		}
		_ = a.audit(r.Context(), id.ID, "withdraw", docID, nil)
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	case "read-status":
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		a.documentReadStatus(w, r, docID)
	default:
		writeAPIError(w, http.StatusNotFound, "管理接口不存在")
	}
}

func (a *app) adminDocumentPatch(w http.ResponseWriter, r *http.Request, id identity, docID int64) {
	var body struct {
		Title       *string `json:"title"`
		Description *string `json:"description"`
		Category    *string `json:"category"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil {
		writeAPIError(w, http.StatusBadRequest, "请求格式无效")
		return
	}
	sets := make([]string, 0, 3)
	args := []any{}
	if body.Title != nil {
		value := truncateRunes(strings.TrimSpace(*body.Title), 240)
		if value == "" {
			writeAPIError(w, http.StatusBadRequest, "文件标题不能为空")
			return
		}
		args = append(args, value)
		sets = append(sets, "title=$"+strconv.Itoa(len(args)))
	}
	if body.Description != nil {
		args = append(args, truncateRunes(*body.Description, 4000))
		sets = append(sets, "description=$"+strconv.Itoa(len(args)))
	}
	if body.Category != nil {
		args = append(args, truncateRunes(*body.Category, 120))
		sets = append(sets, "category=$"+strconv.Itoa(len(args)))
	}
	if len(sets) == 0 {
		writeAPIError(w, http.StatusBadRequest, "没有可更新字段")
		return
	}
	args = append(args, docID)
	_, err := a.db.Exec(r.Context(), `UPDATE distributed_documents SET `+strings.Join(sets, ",")+`,updated_at=now() WHERE id=$`+strconv.Itoa(len(args)), args...)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "更新文件信息失败")
		return
	}
	_ = a.audit(r.Context(), id.ID, "edit", docID, nil)
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (a *app) documentReadStatus(w http.ResponseWriter, r *http.Request, docID int64) {
	var exists bool
	if err := a.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM distributed_documents WHERE id=$1)`, docID).Scan(&exists); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取文件失败")
		return
	}
	if !exists {
		writeAPIError(w, http.StatusNotFound, "文件不存在")
		return
	}
	var total, read int
	if err := a.db.QueryRow(r.Context(), `SELECT COUNT(*) FROM users WHERE status='active' AND deleted_at IS NULL`).Scan(&total); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取成员失败")
		return
	}
	if err := a.db.QueryRow(r.Context(), `SELECT COUNT(*) FROM document_reads WHERE document_id=$1`, docID).Scan(&read); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取已读状态失败")
		return
	}
	rows, err := a.db.Query(r.Context(), `SELECT u.id,u.username,COALESCE(u.email,''),dr.read_at FROM users u LEFT JOIN document_reads dr ON dr.user_id=u.id AND dr.document_id=$1 WHERE u.status='active' AND u.deleted_at IS NULL ORDER BY (dr.read_at IS NULL),dr.read_at DESC,u.id`, docID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "读取成员状态失败")
		return
	}
	defer rows.Close()
	members := make([]map[string]any, 0)
	for rows.Next() {
		var userID int64
		var username, email string
		var readAt *time.Time
		if rows.Scan(&userID, &username, &email, &readAt) == nil {
			members = append(members, map[string]any{"user_id": userID, "username": username, "email": email, "read_at": readAt, "is_read": readAt != nil})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"read_count": read, "active_member_count": total, "read_rate": rate(read, total), "members": members})
}

func rate(read, total int) float64 {
	if total == 0 {
		return 0
	}
	return float64(read) / float64(total) * 100
}

func (a *app) audit(ctx context.Context, actorID int64, action string, documentID int64, metadata map[string]any) error {
	_, err := a.db.Exec(ctx, `INSERT INTO workspace_audit_logs(actor_id,action,document_id,metadata) VALUES($1,$2,$3,$4)`, actorID, action, documentID, jsonBytes(metadata))
	return err
}

func scanDocument(row rowScanner) (map[string]any, error) {
	var id int64
	var title, description, category, name, mimeType, status string
	var size int64
	var created time.Time
	var published, readAt *time.Time
	var isRead bool
	var readCount, members int
	if err := row.Scan(&id, &title, &description, &category, &name, &mimeType, &size, &status, &created, &published, &isRead, &readAt, &readCount, &members); err != nil {
		return nil, err
	}
	return map[string]any{
		"id": id, "title": title, "description": description, "category": category,
		"original_filename": name, "mime_type": mimeType, "size_bytes": size, "status": status,
		"created_at": created, "published_at": published, "is_read": isRead, "read_at": readAt,
		"read_count": readCount, "active_member_count": members,
	}, nil
}

func safeStoragePath(base, key string) (string, bool) {
	if len(key) != 32 {
		return "", false
	}
	if _, err := hex.DecodeString(key); err != nil {
		return "", false
	}
	path := filepath.Join(base, key[:2], key)
	rel, err := filepath.Rel(base, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
		return "", false
	}
	return path, true
}

func detectFileType(name string, sniff []byte) (string, bool) {
	ext := strings.ToLower(filepath.Ext(name))
	if blockedExtensions[ext] || !allowedExtensions[ext] {
		return "", false
	}
	if len(sniff) == 0 {
		return "", false
	}
	switch ext {
	case ".pdf":
		return "application/pdf", bytes.HasPrefix(sniff, []byte("%PDF-"))
	case ".png":
		return "image/png", bytes.HasPrefix(sniff, []byte{0x89, 'P', 'N', 'G', 0x0d, 0x0a, 0x1a, 0x0a})
	case ".jpg", ".jpeg":
		return "image/jpeg", len(sniff) >= 3 && sniff[0] == 0xff && sniff[1] == 0xd8 && sniff[2] == 0xff
	case ".webp":
		return "image/webp", len(sniff) >= 12 && bytes.Equal(sniff[:4], []byte("RIFF")) && bytes.Equal(sniff[8:12], []byte("WEBP"))
	case ".txt", ".md", ".csv":
		return map[string]string{".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8"}[ext], utf8.Valid(sniff) && !bytes.ContainsRune(sniff, '\x00')
	case ".docx":
		return "application/vnd.openxmlformats-officedocument.wordprocessingml.document", isZipSignature(sniff)
	case ".xlsx":
		return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", isZipSignature(sniff)
	case ".pptx":
		return "application/vnd.openxmlformats-officedocument.presentationml.presentation", isZipSignature(sniff)
	case ".zip":
		return "application/zip", isZipSignature(sniff)
	default:
		return "", false
	}
}

func allowedFile(name, typ string) bool {
	ext := strings.ToLower(filepath.Ext(name))
	return allowedExtensions[ext] && !blockedExtensions[ext] && strings.TrimSpace(typ) != ""
}

func isOfficeExtension(ext string) bool {
	switch strings.ToLower(ext) {
	case ".docx", ".xlsx", ".pptx":
		return true
	default:
		return false
	}
}

func isZipSignature(data []byte) bool {
	return len(data) >= 4 && bytes.Equal(data[:2], []byte{'P', 'K'}) && (data[2] == 3 || data[2] == 5 || data[2] == 7)
}

func validateZipFile(path, ext string) error {
	archive, err := zip.OpenReader(path)
	if err != nil {
		return err
	}
	defer archive.Close()
	if len(archive.File) > 10000 {
		return fmt.Errorf("too many entries")
	}
	var total uint64
	required := map[string]bool{".docx": false, ".xlsx": false, ".pptx": false}
	for _, file := range archive.File {
		name := strings.ReplaceAll(file.Name, "\\", "/")
		clean := filepath.Clean(name)
		if name == "" || strings.HasPrefix(name, "/") || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return fmt.Errorf("path traversal")
		}
		if file.Mode()&os.ModeSymlink != 0 || blockedExtensions[strings.ToLower(filepath.Ext(name))] {
			return fmt.Errorf("blocked entry")
		}
		total += file.UncompressedSize64
		if total > 2*1024*1024*1024 {
			return fmt.Errorf("archive too large")
		}
		if strings.HasSuffix(name, "vbaProject.bin") || strings.Contains(strings.ToLower(name), "macros") {
			return fmt.Errorf("macro entry")
		}
		for officeExt, seen := range required {
			if seen {
				continue
			}
			switch officeExt {
			case ".docx":
				required[officeExt] = strings.HasPrefix(name, "word/")
			case ".xlsx":
				required[officeExt] = strings.HasPrefix(name, "xl/")
			case ".pptx":
				required[officeExt] = strings.HasPrefix(name, "ppt/")
			}
		}
	}
	if isOfficeExtension(ext) && !required[strings.ToLower(ext)] {
		return fmt.Errorf("not a valid office package")
	}
	return nil
}
