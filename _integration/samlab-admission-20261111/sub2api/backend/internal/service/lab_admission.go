package service

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"unicode/utf8"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/internal/labadmission"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

type labRegistrationKey struct{}
type labDelegationKey struct{}
type labDelegatingAdminKey struct{}

func (s *AuthService) LabAdmission() *labadmission.Engine { return s.labAdmission }

func (s *AuthService) CheckLabSession(ctx context.Context, userID int64, role string, epoch int64) error {
	if s.labAdmission != nil {
		if err := s.labAdmission.CheckSession(ctx, userID, role, epoch); err != nil {
			return infraerrors.Forbidden("LAB_ADMISSION_REQUIRED", "实验室准入未批准、已撤销或暂时无法确认，请重新登录或联系管理员。")
		}
	}
	return nil
}

// SubmitLabRegistration atomically reserves a disabled account and its durable delivery work.
func (s *AuthService) SubmitLabRegistration(ctx context.Context, in labadmission.Input, password, verifyCode, invitationCode string) (*labadmission.Status, error) {
	if s.labAdmission == nil || s.entClient == nil {
		return nil, ErrServiceUnavailable
	}
	if len(in.Validate()) > 0 || utf8.RuneCountInString(password) < 6 || utf8.RuneCountInString(password) > 128 {
		return nil, infraerrors.BadRequest("LAB_FIELDS_INVALID", "用户名、邮箱或密码长度不符合要求。")
	}
	tx, err := s.entClient.Tx(ctx)
	if err != nil {
		return nil, ErrServiceUnavailable
	}
	defer tx.Rollback()
	execCtx := dbent.NewTxContext(ctx, tx)
	if _, err := tx.Client().ExecContext(execCtx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", in.RequestID); err != nil {
		return nil, ErrServiceUnavailable
	}
	delegated, _ := ctx.Value(labDelegationKey{}).(bool)
	actorID, _ := ctx.Value(labDelegatingAdminKey{}).(int64)
	source := "self"
	if delegated {
		if actorID <= 0 {
			return nil, infraerrors.Forbidden("LAB_ADMIN_REQUIRED", "需要可审计的管理员身份。")
		}
		source = "admin_invite"
		actorRows, err := tx.Client().QueryContext(execCtx,
			"SELECT id FROM users WHERE id=$1 AND role='admin' AND status='active' AND deleted_at IS NULL FOR SHARE", actorID)
		if err != nil {
			return nil, ErrServiceUnavailable
		}
		authorized := actorRows.Next()
		actorErr := actorRows.Err()
		actorRows.Close()
		if actorErr != nil {
			return nil, ErrServiceUnavailable
		}
		if !authorized {
			return nil, infraerrors.Forbidden("LAB_ADMIN_REQUIRED", "管理员身份已失效。")
		}
	}
	rows, err := tx.Client().QueryContext(execCtx, `SELECT user_id,username,student_no,email
		FROM lab_registration_requests WHERE request_id=$1`, in.RequestID)
	if err != nil {
		return nil, ErrServiceUnavailable
	}
	if rows.Next() {
		var userID int64
		var username, personNo, email string
		err := rows.Scan(&userID, &username, &personNo, &email)
		rows.Close()
		if err != nil || username != in.Username || email != in.Email {
			return nil, infraerrors.Conflict("LAB_REQUEST_CONFLICT", "申请编号已绑定其他内容。")
		}
		user, err := s.userRepo.GetByID(execCtx, userID)
		// Authenticated delegates may read an identical request, never change its activation mode or password.
		if err != nil || !delegated && !s.CheckPassword(password, user.PasswordHash) {
			return nil, ErrInvalidCredentials
		}
		tx.Rollback()
		status, err := s.labAdmission.Status(ctx, in.RequestID, userID)
		return &status, err
	}
	rowErr := rows.Err()
	rows.Close()
	if rowErr != nil {
		return nil, ErrServiceUnavailable
	}
	execCtx = context.WithValue(execCtx, labRegistrationKey{}, in)
	_, user, err := s.RegisterWithVerification(execCtx, in.Email, password, verifyCode, "", invitationCode, "")
	if err != nil {
		return nil, err
	}
	in.StudentNo = fmt.Sprintf("account:%d", user.ID)
	if _, err := tx.Client().ExecContext(execCtx, `INSERT INTO lab_registration_requests
		(request_id,user_id,username,student_no,email,activation_required,provisioning_source,actor_admin_id)
		VALUES ($1,$2,$3,$4,$5,$6,$7,NULLIF($8,0))`,
		in.RequestID, user.ID, in.Username, in.StudentNo, in.Email, delegated, source, actorID); err != nil {
		return nil, infraerrors.Conflict("LAB_APPLICATION_CONFLICT", "用户名或邮箱已有申请，请查询原申请。")
	}
	if err := tx.Commit(); err != nil {
		return nil, ErrServiceUnavailable
	}
	// Account defaults do not grant access: status and the admission lease both deny it.
	s.postAuthUserBootstrap(ctx, user, "email", false)
	grant := s.resolveSignupGrantPlan(ctx, "email")
	s.assignSubscriptions(ctx, user.ID, grant.Subscriptions, "lab application account defaults")
	_ = s.snapshotPlatformQuotaDefaults(ctx, user.ID, &grant)
	status, err := s.labAdmission.Status(ctx, in.RequestID, user.ID)
	return &status, err
}

func (s *AuthService) DelegateLabRegistration(ctx context.Context, in labadmission.Input, actorID int64) (*labadmission.Status, error) {
	temporary, err := randomHexString(32)
	if err != nil {
		return nil, ErrServiceUnavailable
	}
	ctx = context.WithValue(context.WithValue(ctx, labDelegationKey{}, true), labDelegatingAdminKey{}, actorID)
	return s.SubmitLabRegistration(ctx, in, temporary, "", "")
}

func (s *AuthService) LabApplicationStatus(ctx context.Context, requestID, email, password string) (*labadmission.Status, error) {
	if s.labAdmission == nil {
		return nil, ErrServiceUnavailable
	}
	user, err := s.userRepo.GetByEmail(ctx, strings.ToLower(strings.TrimSpace(email)))
	if err != nil || !s.CheckPassword(password, user.PasswordHash) {
		return nil, ErrInvalidCredentials
	}
	status, err := s.labAdmission.Status(ctx, requestID, user.ID)
	if err == sql.ErrNoRows {
		return nil, ErrInvalidCredentials
	}
	if err != nil {
		return nil, ErrServiceUnavailable
	}
	return &status, nil
}
