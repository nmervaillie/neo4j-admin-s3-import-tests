#!/usr/bin/env bash
# Run neo4j-admin database import from S3 or local scratch, then upload the
# resulting database artifact back to S3 (mandatory, size-verified).
set -euo pipefail

BUCKET="" MODE="" DB="neo4j" DRY_RUN=0 KEEP_SCRATCH=0
JAVA_HEAP="${JAVA_HEAP:-8g}"
NEO4J_HOME="${NEO4J_HOME:-/opt/neo4j}"
NEO4J_DATA_DIR="${NEO4J_DATA_DIR:-/scratch/neo4j-data}"
SCRATCH="${SCRATCH:-/scratch}"
ZSTD_LEVEL="${ZSTD_LEVEL:-3}"
SOURCE_NODES="nodes" SOURCE_RELS="relationships"

usage() {
  cat <<EOF
Usage: $(basename "$0") --bucket BUCKET --mode s3|local [--db NAME]
                      [--dry-run] [--keep-scratch]

Environment: JAVA_HEAP (default 8g), NEO4J_HOME (default /opt/neo4j),
             NEO4J_DATA_DIR (default /scratch/neo4j-data),
             SCRATCH (default /scratch), ZSTD_LEVEL (default 3)
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket) BUCKET="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --keep-scratch) KEEP_SCRATCH=1; shift ;;
    *) usage ;;
  esac
done
[[ -n "$BUCKET" && ( "$MODE" == "s3" || "$MODE" == "local" ) ]] || usage

S3_SOURCE="s3://$BUCKET/source"
DB_DIR="$NEO4J_DATA_DIR/databases/$DB"
TX_DIR="$NEO4J_DATA_DIR/transactions/$DB"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_NAME="${DB}-${TIMESTAMP}.tar.zst"
ARTIFACT="$SCRATCH/databases/$ARTIFACT_NAME"
S3_DB_DEST="s3://$BUCKET/databases/$ARTIFACT_NAME"
S3_TIMINGS_DEST="s3://$BUCKET/timings/${DB}-${TIMESTAMP}.json"
fail() { echo "ERROR: $*" >&2; exit 1; }

# --- source file listing ----------------------------------------------------
s3_file_list() { # $1 = s3 dir; prints full s3:// URLs of contained files
  aws s3 ls --recursive "$1" | awk -v prefix="s3://$BUCKET/" '{print prefix $4}'
}
local_file_list() { # $1 = local dir
  find "$1" -type f | sort
}

SYNC_SECONDS=0
if [[ "$MODE" == "s3" ]]; then
  NODE_LABELS=$(aws s3 ls "$S3_SOURCE/$SOURCE_NODES/" | awk '/PRE /{print $2}' | tr -d '/' | tr '\n' ' ')
  REL_TYPES=$(aws s3 ls "$S3_SOURCE/$SOURCE_RELS/" | awk '/PRE /{print $2}' | tr -d '/' | tr '\n' ' ')
else
  mkdir -p "$SCRATCH/source"
  T_SYNC0=$(date +%s)
  aws s3 sync --delete "$S3_SOURCE/" "$SCRATCH/source/"
  T_SYNC1=$(date +%s)
  SYNC_SECONDS=$((T_SYNC1 - T_SYNC0))
  NODE_LABELS=$(cd "$SCRATCH/source/$SOURCE_NODES" 2>/dev/null && ls -1 || true)
  REL_TYPES=$(cd "$SCRATCH/source/$SOURCE_RELS" 2>/dev/null && ls -1 || true)
fi

[[ -n "$NODE_LABELS" ]] || fail "no source node files found under $S3_SOURCE/$SOURCE_NODES/ — run the generator first"

