#!/bin/sh
set -eu

archive_dir=${ARCHIVE_DIR:-/opt/sub2api/archive-proxy}
archive_env=$archive_dir/.env
incoming_dir=${ARCHIVE_IMPORT_DIR:-/opt/sub2api/archive-import/incoming}
network=${SUB2API_NETWORK:-sub2api_sub2api-network}
image=${ARCHIVE_IMAGE:-sub2api-conversation-archive:4}

test -f "$archive_env"
. "$archive_env"
test -n "${TRAJECTORY_IMPORT_DATABASE_URL:-}"
mkdir -p "$incoming_dir"
chmod 700 "$incoming_dir"
find "$incoming_dir" -maxdepth 1 -type f -name '*.ndjson.gz*' -mmin +1440 -delete

run_importer() {
  docker run --rm \
    --network "$network" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    -e DATABASE_URL="$TRAJECTORY_IMPORT_DATABASE_URL" \
    -e PRIVACY_FILTER_URL=http://privacy-filter:8088 \
    -e PRIVACY_FILTER_TIMEOUT=30s \
    -e RETENTION_DAYS=30 \
    "$@"
}

case ${1:-} in
  cleanup)
    exit 0
    ;;
  resolve-user)
    shift
    run_importer "$image" resolve-user "$@"
    ;;
  import)
    shift
    email=
    file=
    while [ "$#" -gt 0 ]; do
      case $1 in
        --email) email=${2:-}; shift 2 ;;
        --file) file=${2:-}; shift 2 ;;
        *) echo "unknown import argument: $1" >&2; exit 2 ;;
      esac
    done
    test -n "$email"
    test -n "$file"
    resolved=$(readlink -f "$file")
    case "$resolved" in
      "$incoming_dir"/*.ndjson.gz) ;;
      *) echo "import file is outside the controlled incoming directory" >&2; exit 2 ;;
    esac
    chown 65532:65532 "$resolved"
    chmod 600 "$resolved"
    if run_importer -v "$resolved:/import.ndjson.gz:ro" "$image" \
      import-trajectories --email "$email" --file "/import.ndjson.gz"; then
      rm -f -- "$resolved"
      exit 0
    fi
    exit 1
    ;;
  *)
    echo "usage: $0 resolve-user --email EMAIL | import --email EMAIL --file PACKAGE" >&2
    exit 2
    ;;
esac
