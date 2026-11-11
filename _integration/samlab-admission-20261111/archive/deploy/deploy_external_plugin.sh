#!/bin/sh
set -eu

release_dir=${1:-/opt/sub2api/releases/conversation-archive-plugin-v1}
main_compose=${MAIN_COMPOSE:-/opt/sub2api/docker-compose.yml}
archive_dir=${ARCHIVE_DIR:-/opt/sub2api/archive-proxy}
archive_compose=$archive_dir/docker-compose.yml
archive_env=$archive_dir/.env
network=${SUB2API_NETWORK:-sub2api_sub2api-network}
postgres_container=${POSTGRES_CONTAINER:-sub2api-postgres}
sub2api_container=${SUB2API_CONTAINER:-sub2api}
privacy_container=${PRIVACY_CONTAINER:-sub2api-privacy-filter}
archive_public_url=${ARCHIVE_PUBLIC_URL:-https://researchchat.samlab.cn}
case "$archive_public_url" in
  https://*) ;;
  *) echo "ARCHIVE_PUBLIC_URL must use HTTPS" >&2; exit 1 ;;
esac
stamp=$(date -u +%Y%m%dT%H%M%SZ)
main_backup=$main_compose.pre-archive-plugin-$stamp
archive_compose_backup=$archive_compose.pre-archive-plugin-$stamp
deployment_ok=0
official_switched=0
archive_existed=0
archive_env_created=0

probe_sub2api_client_request_id() {
  container=$1
  probe_key=$(docker exec -i "$postgres_container" sh -c \
    'psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT key
FROM api_keys
WHERE status = 'active'
  AND deleted_at IS NULL
ORDER BY last_used_at DESC NULLS LAST, id
LIMIT 1;
SQL
  )
  if [ -z "$probe_key" ]; then
    echo "Sub2API compatibility gate failed: no active API key is available for an authenticated probe" >&2
    return 1
  fi
  header_probe=$(docker exec "$container" sh -c \
    'wget -S -T 10 -O /dev/null --header "Authorization: Bearer $1" http://127.0.0.1:8080/v1/models 2>&1 || true' \
    sh "$probe_key")
  probe_key=
  if ! printf '%s\n' "$header_probe" | grep -qi 'X-Client-Request-ID:'; then
    echo "Sub2API compatibility gate failed: authenticated gateway response is missing X-Client-Request-ID" >&2
    return 1
  fi
}

rollback() {
  rc=$?
  if [ "$deployment_ok" -eq 0 ]; then
    docker rm -f sub2api-conversation-archive-canary >/dev/null 2>&1 || true
    if [ "$official_switched" -eq 1 ] && [ -f "$main_backup" ]; then
      cp "$main_backup" "$main_compose"
      docker compose -f "$main_compose" up -d --no-deps sub2api >/dev/null 2>&1 || true
    fi
    if [ -f "$archive_compose_backup" ]; then
      cp "$archive_compose_backup" "$archive_compose"
    fi
    if docker image inspect sub2api-conversation-archive:pre-external-plugin >/dev/null 2>&1; then
      docker tag sub2api-conversation-archive:pre-external-plugin sub2api-conversation-archive:4 >/dev/null 2>&1 || true
      if [ -f "$archive_compose" ] && [ -f "$archive_env" ]; then
        docker compose --env-file "$archive_env" -f "$archive_compose" up -d >/dev/null 2>&1 || true
      fi
    elif [ "$archive_existed" -eq 0 ]; then
      docker rm -f sub2api-conversation-archive >/dev/null 2>&1 || true
    fi
    if [ "$archive_env_created" -eq 1 ]; then
      rm -f "$archive_env"
    fi
  fi
  exit "$rc"
}
trap rollback EXIT INT TERM

test -f "$release_dir/Dockerfile"
test -f "$release_dir/migrations/001_external_archive_plugin.sql"
test -f "$release_dir/migrations/002_agent_trajectories.sql"
test -f "$release_dir/migrations/003_trajectory_https_upload.sql"
test -f "$release_dir/deploy/docker-compose.production.yml"
test -f "$release_dir/deploy/import-trajectories.sh"
test -f "$main_compose"

mkdir -p "$archive_dir"
cp "$main_compose" "$main_backup"
if [ -f "$archive_compose" ]; then
  cp "$archive_compose" "$archive_compose_backup"
fi
if docker inspect sub2api-conversation-archive >/dev/null 2>&1; then
  archive_existed=1
  docker tag "$(docker inspect --format '{{.Image}}' sub2api-conversation-archive)" \
    sub2api-conversation-archive:pre-external-plugin
