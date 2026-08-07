#!/bin/bash

EXE_FILE_NAME=${EXE_FILE_NAME:-$(which tsbs_run_queries_influx3)}
if [[ -z "$EXE_FILE_NAME" ]]; then
    echo "tsbs_run_queries_influx3 not available. It is not specified explicitly and not found in \$PATH"
    exit 1
fi

BULK_DATA_DIR=${BULK_DATA_DIR:-/tmp/bulk_queries}
MAX_QUERIES=${MAX_QUERIES:-0}
NUM_WORKERS=${NUM_WORKERS:-$(grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 4)}
DATABASE_NAME=${DATABASE_NAME:-benchmark}
INFLUXDB3_URL=${INFLUXDB3_URL:-http://localhost:8181}
INFLUXDB3_AUTH_TOKEN=${INFLUXDB3_AUTH_TOKEN:-}

run_file() {
    local full_data_file_name=$1
    local data_file_name
    local no_ext_data_file_name
    local out_full_file_name
    data_file_name=$(basename -- "${full_data_file_name}")
    no_ext_data_file_name="${data_file_name%.*}"
    out_full_file_name="$(dirname "${full_data_file_name}")/result_${no_ext_data_file_name}.out"

    echo "Running ${data_file_name}"
    if [[ "${data_file_name##*.}" == "gz" ]]; then
        gzip -dc "${full_data_file_name}"
    else
        command cat "${full_data_file_name}"
    fi | "$EXE_FILE_NAME" \
        --max-queries="${MAX_QUERIES}" \
        --workers="${NUM_WORKERS}" \
        --db-name="${DATABASE_NAME}" \
        --urls="${INFLUXDB3_URL}" \
        --auth-token="${INFLUXDB3_AUTH_TOKEN}" | tee "${out_full_file_name}"
}

if [[ "$#" -gt 0 ]]; then
    for full_data_file_name in "$@"; do
        run_file "${full_data_file_name}"
    done
else
    for full_data_file_name in "${BULK_DATA_DIR}"/queries_influx3*; do
        [[ -e "${full_data_file_name}" ]] || continue
        run_file "${full_data_file_name}"
    done
fi
