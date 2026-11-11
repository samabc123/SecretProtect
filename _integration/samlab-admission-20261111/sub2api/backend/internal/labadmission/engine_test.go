package labadmission

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
)

func TestInputBoundaries(t *testing.T) {
	in := Input{RequestID: "test-request-0001", Username: "student01", Email: "s@example.edu"}
	if len(in.Validate()) != 0 {
		t.Fatal("valid application rejected")
	}
	in.Email = ""
	if in.Validate()["email"] == "" {
		t.Fatal("mandatory email not checked")
	}
	for _, email := range []string{"a..b@example.edu", "a.b@example", `"a"@example.edu`} {
		in.Email = email
		if in.Validate()["email"] == "" {
			t.Fatal("email rejected by Sentinel was accepted locally")
		}
	}
	in.Email = "student@example.edu"
	in.Username = ".student"
	if in.Validate()["username"] == "" {
		t.Fatal("username rejected by Sentinel was accepted locally")
	}
}

func TestAccessLeaseAndEpoch(t *testing.T) {
	for _, decision := range []string{"pending", "rejected", "revoked", "approved"} {
		db, mock, _ := sqlmock.New()
		e := &Engine{DB: db}
		until := time.Now().Add(time.Minute)
		if decision == "approved" {
			until = time.Now().Add(-time.Second)
		}
		mock.ExpectQuery("SELECT r.generation").WithArgs(int64(12)).
			WillReturnRows(sqlmock.NewRows([]string{"generation", "decision", "valid_until", "status"}).AddRow(2, decision, until, "active"))
		if _, err := e.Epoch(context.Background(), 12, "user"); !errors.Is(err, ErrDenied) {
			t.Fatal("invalid lease accepted")
		}
		db.Close()
	}
	db, mock, _ := sqlmock.New()
	defer db.Close()
	e := &Engine{DB: db}
	mock.ExpectQuery("SELECT r.generation").WithArgs(int64(12)).
		WillReturnRows(sqlmock.NewRows([]string{"generation", "decision", "valid_until", "status"}).AddRow(4, "approved", time.Now().Add(time.Minute), "active"))
	if err := e.CheckSession(context.Background(), 12, "user", 2); !errors.Is(err, ErrDenied) {
		t.Fatal("old session survived reenrollment")
	}
}

func TestManagementExceptionRequiresCurrentDatabaseRole(t *testing.T) {
	for _, activeAdmin := range []bool{false, true} {
		db, mock, _ := sqlmock.New()
		e := &Engine{DB: db}
		mock.ExpectQuery("SELECT r.generation").WithArgs(int64(12)).WillReturnError(sql.ErrNoRows)
		mock.ExpectQuery("SELECT EXISTS").WithArgs(int64(12)).
			WillReturnRows(sqlmock.NewRows([]string{"exists"}).AddRow(activeAdmin))
		epoch, err := e.Epoch(context.Background(), 12, "admin")
		if activeAdmin && (err != nil || epoch != 0) || !activeAdmin && !errors.Is(err, ErrDenied) {
			t.Fatal("management authorization relied on a cached role")
		}
		if err := mock.ExpectationsWereMet(); err != nil {
			t.Fatal(err)
		}
		db.Close()
	}
}

func TestStalePolicyCannotReactivate(t *testing.T) {
	db, mock, _ := sqlmock.New()
	defer db.Close()
	e := &Engine{DB: db}
	member := "member-1"
	mock.ExpectBegin()
	mock.ExpectQuery("SELECT decision,policy_version").WithArgs("test-request-0001").
		WillReturnRows(sqlmock.NewRows([]string{"decision", "policy_version"}).AddRow("revoked", 3))
	mock.ExpectRollback()
	err := e.apply(context.Background(), "test-request-0001", 12, RemoteStatus{
		RequestID: "test-request-0001", Decision: "approved", Allowed: true, MemberID: &member,
		PolicyVersion: 2, ValidUntil: float64(time.Now().Add(12 * time.Second).Unix()),
	})
	if !errors.Is(err, ErrDenied) {
		t.Fatal("stale approval accepted")
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestSignedWireContractDoesNotSendPassword(t *testing.T) {
	secret := []byte("Synthetic-integration-test-secret-32-bytes")
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		if strings.Contains(string(raw), "password") {
			t.Error("password was transmitted")
		}
		if strings.Contains(string(raw), "person_no") {
			t.Error("retired student number was transmitted")
		}
		digest := sha256.Sum256(raw)
		message := strings.Join([]string{r.Header.Get("X-Samlab-Timestamp"), r.Header.Get("X-Samlab-Nonce"), r.Method, r.URL.Path, hex.EncodeToString(digest[:])}, "\n")
		mac := hmac.New(sha256.New, secret)
		mac.Write([]byte(message))
		if r.Header.Get("X-Samlab-Signature") != hex.EncodeToString(mac.Sum(nil)) {
			t.Error("signature mismatch")
		}
		calls++
		json.NewEncoder(w).Encode(RemoteStatus{RequestID: "test-request-0001", Decision: "pending"})
	}))
	defer server.Close()
	e := &Engine{endpoint: server.URL, secret: secret, client: server.Client()}
	input := Input{RequestID: "test-request-0001", Username: "student01", StudentNo: "2026012345", Email: "s@example.edu"}
	var result RemoteStatus
	if err := e.call(context.Background(), "POST", "/api/integration/samlab/requests", input, &result); err != nil || calls != 1 || result.Decision != "pending" {
		t.Fatal("wire contract failed")
	}
}
