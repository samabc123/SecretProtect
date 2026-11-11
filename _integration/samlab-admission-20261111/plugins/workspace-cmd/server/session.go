package main

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

const sessionTTL = 30 * time.Minute

func (a *app) session(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	token := strings.TrimSpace(r.URL.Query().Get("token"))
	if token == "" {
		if cookie, err := r.Cookie(cookieName); err == nil {
			if id, ok := a.sessionIdentity(r.Context(), cookie.Value); ok {
				writeJSON(w, http.StatusOK, id)
				return
			}
		}
	}
	if token == "" {
		writeAPIError(w, http.StatusUnauthorized, "登录已失效")
		return
	}
	id, err := a.resolveToken(r.Context(), token)
	if err != nil {
		writeAPIError(w, http.StatusUnauthorized, "Sub2API 登录已失效")
		return
	}
	sid := randomID()
	protected, err := a.protectUpstreamToken(token)
	if err != nil {
		writeAPIError(w, http.StatusServiceUnavailable, "会话服务暂不可用")
		return
	}
	a.storeSession(sid, sessionRecord{Identity: id, ExpiresAt: time.Now().Add(sessionTTL), UpstreamToken: protected})
	http.SetCookie(w, &http.Cookie{
		Name:     cookieName,
		Value:    a.signedSessionID(sid),
		Path:     "/_workspace/",
		HttpOnly: true,
		Secure:   a.cfg.CookieSecure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   int(sessionTTL.Seconds()),
	})
	writeJSON(w, http.StatusOK, id)
}

func (a *app) auth(r *http.Request) (identity, bool) {
	if cookie, err := r.Cookie(cookieName); err == nil {
		if id, ok := a.sessionIdentity(r.Context(), cookie.Value); ok {
			return id, true
		}
	}
	token := strings.TrimSpace(r.URL.Query().Get("token"))
	if token == "" {
		token = strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	}
	if token == "" {
		return identity{}, false
	}
	id, err := a.resolveToken(r.Context(), token)
	return id, err == nil
}

func (a *app) requireAuth(w http.ResponseWriter, r *http.Request) (identity, bool) {
	id, ok := a.auth(r)
	if !ok {
		writeAPIError(w, http.StatusUnauthorized, "需要登录")
		return identity{}, false
	}
	return id, true
}

func requireAdmin(w http.ResponseWriter, id identity) bool {
	if id.Role != "admin" {
		writeAPIError(w, http.StatusForbidden, "需要管理员权限")
		return false
	}
	return true
}

func (a *app) resolveToken(ctx context.Context, token string) (identity, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.cfg.UpstreamURL+"/api/v1/auth/me", nil)
	if err != nil {
		return identity{}, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := a.http.Do(req)
	if err != nil {
		return identity{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return identity{}, fmt.Errorf("upstream status %d", resp.StatusCode)
	}
	var payload struct {
		Code int `json:"code"`
		Data struct {
			ID       int64  `json:"id"`
			Username string `json:"username"`
			Email    string `json:"email"`
			Role     string `json:"role"`
			LabBusinessAccess *bool `json:"lab_business_access"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&payload); err != nil {
		return identity{}, err
	}
	if payload.Code != 0 || payload.Data.ID <= 0 || payload.Data.LabBusinessAccess != nil && !*payload.Data.LabBusinessAccess {
		return identity{}, errors.New("invalid identity")
	}
	if os.Getenv("LAB_ADMISSION_ENABLED") == "1" && payload.Data.LabBusinessAccess == nil {
		return identity{}, errors.New("upstream admission contract unavailable")
	}
	return identity{
		ID:       payload.Data.ID,
		Username: payload.Data.Username,
		Email:    payload.Data.Email,
		Role:     payload.Data.Role,
	}, nil
}

func (a *app) storeSession(id string, record sessionRecord) {
	if a.redisOK {
		_ = a.redis.Set(context.Background(), "workspace:session:"+id, marshal(record), sessionTTL).Err()
		return
	}
	a.mu.Lock()
	a.sessions[id] = record
	a.mu.Unlock()
}

func (a *app) sessionRecord(id string) (sessionRecord, bool) {
	if a.redisOK {
		value, err := a.redis.Get(context.Background(), "workspace:session:"+id).Result()
		if err != nil {
			return sessionRecord{}, false
		}
		var record sessionRecord
		if json.Unmarshal([]byte(value), &record) != nil || time.Now().After(record.ExpiresAt) {
			return sessionRecord{}, false
		}
		return record, true
	}
	a.mu.RLock()
	record, ok := a.sessions[id]
	a.mu.RUnlock()
	if !ok || time.Now().After(record.ExpiresAt) {
		return sessionRecord{}, false
	}
	return record, true
}

func (a *app) sessionIdentity(ctx context.Context, cookie string) (identity, bool) {
	id, ok := a.verifySessionID(cookie)
	if !ok {
		return identity{}, false
	}
	record, ok := a.sessionRecord(id)
	if !ok {
		return identity{}, false
	}
	token, err := a.openUpstreamToken(record.UpstreamToken)
	if err != nil {
		return identity{}, false
	}
	current, err := a.resolveToken(ctx, token)
	if err != nil || current.ID != record.Identity.ID {
		return identity{}, false
	}
	return current, true
}

func (a *app) sessionCipher() (cipher.AEAD, error) {
	key := sha256.Sum256(a.sessionKey)
	block, err := aes.NewCipher(key[:])
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func (a *app) protectUpstreamToken(token string) (string, error) {
	gcm, err := a.sessionCipher()
	if err != nil {
		return "", err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	protected := gcm.Seal(nonce, nonce, []byte(token), []byte("workspace-upstream-session-v1"))
	return base64.RawStdEncoding.EncodeToString(protected), nil
}

func (a *app) openUpstreamToken(protected string) (string, error) {
	gcm, err := a.sessionCipher()
	if err != nil {
		return "", err
	}
	raw, err := base64.RawStdEncoding.DecodeString(protected)
	if err != nil || len(raw) < gcm.NonceSize()+gcm.Overhead() {
		return "", errors.New("invalid workspace session")
	}
	token, err := gcm.Open(nil, raw[:gcm.NonceSize()], raw[gcm.NonceSize():], []byte("workspace-upstream-session-v1"))
	return string(token), err
}

func (a *app) signedSessionID(id string) string {
	mac := hmac.New(sha256.New, a.sessionKey)
	_, _ = mac.Write([]byte(id))
	return id + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func (a *app) verifySessionID(value string) (string, bool) {
	parts := strings.Split(value, ".")
	if len(parts) != 2 || len(parts[0]) != 32 {
		return "", false
	}
	if _, err := hex.DecodeString(parts[0]); err != nil {
		return "", false
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return "", false
	}
	mac := hmac.New(sha256.New, a.sessionKey)
	_, _ = mac.Write([]byte(parts[0]))
	return parts[0], hmac.Equal(sig, mac.Sum(nil))
}

func randomID() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		panic(err)
	}
	return hex.EncodeToString(buf)
}
