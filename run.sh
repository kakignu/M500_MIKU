#!/bin/bash
# HiBy M500 Monitor 起動スクリプト
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/.venv/bin/activate"
python "${SCRIPT_DIR}/hiby_monitor.py"
