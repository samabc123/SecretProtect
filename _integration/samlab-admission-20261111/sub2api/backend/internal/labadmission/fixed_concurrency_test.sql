-- Run only against the disposable candidate database; all work is rolled back.
BEGIN;
DO $$
DECLARE
    target_user bigint;
    rejected boolean := false;
    current_limit integer;
BEGIN
    SELECT u.id INTO target_user
    FROM users u JOIN lab_registration_requests r ON r.user_id=u.id
    WHERE r.provisioning_source='admin_direct' AND u.email LIKE 'direct-%@example.edu'
    ORDER BY u.id DESC LIMIT 1;
    IF target_user IS NULL THEN
        RAISE EXCEPTION 'No synthetic candidate account exists';
    END IF;
    BEGIN
        UPDATE users SET concurrency=9 WHERE id=target_user;
    EXCEPTION WHEN check_violation THEN
        rejected := true;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'Fixed concurrency constraint was bypassed';
    END IF;
    SELECT concurrency INTO current_limit FROM users WHERE id=target_user;
    IF current_limit <> 5 THEN
        RAISE EXCEPTION 'Synthetic concurrency value is not five';
    END IF;
    RAISE NOTICE 'PASS: database rejects a direct concurrency override; synthetic account remains at five';
END
$$;
ROLLBACK;
