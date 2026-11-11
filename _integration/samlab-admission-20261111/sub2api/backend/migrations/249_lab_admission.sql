-- Additive, single-platform admission state. No existing account is enrolled.
CREATE TABLE IF NOT EXISTS lab_registration_requests (
    request_id varchar(100) PRIMARY KEY,
    user_id bigint NOT NULL UNIQUE REFERENCES users(id),
    username varchar(32) NOT NULL,
    student_no varchar(10) NOT NULL UNIQUE CHECK (student_no ~ '^[0-9]{10}$'),
    email varchar(180) NOT NULL,
    decision varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (decision IN ('pending','approved','rejected','revoked')),
    member_id varchar(128) NOT NULL DEFAULT '',
    policy_version bigint NOT NULL DEFAULT 0,
    generation bigint NOT NULL DEFAULT 1,
    valid_until timestamptz NOT NULL DEFAULT 'epoch',
    revoked_at timestamptz,
    submitted boolean NOT NULL DEFAULT false,
    activation_required boolean NOT NULL DEFAULT false,
    ack_version bigint NOT NULL DEFAULT 0,
    ack_state varchar(16) NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 0,
    retry_at timestamptz NOT NULL DEFAULT now(),
    locked_until timestamptz NOT NULL DEFAULT 'epoch',
    last_error_code varchar(64) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS lab_registration_username ON lab_registration_requests(lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS lab_registration_email ON lab_registration_requests(lower(email));
CREATE INDEX IF NOT EXISTS lab_registration_due ON lab_registration_requests(retry_at,locked_until);
CREATE TABLE IF NOT EXISTS lab_admission_audit (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL,
    action varchar(64) NOT NULL,
    policy_version bigint NOT NULL,
    actor_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lab_account_invitations (
    token_hash char(64) PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lab_invitation_user ON lab_account_invitations(user_id);
