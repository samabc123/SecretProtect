// Package labadmission manages Samlab's own admission projection, not Sentinel's database.
package labadmission

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/mail"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

var (
	ErrDenied      = errors.New("lab admission is not valid")
	ErrUnavailable = errors.New("lab admission service unavailable")
	identifier     = regexp.MustCompile(`^[A-Za-z0-9_-]{16,100}$`)
	usernameRule   = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{2,31}$`)
	emailRule      = regexp.MustCompile("^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\\.[A-Za-z]{2,63}$")
)

func Enabled() bool { return os.Getenv("LAB_ADMISSION_ENABLED") == "1" }

const FixedConcurrency = 5

type Input struct {
	RequestID string `json:"request_id"`
	Username  string `json:"username"`
	StudentNo string `json:"-"`
	Email     string `json:"email"`
}

func (in *Input) Validate() map[string]string {
	in.Username, in.StudentNo, in.Email = strings.TrimSpace(in.Username), strings.TrimSpace(in.StudentNo), strings.ToLower(strings.TrimSpace(in.Email))
	fields := map[string]string{}
	if !identifier.MatchString(in.RequestID) {
		fields["request_id"] = "申请编号须为16至100位字母、数字、下划线或连字符。"
	}
	if !usernameRule.MatchString(in.Username) {
		fields["username"] = "用户名须为3至32位字母、数字、下划线、点或连字符，首位不能为点或连字符。"
	}
	addr, err := mail.ParseAddress(in.Email)
	if len(in.Email) > 180 || err != nil || addr.Address != in.Email || !emailRule.MatchString(in.Email) || strings.Contains(in.Email, "..") {
		fields["email"] = "邮箱必填，最多180个字符，请填写有效邮箱。"
	}
	return fields
}

type RemoteStatus struct {
	RequestID     string  `json:"request_id"`
	Decision      string  `json:"decision"`
	Allowed       bool    `json:"allowed"`
	MemberID      *string `json:"member_id"`
	PolicyVersion int64   `json:"policy_version"`
	AccountID     *string `json:"account_id"`
	AccountState  string  `json:"account_state"`
	ValidUntil    float64 `json:"valid_until"`
}

type Status struct {
	RequestID string `json:"request_id"`
	Decision  string `json:"decision"`
	Ready     bool   `json:"ready"`
}

type Engine struct {
	DB       *sql.DB
	endpoint string
	secret   []byte
	client   *http.Client
}

func Open(dsn string) (*Engine, error) {
	endpoint, err := url.Parse(os.Getenv("LAB_ADMISSION_URL"))
	if err != nil || endpoint == nil || endpoint.Host == "" || endpoint.User != nil || endpoint.RawQuery != "" ||
		endpoint.Fragment != "" || endpoint.Path != "" && endpoint.Path != "/" ||
		(endpoint.Scheme != "https" && !(endpoint.Scheme == "http" && os.Getenv("LAB_ADMISSION_ALLOW_HTTP") == "1")) {
		return nil, ErrUnavailable
	}
	secret := []byte(os.Getenv("SAMLAB_INTEGRATION_SECRET"))
	if path := os.Getenv("SAMLAB_INTEGRATION_SECRET_FILE"); path != "" {
		secret, err = os.ReadFile(path)
		if err != nil {
			return nil, ErrUnavailable
		}
		secret = bytes.TrimSpace(secret)
	}
	if len(secret) < 32 {
		return nil, ErrUnavailable
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, ErrUnavailable
	}
	db.SetMaxOpenConns(6)
	db.SetMaxIdleConns(2)
	db.SetConnMaxLifetime(5 * time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, ErrUnavailable
	}
	rows, err := db.QueryContext(ctx, "SELECT request_id FROM lab_registration_requests LIMIT 0")
	if err != nil {
		db.Close()
		return nil, ErrUnavailable
	}
	rows.Close()
	return &Engine{DB: db, endpoint: strings.TrimRight(endpoint.String(), "/"), secret: secret,
		client: &http.Client{Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

// Epoch is checked on token creation as well as every authenticated request.
// Only existing, unmanaged business administrators retain a management login.
func (e *Engine) Epoch(ctx context.Context, userID int64, role string) (int64, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var generation int64
	var decision, status string
	var until time.Time
	err := e.DB.QueryRowContext(ctx, `SELECT r.generation,r.decision,r.valid_until,u.status
		FROM lab_registration_requests r JOIN users u ON u.id=r.user_id
		WHERE r.user_id=$1 AND u.deleted_at IS NULL`, userID).Scan(&generation, &decision, &until, &status)
	if errors.Is(err, sql.ErrNoRows) && role == "admin" {
		var management bool
		if err := e.DB.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM users
			WHERE id=$1 AND role='admin' AND status='active' AND deleted_at IS NULL)`, userID).Scan(&management); err != nil {
			return 0, ErrUnavailable
		}
		if management {
			return 0, nil
		}
		return 0, ErrDenied
	}
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return 0, ErrUnavailable
	}
	if err != nil || decision != "approved" || !until.After(time.Now()) || status != "active" {
		return 0, ErrDenied
	}
	return generation, nil
}

