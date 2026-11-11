-- Administrator creation remains a trusted, audited path; self-registration still needs review.
ALTER TABLE lab_registration_requests
    ADD COLUMN IF NOT EXISTS provisioning_source varchar(16) NOT NULL DEFAULT 'self',
    ADD COLUMN IF NOT EXISTS actor_admin_id bigint,
    ADD COLUMN IF NOT EXISTS admin_disabled boolean NOT NULL DEFAULT false;
ALTER TABLE lab_registration_requests
    ALTER COLUMN student_no TYPE varchar(64),
    ALTER COLUMN email TYPE varchar(255);
ALTER TABLE lab_registration_requests
    DROP CONSTRAINT IF EXISTS lab_registration_requests_student_no_check;
ALTER TABLE lab_registration_requests
    ADD CONSTRAINT lab_registration_identity_check CHECK (
        (provisioning_source = 'self' AND student_no ~ '^[0-9]{10}$')
        OR (provisioning_source = 'admin_invite' AND actor_admin_id IS NOT NULL AND actor_admin_id > 0 AND student_no ~ '^[0-9]{10}$')
        OR (provisioning_source = 'admin_direct' AND actor_admin_id IS NOT NULL AND actor_admin_id > 0 AND student_no = 'account:' || user_id::text)
    );
