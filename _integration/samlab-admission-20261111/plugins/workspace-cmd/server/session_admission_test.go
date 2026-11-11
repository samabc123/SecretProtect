package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestWorkspaceCookieRevalidatesUpstreamAdmission(t *testing.T) {
	t.Setenv("LAB_ADMISSION_ENABLED", "1")
	var denied atomic.Bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/auth/me" || r.Header.Get("Authorization") != "Bearer synthetic-test-token" {
			t.Error("wrong upstream authentication")
		}
		if denied.Load() {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"code":0,"data":{"id":23,"role":"user","username":"synthetic","lab_business_access":true}}`))
	}))
	defer upstream.Close()
	a := &app{cfg: config{UpstreamURL: upstream.URL}, http: upstream.Client(),
		sessions: make(map[string]sessionRecord), sessionKey: []byte("synthetic-session-key-not-production")}
	protected, err := a.protectUpstreamToken("synthetic-test-token")
	if err != nil {
		t.Fatal(err)
	}
	sid := randomID()
	a.storeSession(sid, sessionRecord{Identity: identity{ID: 23}, ExpiresAt: time.Now().Add(time.Minute), UpstreamToken: protected})
	cookie := a.signedSessionID(sid)
	if _, ok := a.sessionIdentity(context.Background(), cookie); !ok {
		t.Fatal("valid admitted session denied")
	}
	denied.Store(true)
	if _, ok := a.sessionIdentity(context.Background(), cookie); ok {
		t.Fatal("revoked upstream session accepted from cached workspace cookie")
	}
	upstream.Close()
	if _, ok := a.sessionIdentity(context.Background(), cookie); ok {
		t.Fatal("upstream outage must fail closed")
	}
	if _, err := a.openUpstreamToken(protected + "tampered"); err == nil {
		t.Fatal("tampered encrypted token accepted")
	}
}

func TestWorkspaceLegacyCookieWithoutUpstreamTokenDenied(t *testing.T) {
	a := &app{sessions: make(map[string]sessionRecord), sessionKey: []byte("synthetic-session-key-not-production")}
	sid := randomID()
	a.storeSession(sid, sessionRecord{Identity: identity{ID: 23}, ExpiresAt: time.Now().Add(time.Minute)})
	if _, ok := a.sessionIdentity(context.Background(), a.signedSessionID(sid)); ok {
		t.Fatal("legacy cookie bypassed admission")
	}
}

func TestWorkspaceManagementAdminCannotBypassBusinessAdmission(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"code":0,"data":{"id":1,"role":"admin","lab_business_access":false}}`))
	}))
	defer upstream.Close()
	a := &app{cfg: config{UpstreamURL: upstream.URL}, http: upstream.Client()}
	if _, err := a.resolveToken(context.Background(), "synthetic-admin-token"); err == nil {
		t.Fatal("management identity bypassed business admission")
	}
}

func TestWorkspaceAdmissionModeRejectsOlderUpstreamContract(t *testing.T) {
	t.Setenv("LAB_ADMISSION_ENABLED", "1")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"code":0,"data":{"id":1,"role":"admin"}}`))
	}))
	defer upstream.Close()
	a := &app{cfg: config{UpstreamURL: upstream.URL}, http: upstream.Client()}
	if _, err := a.resolveToken(context.Background(), "synthetic-admin-token"); err == nil {
		t.Fatal("missing upstream admission result accepted")
	}
}