func (e *Engine) CheckSession(ctx context.Context, userID int64, role string, epoch int64) error {
	current, err := e.Epoch(ctx, userID, role)
	if err != nil {
		return err
	}
	if epoch != current {
		return ErrDenied
	}
	return nil
}

func (e *Engine) CheckAPIKey(ctx context.Context, keyID, userID int64) error {
	if _, err := e.Epoch(ctx, userID, "user"); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var valid bool
	err := e.DB.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM api_keys k
		JOIN lab_registration_requests r ON r.user_id=k.user_id
		WHERE k.id=$1 AND k.user_id=$2 AND k.deleted_at IS NULL AND k.status<>'disabled'
		AND (r.revoked_at IS NULL OR k.created_at>r.revoked_at))`, keyID, userID).Scan(&valid)
	if err != nil {
		return ErrUnavailable
	}
	if !valid {
		return ErrDenied
	}
	return nil
}

func (e *Engine) Status(ctx context.Context, requestID string, userID int64) (Status, error) {
	var out Status
	err := e.DB.QueryRowContext(ctx, `SELECT r.request_id,r.decision,
		(r.decision='approved' AND r.valid_until>now() AND NOT r.activation_required AND u.status='active')
		FROM lab_registration_requests r JOIN users u ON u.id=r.user_id
		WHERE r.request_id=$1 AND r.user_id=$2 AND u.deleted_at IS NULL`, requestID, userID).Scan(&out.RequestID, &out.Decision, &out.Ready)
	return out, err
}

func (e *Engine) call(ctx context.Context, method, path string, body any, target any) error {
	var raw []byte
	if body != nil {
		var err error
		raw, err = json.Marshal(body)
		if err != nil {
			return ErrUnavailable
		}
	}
	nonceBytes := make([]byte, 24)
	if _, err := rand.Read(nonceBytes); err != nil {
		return ErrUnavailable
	}
	nonce, timestamp := hex.EncodeToString(nonceBytes), strconv.FormatInt(time.Now().Unix(), 10)
	digest := sha256.Sum256(raw)
	message := strings.Join([]string{timestamp, nonce, method, path, hex.EncodeToString(digest[:])}, "\n")
	mac := hmac.New(sha256.New, e.secret)
	mac.Write([]byte(message))
	req, err := http.NewRequestWithContext(ctx, method, e.endpoint+path, bytes.NewReader(raw))
	if err != nil {
		return ErrUnavailable
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Samlab-Timestamp", timestamp)
	req.Header.Set("X-Samlab-Nonce", nonce)
	req.Header.Set("X-Samlab-Signature", hex.EncodeToString(mac.Sum(nil)))
	resp, err := e.client.Do(req)
	if err != nil {
		return ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		// Never log response bodies, request metadata, tokens, or the configured URL.
		return fmt.Errorf("admission_http_%d", resp.StatusCode)
	}
	if target != nil {
		if err := json.NewDecoder(io.LimitReader(resp.Body, 16385)).Decode(target); err != nil {
			return ErrUnavailable
		}
	}
	return nil
}

// SyncOnce claims persisted work with a lease; another worker cannot claim it.
func (e *Engine) SyncOnce(ctx context.Context) (int, error) {
	claimCtx, claimCancel := context.WithTimeout(ctx, 2*time.Second)
	defer claimCancel()
	rows, err := e.DB.QueryContext(claimCtx, `WITH due AS (
		SELECT request_id FROM lab_registration_requests WHERE retry_at<=now() AND locked_until<=now()
		ORDER BY retry_at LIMIT 4 FOR UPDATE SKIP LOCKED)
		UPDATE lab_registration_requests r SET locked_until=now()+interval '60 seconds'
		FROM due WHERE r.request_id=due.request_id RETURNING r.request_id`)
	if err != nil {
		return 0, ErrUnavailable
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return 0, ErrUnavailable
		}
		ids = append(ids, id)
	}
	rowErr := rows.Err()
	rows.Close()
	if rowErr != nil {
		return 0, ErrUnavailable
	}
	for _, id := range ids {
		workCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
		err := e.syncRequest(workCtx, id)
		cancel()
		if err != nil {
			retryCtx, retryCancel := context.WithTimeout(ctx, 2*time.Second)
			_, _ = e.DB.ExecContext(retryCtx, `UPDATE lab_registration_requests SET attempts=attempts+1,
				last_error_code='sync_failed',locked_until='epoch',
				retry_at=now()+make_interval(secs=>LEAST(60,power(2,LEAST(attempts,5))::int))
				WHERE request_id=$1`, id)
			retryCancel()
		}
	}
	return len(ids), nil
}

func (e *Engine) syncRequest(ctx context.Context, id string) error {
	var input Input
	var userID, ackVersion int64
	var submitted bool
	var activation bool
	var ackState string
	var source, displayUsername string
	var actorID int64
	err := e.DB.QueryRowContext(ctx, `SELECT r.request_id,r.username,r.student_no,r.email,r.user_id,r.submitted,
		r.ack_version,r.ack_state,r.activation_required,r.provisioning_source,COALESCE(r.actor_admin_id,0),u.username
		FROM lab_registration_requests r JOIN users u ON u.id=r.user_id WHERE r.request_id=$1`,
		id).Scan(&input.RequestID, &input.Username, &input.StudentNo, &input.Email, &userID, &submitted,
		&ackVersion, &ackState, &activation, &source, &actorID, &displayUsername)
	if err != nil {
		return ErrUnavailable
	}
	path := "/api/integration/samlab/requests"
	if !submitted {
		submitPath, body := path, any(input)
		if source == "admin_direct" || source == "admin_invite" {
			submitPath = "/api/integration/samlab/admin-provision"
			body = map[string]any{
				"request_id": input.RequestID, "username": input.Username, "person_no": input.StudentNo,
				"email": input.Email, "account_id": strconv.FormatInt(userID, 10),
				"actor_id": actorID, "source": source, "display_username": displayUsername,
			}
		} else if source != "self" {
			return ErrDenied
		}
		if err := e.call(ctx, "POST", submitPath, body, &RemoteStatus{}); err != nil {
			return err
		}
		if _, err := e.DB.ExecContext(ctx, "UPDATE lab_registration_requests SET submitted=true WHERE request_id=$1", id); err != nil {
			return ErrUnavailable
		}
	}
	var remote RemoteStatus
	if err := e.call(ctx, "GET", path+"/"+id, nil, &remote); err != nil {
		return err
	}
	if err := e.apply(ctx, id, userID, remote); err != nil {
		return err
	}
	state := "disabled"
	if remote.Allowed {
		state = "active"
	}
	if remote.MemberID != nil && !(remote.Allowed && activation) && (ackVersion != remote.PolicyVersion || ackState != state) {
		body := map[string]any{"account_id": strconv.FormatInt(userID, 10), "policy_version": remote.PolicyVersion, "state": state}
		if err := e.call(ctx, "POST", path+"/"+id+"/acknowledgement", body, &RemoteStatus{}); err != nil {
			return err
		}
		if _, err := e.DB.ExecContext(ctx, "UPDATE lab_registration_requests SET ack_version=$2,ack_state=$3 WHERE request_id=$1", id, remote.PolicyVersion, state); err != nil {
			return ErrUnavailable
		}
	}
	_, err = e.DB.ExecContext(ctx, `UPDATE lab_registration_requests SET attempts=0,last_error_code='',
		locked_until='epoch',retry_at=now()+interval '2 seconds',updated_at=now() WHERE request_id=$1`, id)
	return err
}

func (e *Engine) apply(ctx context.Context, id string, userID int64, remote RemoteStatus) error {
	if remote.RequestID != id || remote.PolicyVersion < 0 || remote.AccountID != nil && *remote.AccountID != strconv.FormatInt(userID, 10) {
		return ErrDenied
	}
	decision := remote.Decision
	until := time.Unix(0, 0)
	if remote.Allowed {
		if remote.Decision != "approved" || remote.MemberID == nil || *remote.MemberID == "" || remote.PolicyVersion < 1 ||
			remote.ValidUntil <= float64(time.Now().Unix()) || remote.ValidUntil > float64(time.Now().Add(16*time.Second).Unix()) {
			return ErrDenied
		}
		until = time.UnixMilli(int64(remote.ValidUntil * 1000))
	} else if decision == "approved" {
		decision = "revoked"
	}
	if decision != "pending" && decision != "approved" && decision != "rejected" && decision != "revoked" {
		return ErrDenied
	}
	tx, err := e.DB.BeginTx(ctx, nil)
	if err != nil {
		return ErrUnavailable
	}
	defer tx.Rollback()
	var previous string
	var version int64
	if err := tx.QueryRowContext(ctx, "SELECT decision,policy_version FROM lab_registration_requests WHERE request_id=$1 FOR UPDATE", id).Scan(&previous, &version); err != nil {
		return ErrUnavailable
	}
	if remote.PolicyVersion < version {
		return ErrDenied
	}
	member := ""
	if remote.MemberID != nil {
		member = *remote.MemberID
	}
	changed := (previous == "approved") != (decision == "approved")
	_, err = tx.ExecContext(ctx, `UPDATE lab_registration_requests SET decision=$2,member_id=$3,
		policy_version=$4,valid_until=$5,generation=generation+CASE WHEN $6 THEN 1 ELSE 0 END,
		revoked_at=CASE WHEN $7 THEN now() ELSE revoked_at END,updated_at=now() WHERE request_id=$1`,
		id, decision, member, remote.PolicyVersion, until, changed, previous == "approved" && decision != "approved")
	if err != nil {
		return ErrUnavailable
	}
	status := "disabled"
	if decision == "approved" {
		status = "active"
	}
	result, err := tx.ExecContext(ctx, `UPDATE users SET status=CASE WHEN
		(SELECT activation_required OR admin_disabled FROM lab_registration_requests WHERE user_id=$1)
		THEN 'disabled' ELSE $2 END,updated_at=now() WHERE id=$1 AND deleted_at IS NULL`, userID, status)
	if err != nil {
		return ErrUnavailable
	}
	if count, err := result.RowsAffected(); err != nil || count != 1 {
		return ErrUnavailable
	}
	if changed && decision != "approved" {
		if _, err := tx.ExecContext(ctx, "UPDATE api_keys SET status='disabled',updated_at=now() WHERE user_id=$1 AND deleted_at IS NULL", userID); err != nil {
			return ErrUnavailable
		}
		if _, err := tx.ExecContext(ctx, "UPDATE lab_account_invitations SET used_at=now() WHERE user_id=$1 AND used_at IS NULL", userID); err != nil {
			return ErrUnavailable
		}
	}
	if previous != decision || version != remote.PolicyVersion {
		if _, err := tx.ExecContext(ctx, "INSERT INTO lab_admission_audit(user_id,action,policy_version) VALUES ($1,$2,$3)", userID, "policy."+decision, remote.PolicyVersion); err != nil {
			return ErrUnavailable
		}
	}
	return tx.Commit()
}