fi

docker build -t sub2api-conversation-archive:4 "$release_dir"

docker exec -i "$postgres_container" sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$release_dir/migrations/001_external_archive_plugin.sql"
docker exec -i "$postgres_container" sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$release_dir/migrations/002_agent_trajectories.sql"
docker exec -i "$postgres_container" sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$release_dir/migrations/003_trajectory_https_upload.sql"

if [ -f "$archive_env" ]; then
  archive_password=$(sed -n 's|^ARCHIVE_DATABASE_URL=postgresql://conversation_archiver:\([^@]*\)@.*|\1|p' "$archive_env" | head -n 1)
  if [ -z "$archive_password" ]; then
    echo "existing archive environment does not contain a reusable conversation_archiver credential" >&2
    exit 1
  fi
  importer_password=$(sed -n 's|^TRAJECTORY_IMPORT_DATABASE_URL=postgresql://trajectory_importer:\([^@]*\)@.*|\1|p' "$archive_env" | head -n 1)
  if [ -z "$importer_password" ]; then
    importer_password=$(openssl rand -hex 32)
  fi
else
  archive_password=$(openssl rand -hex 32)
  importer_password=$(openssl rand -hex 32)
  archive_env_created=1
fi
docker exec -i "$postgres_container" sh -c \
  'psql -v ON_ERROR_STOP=1 -v archive_password="$1" -v importer_password="$2" -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  sh "$archive_password" "$importer_password" <<'SQL'
SELECT 'CREATE ROLE conversation_archiver LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'conversation_archiver')
\gexec
ALTER ROLE conversation_archiver WITH LOGIN PASSWORD :'archive_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

SELECT 'CREATE ROLE trajectory_importer LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trajectory_importer')
\gexec
ALTER ROLE trajectory_importer WITH LOGIN PASSWORD :'importer_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

SELECT 'CREATE ROLE conversation_archive_identity_owner NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'conversation_archive_identity_owner')
\gexec
ALTER ROLE conversation_archive_identity_owner WITH NOLOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

SELECT format('GRANT CONNECT ON DATABASE %I TO conversation_archiver', current_database())
\gexec
REVOKE ALL ON SCHEMA public FROM conversation_archiver;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM conversation_archiver;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM conversation_archiver;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM conversation_archiver;
GRANT USAGE ON SCHEMA public TO conversation_archiver;
GRANT SELECT, INSERT, DELETE ON TABLE conversation_archives TO conversation_archiver;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversation_archive_pending TO conversation_archiver;
GRANT SELECT, DELETE ON TABLE agent_trajectory_import_batches TO conversation_archiver;
GRANT SELECT, DELETE ON TABLE agent_trajectories TO conversation_archiver;
GRANT SELECT ON TABLE agent_trajectory_events TO conversation_archiver;
GRANT SELECT ON TABLE agent_trajectory_labels TO conversation_archiver;
GRANT USAGE, SELECT ON SEQUENCE conversation_archives_id_seq TO conversation_archiver;
GRANT USAGE, SELECT ON SEQUENCE conversation_archive_pending_id_seq TO conversation_archiver;
GRANT USAGE, SELECT ON SEQUENCE agent_trajectories_id_seq TO conversation_archiver;
GRANT USAGE, SELECT ON SEQUENCE agent_trajectory_events_id_seq TO conversation_archiver;
GRANT USAGE, SELECT ON SEQUENCE agent_trajectory_labels_id_seq TO conversation_archiver;

SELECT format('GRANT CONNECT ON DATABASE %I TO trajectory_importer', current_database())
\gexec
REVOKE ALL ON SCHEMA public FROM trajectory_importer;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM trajectory_importer;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM trajectory_importer;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM trajectory_importer;
GRANT USAGE ON SCHEMA public TO trajectory_importer;
GRANT SELECT, INSERT, UPDATE ON TABLE agent_trajectory_import_batches TO trajectory_importer;
GRANT SELECT, INSERT, UPDATE ON TABLE agent_trajectories TO trajectory_importer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent_trajectory_events TO trajectory_importer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trajectory_upload_device_codes TO trajectory_importer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trajectory_upload_sessions TO trajectory_importer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trajectory_upload_items TO trajectory_importer;
GRANT SELECT ON TABLE conversation_archive_plugin_migrations TO trajectory_importer;
GRANT USAGE, SELECT ON SEQUENCE agent_trajectories_id_seq TO trajectory_importer;
GRANT USAGE, SELECT ON SEQUENCE agent_trajectory_events_id_seq TO trajectory_importer;

