#!/usr/bin/env bash
set -euo pipefail

PREDICTIONS_PATH="${1:?Usage: $0 <predictions.jsonl> [run_id] [output_dir]}"
RUN_ID="${2:-mini_cc_eval}"
OUTPUT_DIR="${3:-results/swebench}"

echo "Running swebench harness..."
echo "  Predictions: ${PREDICTIONS_PATH}"
echo "  Run ID:      ${RUN_ID}"
echo "  Dataset:     princeton-nlp/SWE-bench_Verified"
echo

python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path "${PREDICTIONS_PATH}" \
    --max_workers 4 \
    --run_id "${RUN_ID}"

HARNESS_JSON=$(find . -maxdepth 1 -name "*.${RUN_ID}.json" | head -1)
if [[ -n "${HARNESS_JSON}" ]]; then
    TARGET="${OUTPUT_DIR}/$(basename "${HARNESS_JSON}")"
    mkdir -p "${OUTPUT_DIR}"
    mv "${HARNESS_JSON}" "${TARGET}"
    echo
    echo "Harness report moved to: ${TARGET}"
    echo
    echo "To merge with trajectory:"
    echo "  uv run python scripts/eval/merge_report.py \\"
    echo "    --trajectory ${OUTPUT_DIR}/trajectory.json \\"
    echo "    --harness ${TARGET} \\"
    echo "    --output ${OUTPUT_DIR}/final_report.json"
else
    echo
    echo "[warn] Harness report JSON not found in project root."
fi
