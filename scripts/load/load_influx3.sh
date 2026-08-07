#!/bin/bash

# Ensure loader is available
EXE_FILE_NAME=${EXE_FILE_NAME:-$(which tsbs_load_influx3)}
if [[ -z "$EXE_FILE_NAME" ]]; then
    echo "tsbs_load_influx3 not available. It is not specified explicitly and not found in \$PATH"
    exit 1
fi

DATA_FILE_NAME=${DATA_FILE_NAME:-influx3-data.gz}
DATABASE_PORT=${DATABASE_PORT:-8181}
INFLUXDB3_URL=${INFLUXDB3_URL:-http://${DATABASE_HOST:-localhost}:${DATABASE_PORT}}
INFLUXDB3_AUTH_TOKEN=${INFLUXDB3_AUTH_TOKEN:-}
INFLUXDB3_ADMIN_TOKEN=${INFLUXDB3_ADMIN_TOKEN:-${INFLUXDB3_AUTH_TOKEN}}
INFLUXDB3_ACCEPT_PARTIAL=${INFLUXDB3_ACCEPT_PARTIAL:-false}
INFLUXDB3_NO_SYNC=${INFLUXDB3_NO_SYNC:-false}

EXE_DIR=${EXE_DIR:-$(dirname "$0")}
source "${EXE_DIR}/load_common.sh"
set +x

gzip -dc "${DATA_FILE}" | "$EXE_FILE_NAME" \
    --db-name="${DATABASE_NAME}" \
    --do-create-db="${DO_CREATE_DB}" \
    --backoff="${BACKOFF_SECS}" \
    --workers="${NUM_WORKERS}" \
    --batch-size="${BATCH_SIZE}" \
    --reporting-period="${REPORTING_PERIOD}" \
    --urls="${INFLUXDB3_URL}" \
    --auth-token="${INFLUXDB3_AUTH_TOKEN}" \
    --admin-token="${INFLUXDB3_ADMIN_TOKEN}" \
    --accept-partial="${INFLUXDB3_ACCEPT_PARTIAL}" \
    --no-sync="${INFLUXDB3_NO_SYNC}"
