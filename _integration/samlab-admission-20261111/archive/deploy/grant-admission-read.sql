-- Deployment prerequisite only. Supply archive_role with psql -v; never grant to PUBLIC.
GRANT SELECT (user_id, decision, valid_until, activation_required, revoked_at)
    ON TABLE lab_registration_requests TO :"archive_role";
GRANT SELECT (id, status, deleted_at) ON TABLE users TO :"archive_role";