IMPORT_ARGS=(--verbose)
for label in $NODE_LABELS; do
  if [[ "$MODE" == "s3" ]]; then
    files=$(s3_file_list "$S3_SOURCE/$SOURCE_NODES/$label/")
  else
    files=$(local_file_list "$SCRATCH/source/$SOURCE_NODES/$label")
  fi
  [[ -n "$files" ]] || fail "no files for node label $label"
  IMPORT_ARGS+=(--nodes="$label=$(echo "$files" | paste -sd, -)")
done
for rtype in $REL_TYPES; do
  if [[ "$MODE" == "s3" ]]; then
    files=$(s3_file_list "$S3_SOURCE/$SOURCE_RELS/$rtype/")
  else
    files=$(local_file_list "$SCRATCH/source/$SOURCE_RELS/$rtype")
  fi
  [[ -n "$files" ]] || fail "no files for relationship type $rtype"
  IMPORT_ARGS+=(--relationships="$rtype=$(echo "$files" | paste -sd, -)")
done

CMD=(neo4j-admin database import full "$DB" --overwrite-destination "${IMPORT_ARGS[@]}")

if [[ "$DRY_RUN" == 1 ]]; then
  echo "DRY RUN resolved command:"
  echo "  ${CMD[*]}"
  echo "DRY RUN artifact destination: $S3_DB_DEST"
  exit 0
fi

# --- import -------------------------------------------------------------------
T_IMPORT0=$(date +%s)
HEAP_SIZE="$JAVA_HEAP" "${CMD[@]}" || fail "neo4j-admin import failed (exit $?); source kept at $SCRATCH/source for post-mortem"
T_IMPORT1=$(date +%s)
[[ -d "$DB_DIR" ]] || fail "import succeeded but database dir not found: $DB_DIR"

# --- artifact upload (mandatory) ----------------------------------------------
mkdir -p "$SCRATCH/databases"
T_PACK0=$(date +%s)
TAR_PATHS=("databases/$DB")
[[ -d "$TX_DIR" ]] && TAR_PATHS+=("transactions/$DB")
tar -C "$NEO4J_DATA_DIR" -cf - "${TAR_PATHS[@]}" | zstd -"$ZSTD_LEVEL" -o "$ARTIFACT" \
  || fail "artifact packing failed"
T_PACK1=$(date +%s)
aws s3 cp "$ARTIFACT" "$S3_DB_DEST" || fail "artifact upload failed"
LOCAL_SIZE=$(wc -c < "$ARTIFACT" | tr -d ' ')
REMOTE_SIZE=$(aws s3 ls "$S3_DB_DEST" | awk '{print $3}')
[[ -n "$REMOTE_SIZE" && "$REMOTE_SIZE" == "$LOCAL_SIZE" ]] \
  || fail "artifact upload size mismatch: local=$LOCAL_SIZE remote=${REMOTE_SIZE:-none}"

# --- timings ------------------------------------------------------------------
T_TOTAL=$(date +%s)
TIMINGS_FILE="$SCRATCH/databases/${DB}-${TIMESTAMP}.json"
cat > "$TIMINGS_FILE" <<EOF
{"db": "$DB", "mode": "$MODE", "bucket": "$BUCKET",
 "java_heap": "$JAVA_HEAP", "timestamp": "$TIMESTAMP",
 "sync_seconds": $SYNC_SECONDS,
 "import_seconds": $((T_IMPORT1 - T_IMPORT0)),
 "pack_seconds": $((T_PACK1 - T_PACK0)),
 "total_seconds": $((T_TOTAL - T_IMPORT0)),
 "artifact_bytes": $LOCAL_SIZE,
 "artifact": "$S3_DB_DEST"}
EOF
aws s3 cp "$TIMINGS_FILE" "$S3_TIMINGS_DEST" || fail "timings upload failed"

if [[ "$MODE" == "local" && "$KEEP_SCRATCH" != 1 ]]; then
  rm -rf "$SCRATCH/source"
fi

echo "OK: import complete, artifact at $S3_DB_DEST, timings at $S3_TIMINGS_DEST"