REVOKE ALL ON SCHEMA public FROM conversation_archive_identity_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM conversation_archive_identity_owner;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM conversation_archive_identity_owner;
GRANT USAGE ON SCHEMA public TO conversation_archive_identity_owner;
GRANT SELECT (request_id, user_id, api_key_id, stream, created_at)
  ON TABLE usage_logs TO conversation_archive_identity_owner;
GRANT SELECT (id, email, role, status, deleted_at)
  ON TABLE users TO conversation_archive_identity_owner;
GRANT CREATE ON SCHEMA public TO conversation_archive_identity_owner;
ALTER FUNCTION resolve_archive_identity(UUID, TIMESTAMPTZ, TIMESTAMPTZ)
  OWNER TO conversation_archive_identity_owner;
ALTER FUNCTION resolve_archive_user_by_email(TEXT)
  OWNER TO conversation_archive_identity_owner;
REVOKE CREATE ON SCHEMA public FROM conversation_archive_identity_owner;
REVOKE ALL ON FUNCTION resolve_archive_identity(UUID, TIMESTAMPTZ, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_archive_identity(UUID, TIMESTAMPTZ, TIMESTAMPTZ)
  TO conversation_archiver;
REVOKE ALL ON FUNCTION resolve_archive_user_by_email(TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION resolve_archive_user_by_email(TEXT)
  FROM conversation_archiver;
GRANT EXECUTE ON FUNCTION resolve_archive_user_by_email(TEXT)
  TO trajectory_importer;

SET ROLE conversation_archiver;
SELECT COUNT(*)
FROM resolve_archive_identity(
  '00000000-0000-0000-0000-000000000000'::UUID,
  NOW(),
  NOW()
);
RESET ROLE;

SET ROLE trajectory_importer;
SELECT COUNT(*)
FROM resolve_archive_user_by_email('__archive_deploy_missing_user__@invalid.example');
RESET ROLE;
SQL

umask 077
archive_bind_host=$(getent ahostsv4 localhost | awk 'NR == 1 { print $1 }')
if [ -z "$archive_bind_host" ]; then
  echo "could not resolve an IPv4 loopback address for the archive listener" >&2
  exit 1
fi
if [ "$archive_env_created" -eq 1 ]; then
  printf 'ARCHIVE_BIND_HOST=%s\n' "$archive_bind_host" > "$archive_env"
  printf 'ARCHIVE_DATABASE_URL=postgresql://conversation_archiver:%s@postgres:5432/sub2api?sslmode=disable\n' \
    "$archive_password" >> "$archive_env"
  printf 'TRAJECTORY_IMPORT_DATABASE_URL=postgresql://trajectory_importer:%s@postgres:5432/sub2api?sslmode=disable\n' \
    "$importer_password" >> "$archive_env"
  printf 'ARCHIVE_PUBLIC_URL=%s\n' "$archive_public_url" >> "$archive_env"
else
  if grep -q '^ARCHIVE_BIND_HOST=' "$archive_env"; then
    sed -i "s/^ARCHIVE_BIND_HOST=.*/ARCHIVE_BIND_HOST=$archive_bind_host/" "$archive_env"
  else
    printf 'ARCHIVE_BIND_HOST=%s\n' "$archive_bind_host" >> "$archive_env"
  fi
  if ! grep -q '^TRAJECTORY_IMPORT_DATABASE_URL=' "$archive_env"; then
    printf 'TRAJECTORY_IMPORT_DATABASE_URL=postgresql://trajectory_importer:%s@postgres:5432/sub2api?sslmode=disable\n' \
      "$importer_password" >> "$archive_env"
  fi
  if grep -q '^ARCHIVE_PUBLIC_URL=' "$archive_env"; then
    sed -i "s|^ARCHIVE_PUBLIC_URL=.*|ARCHIVE_PUBLIC_URL=$archive_public_url|" "$archive_env"
  else
    printf 'ARCHIVE_PUBLIC_URL=%s\n' "$archive_public_url" >> "$archive_env"
  fi
fi

if ! docker inspect "$privacy_container" >/dev/null 2>&1; then
  echo "privacy-filter container is missing: $privacy_container" >&2
  exit 1
fi
if ! docker exec "$privacy_container" wget -q -T 5 -O /dev/null \
  http://127.0.0.1:8088/health; then
  echo "privacy-filter health check failed" >&2
  docker logs --tail 100 "$privacy_container" >&2 || true
  exit 1
fi
privacy_probe=$(docker exec "$privacy_container" wget -q -T 10 -O - \
  --header 'Content-Type: application/json' \
  --post-data '{"texts":["archive-plugin-health-probe"]}' \
  http://127.0.0.1:8088/redact/batch)
if ! printf '%s\n' "$privacy_probe" | grep -q '"redacted":"archive-plugin-health-probe"'; then
  echo "privacy-filter /redact/batch contract check failed" >&2
  exit 1
fi

docker rm -f sub2api-conversation-archive-canary >/dev/null 2>&1 || true
docker run -d \
  --name sub2api-conversation-archive-canary \
  --network "$network" \
  -p 127.0.0.1:18081:8081 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -e LISTEN_ADDR=:8081 \
  -e UPSTREAM_URL=http://sub2api:8080 \
  -e PRIVACY_FILTER_URL=http://privacy-filter:8088 \
  -e PRIVACY_FILTER_TIMEOUT=10s \
  -e PRIVACY_MAX_BODY_BYTES=268435456 \
  -e DATABASE_URL="postgresql://conversation_archiver:$archive_password@postgres:5432/sub2api?sslmode=disable" \
  -e TRAJECTORY_UPLOAD_DATABASE_URL="postgresql://trajectory_importer:$importer_password@postgres:5432/sub2api?sslmode=disable" \
  -e ARCHIVE_PUBLIC_URL="$archive_public_url" \
  -e DATABASE_TIMEOUT=5s \
  -e IDENTITY_RESOLVE_TIMEOUT=30s \
  -e IDENTITY_RESOLVE_INTERVAL=1s \
  -e IDENTITY_RESOLVE_BATCH_SIZE=100 \
  -e ADMIN_AUTH_TIMEOUT=5s \
  -e UPLOAD_DEVICE_TTL=10m \
  -e UPLOAD_SESSION_TTL=2h \
  -e UPLOAD_POLL_INTERVAL=3s \
  -e UPLOAD_MAX_BYTES=2147483648 \
  -e UPLOAD_MAX_RECORD_BYTES=16777216 \
  -e RETENTION_DAYS=30 \
  -e SWEEP_INTERVAL=1h \
  -e SWEEP_BATCH_SIZE=5000 \
  sub2api-conversation-archive:4 >/dev/null

attempt=0
until canary_health=$(wget -q -T 5 -O - http://127.0.0.1:18081/_archive/health) &&
  printf '%s\n' "$canary_health" | grep -q '"plugin_version":"external-v3"'; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker logs sub2api-conversation-archive-canary >&2 || true
    echo "archive plugin canary did not become healthy" >&2
    exit 1
  fi
  sleep 1
done

probe_sub2api_client_request_id "$sub2api_container"

if grep -q 'image:[[:space:]]*sub2api-custom:' "$main_compose"; then
  sed -i -E '0,/image:[[:space:]]*sub2api-custom:[^[:space:]]+/s//image: weishaw\/sub2api:latest/' "$main_compose"
  official_switched=1
  docker compose -f "$main_compose" pull sub2api
  docker compose -f "$main_compose" up -d --no-deps sub2api
fi

attempt=0
until docker exec "$sub2api_container" wget -q -T 5 -O /dev/null http://127.0.0.1:8080/health; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "official Sub2API container did not become healthy" >&2
    exit 1
  fi
  sleep 2
done

probe_sub2api_client_request_id "$sub2api_container"

cp "$release_dir/deploy/docker-compose.production.yml" "$archive_compose"
cp "$release_dir/deploy/import-trajectories.sh" "$archive_dir/import-trajectories.sh"
chmod 700 "$archive_dir/import-trajectories.sh"
mkdir -p /opt/sub2api/archive-import/incoming
chmod 700 /opt/sub2api/archive-import /opt/sub2api/archive-import/incoming
docker compose --env-file "$archive_env" -f "$archive_compose" up -d

attempt=0
until production_health=$(wget -q -T 5 -O - http://127.0.0.1:8081/_archive/health) &&
  printf '%s\n' "$production_health" | grep -q '"plugin_version":"external-v3"'; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker logs sub2api-conversation-archive >&2 || true
    echo "production archive plugin did not become healthy" >&2
    exit 1
  fi
  sleep 1
done

if [ -d /etc/cron.hourly ]; then
  cat > /etc/cron.hourly/sub2api-archive-import-cleanup <<EOF
#!/bin/sh
exec "$archive_dir/import-trajectories.sh" cleanup
EOF
  chmod 700 /etc/cron.hourly/sub2api-archive-import-cleanup
fi

docker rm -f sub2api-conversation-archive-canary >/dev/null
deployment_ok=1
trap - EXIT INT TERM
echo "external conversation archive plugin deployed"
