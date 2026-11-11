package main

import (
	"context"
	"database/sql"
	"os"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestArchiveAdmissionModeRejectsOldUpstreamContract(t *testing.T) {
	t.Setenv("LAB_ADMISSION_ENABLED", "1")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"code":0,"data":{"id":1,"role":"admin","status":"active"}}`))
	}))
	defer upstream.Close()
	endpoint, _ := url.Parse(upstream.URL)
	s := &proxyServer{cfg: config{UpstreamURL: endpoint}, adminClient: upstream.Client()}
	request := httptest.NewRequest("GET", "/", nil)
	request.Header.Set("Authorization", "Bearer synthetic-old-contract-token")
	if _, status, err := s.validateSub2APIUser(request); err == nil || status != http.StatusForbidden {
		t.Fatal("missing admission result accepted")
	}
}

func TestArchiveUploadAdmissionWithIsolatedPostgres(t *testing.T) {
	dsn := os.Getenv("LAB_ADMISSION_DATABASE_URL")
	if !strings.Contains(dsn, ":55433/labtest?") {
		t.Skip("requires explicitly isolated database, never production")
	}
	t.Setenv("LAB_ADMISSION_ENABLED", "1")
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, `CREATE TEMP TABLE users(id bigint,status text,deleted_at timestamptz) ON COMMIT DROP;
		CREATE TEMP TABLE lab_registration_requests(user_id bigint,decision text,valid_until timestamptz,
			activation_required boolean,revoked_at timestamptz) ON COMMIT DROP;
		INSERT INTO users VALUES(91,'active',NULL);
		INSERT INTO lab_registration_requests VALUES(91,'approved',now()+interval '1 hour',false,NULL)`)
	if err != nil {
		t.Fatal(err)
	}
	granted := time.Now().Add(-time.Minute)
	_, err = tx.ExecContext(ctx, `CREATE TEMP TABLE trajectory_upload_sessions(
		id text,import_batch_id text,user_id bigint,status text,max_bytes bigint,received_bytes bigint,
		total_records int,inserted_records int,updated_records int,skipped_records int,failed_records int,
		created_at timestamptz,expires_at timestamptz,completed_at timestamptz,token_hash text) ON COMMIT DROP;
		INSERT INTO trajectory_upload_sessions VALUES('session','batch',91,'active',1024,0,0,0,0,0,0,
			now()-interval '1 minute',now()+interval '1 hour',NULL,'synthetic-upload-token-hash')`)
	if err != nil { t.Fatal(err) }
	if _, err := lockUploadSession(ctx, tx, "synthetic-upload-token-hash", time.Now()); err != nil {
		t.Fatal("valid upload session denied:", err)
	}
	if err := checkLabUploadGrant(ctx, tx, 91, granted); err != nil {
		t.Fatal("approved upload denied:", err)
	}
	if err := checkLabUploadGrant(ctx, tx, 92, granted); err == nil {
		t.Fatal("unmapped identity accepted")
	}
	for _, statement := range []string{
		"UPDATE lab_registration_requests SET decision='revoked'",
		"UPDATE lab_registration_requests SET decision='approved',valid_until=now()-interval '1 second'",
		"UPDATE lab_registration_requests SET valid_until=now()+interval '1 hour',activation_required=true",
		"UPDATE lab_registration_requests SET activation_required=false,revoked_at=now()",
	} {
		if _, err := tx.ExecContext(ctx, statement); err != nil {
			t.Fatal(err)
		}
		if err := checkLabUploadGrant(ctx, tx, 91, granted); err == nil {
			t.Fatal("expired, revoked or pre-revocation upload grant accepted")
		}
		if _, err := lockUploadSession(ctx, tx, "synthetic-upload-token-hash", time.Now()); err == nil {
			t.Fatal("shared upload/session/completion gate accepted invalid admission")
		}
	}
	if err := checkLabUploadGrant(ctx, tx, 91, time.Now().Add(time.Minute)); err != nil {
		t.Fatal("new grant after reapproval denied:", err)
	}
	if _, err := tx.ExecContext(ctx, "UPDATE users SET status='disabled'"); err != nil {
		t.Fatal(err)
	}
	if err := checkLabUploadGrant(ctx, tx, 91, time.Now().Add(time.Minute)); err == nil {
		t.Fatal("disabled account accepted")
	}
}
