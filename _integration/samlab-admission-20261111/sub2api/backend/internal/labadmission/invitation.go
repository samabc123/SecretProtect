package labadmission

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"time"
	"unicode/utf8"

	"github.com/Wei-Shaw/sub2api/internal/pkg/passwordcompat"
)

func (e *Engine) Invite(ctx context.Context, requestID string, actorID int64) (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", ErrUnavailable
	}
	token := hex.EncodeToString(raw)
	hash := sha256.Sum256([]byte(token))
	tx, err := e.DB.BeginTx(ctx, nil)
	if err != nil {
		return "", ErrUnavailable
	}
	defer tx.Rollback()
	var userID int64
	if err := tx.QueryRowContext(ctx, `SELECT user_id FROM lab_registration_requests
		WHERE request_id=$1 AND decision='approved' AND valid_until>now() AND activation_required=true
		FOR UPDATE`, requestID).Scan(&userID); err != nil {
		return "", ErrDenied
	}
	if _, err := tx.ExecContext(ctx, "UPDATE lab_account_invitations SET used_at=now() WHERE user_id=$1 AND used_at IS NULL", userID); err != nil {
		return "", ErrUnavailable
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO lab_account_invitations(token_hash,user_id,expires_at) VALUES ($1,$2,now()+interval '15 minutes')", hex.EncodeToString(hash[:]), userID); err != nil {
		return "", ErrUnavailable
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO lab_admission_audit(user_id,action,policy_version,actor_id) SELECT user_id,'invitation.issued',policy_version,$2 FROM lab_registration_requests WHERE user_id=$1", userID, actorID); err != nil {
		return "", ErrUnavailable
	}
	if err := tx.Commit(); err != nil {
		return "", ErrUnavailable
	}
	return "/lab-activate#token=" + token, nil
}

func (e *Engine) Activate(ctx context.Context, token, password string) error {
	if len(token) != 64 || utf8.RuneCountInString(password) < 6 || utf8.RuneCountInString(password) > 128 {
		return ErrDenied
	}
	if _, err := hex.DecodeString(token); err != nil {
		return ErrDenied
	}
	hash := sha256.Sum256([]byte(token))
	passwordHash, err := passwordcompat.Hash(password)
	if err != nil {
		return ErrUnavailable
	}
	tx, err := e.DB.BeginTx(ctx, nil)
	if err != nil {
		return ErrUnavailable
	}
	defer tx.Rollback()
	var userID int64
	if err := tx.QueryRowContext(ctx, `SELECT r.user_id FROM lab_account_invitations i
		JOIN lab_registration_requests r ON r.user_id=i.user_id
		WHERE i.token_hash=$1 AND i.used_at IS NULL AND i.expires_at>now()
		AND r.decision='approved' AND r.valid_until>now() AND r.activation_required=true AND NOT r.admin_disabled
		FOR UPDATE OF r,i`, hex.EncodeToString(hash[:])).Scan(&userID); err != nil {
		return ErrDenied
	}
	result, err := tx.ExecContext(ctx, "UPDATE users SET password_hash=$2,status='active',updated_at=now() WHERE id=$1 AND deleted_at IS NULL", userID, passwordHash)
	if err != nil {
		return ErrUnavailable
	}
	if count, err := result.RowsAffected(); err != nil || count != 1 {
		return ErrDenied
	}
	if _, err := tx.ExecContext(ctx, "UPDATE lab_registration_requests SET activation_required=false,generation=generation+1,updated_at=now() WHERE user_id=$1", userID); err != nil {
		return ErrUnavailable
	}
	if _, err := tx.ExecContext(ctx, "UPDATE lab_account_invitations SET used_at=now() WHERE user_id=$1 AND used_at IS NULL", userID); err != nil {
		return ErrUnavailable
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO lab_admission_audit(user_id,action,policy_version) SELECT user_id,'invitation.activated',policy_version FROM lab_registration_requests WHERE user_id=$1", userID); err != nil {
		return ErrUnavailable
	}
	if err := tx.Commit(); err != nil {
		return ErrUnavailable
	}
	return nil
}

func (e *Engine) AdminRequests(ctx context.Context) ([]map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	rows, err := e.DB.QueryContext(ctx, `SELECT request_id,username,student_no,email,decision,activation_required,
		(decision='approved' AND valid_until>now()),provisioning_source
		FROM lab_registration_requests ORDER BY created_at DESC LIMIT 200`)
	if err != nil {
		return nil, ErrUnavailable
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var id, username, student, email, decision, source string
		var activation, approved bool
		if err := rows.Scan(&id, &username, &student, &email, &decision, &activation, &approved, &source); err != nil {
			return nil, ErrUnavailable
		}
		out = append(out, map[string]any{"request_id": id, "username": username, "email": email, "decision": decision, "activation_required": activation, "approved": approved, "source": source})
	}
	return out, rows.Err()
}
