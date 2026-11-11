package main

import (
	"context"
	"database/sql"
	"os"
	"time"
)

// Archive and gateway share Samlab's database, never Sentinel's database.
func checkLabUploadGrant(ctx context.Context, tx *sql.Tx, userID int64, grantedAt time.Time) error {
	if os.Getenv("LAB_ADMISSION_ENABLED") != "1" {
		return nil
	}
	var allowed bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(
		SELECT 1 FROM lab_registration_requests r JOIN users u ON u.id=r.user_id
		WHERE r.user_id=$1 AND u.deleted_at IS NULL AND u.status='active'
		AND r.decision='approved' AND r.valid_until>now() AND NOT r.activation_required
		AND (r.revoked_at IS NULL OR r.revoked_at<$2))`, userID, grantedAt).Scan(&allowed)
	if err != nil {
		return err
	}
	if !allowed {
		return errUploadTokenInvalid
	}
	return nil
}
