#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs update — обновить PCS на хосте из репозитория (то же, что install.sh).
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"

pcs_begin update
if [[ -d "$PCS_ROOT/.git" ]]; then
    step "PCS запущен из git-клона — git pull"
    git -C "$PCS_ROOT" pull --ff-only 2>&1 | relay
    exit "${PIPESTATUS[0]}"
fi
exec bash "$PCS_ROOT/install.sh"
