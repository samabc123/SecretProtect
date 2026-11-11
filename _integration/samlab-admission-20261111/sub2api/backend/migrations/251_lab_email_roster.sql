-- Keep historical student numbers for recovery; all new mappings use the platform account ID.
ALTER TABLE lab_registration_requests DROP CONSTRAINT lab_registration_identity_check;
ALTER TABLE lab_registration_requests ADD CONSTRAINT lab_registration_identity_check CHECK (
    provisioning_source IN ('self','admin_invite','admin_direct')
    AND (provisioning_source = 'self' OR (actor_admin_id IS NOT NULL AND actor_admin_id > 0))
    AND (student_no = 'account:' || user_id::text
         OR (provisioning_source <> 'admin_direct' AND student_no ~ '^[0-9]{10}$'))
);

-- Enforce the policy even when a legacy balance/redeem/batch path updates user limits.
CREATE FUNCTION lab_fixed_user_concurrency() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.concurrency <> 5 AND EXISTS (
        SELECT 1 FROM lab_registration_requests WHERE user_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'Lab user concurrency is fixed at 5' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER lab_fixed_user_concurrency BEFORE UPDATE OF concurrency ON users
FOR EACH ROW EXECUTE FUNCTION lab_fixed_user_concurrency();

CREATE FUNCTION lab_initialize_user_concurrency() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE users SET concurrency=5 WHERE id=NEW.user_id AND concurrency<>5;
    RETURN NEW;
END
$$;
CREATE TRIGGER lab_initialize_user_concurrency AFTER INSERT ON lab_registration_requests
FOR EACH ROW EXECUTE FUNCTION lab_initialize_user_concurrency();

INSERT INTO lab_admission_audit(user_id,action,policy_version)
SELECT u.id,'policy.concurrency.fixed5',r.policy_version
FROM users u JOIN lab_registration_requests r ON r.user_id=u.id WHERE u.concurrency<>5;
UPDATE users SET concurrency=5,updated_at=now()
WHERE concurrency<>5 AND id IN (SELECT user_id FROM lab_registration_requests);
