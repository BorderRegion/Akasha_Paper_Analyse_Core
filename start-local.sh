#!/usr/bin/env bash
set -euo pipefail
release_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$release_root/akasha_core"
if [[ ! -f .env ]]; then
  echo '请先复制 akasha_core/.env.example 为 .env 并填写配置。' >&2
  exit 1
fi
set -a
source .env
set +a
if [[ -z "${API_TOKEN:-}" || "${API_TOKEN}" == 'change-me-local-token' ]]; then
  echo '请在 .env 中设置自己的 API_TOKEN；网页登录使用这个值。' >&2
  exit 1
fi
export PAPERINTEL_WEB_DIST="${PAPERINTEL_WEB_DIST:-$release_root/akasha_core_front/apps/web/dist}"
export PAPERINTEL_PROVIDERS_FILE="${PAPERINTEL_PROVIDERS_FILE:-./config/providers.yaml}"
if [[ ! -f "$PAPERINTEL_PROVIDERS_FILE" ]]; then
  echo '缺少 provider 配置，请参照 README_ZH.md 创建。' >&2
  exit 1
fi
case "${1:-api}" in
  api) exec .venv/bin/python -m uvicorn paperintel.api.app:create_app --factory --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8420}" ;;
  worker) exec .venv/bin/celery -A paperintel.workflow.celery_app worker --loglevel=INFO ;;
  beat) exec .venv/bin/celery -A paperintel.workflow.celery_app beat --loglevel=INFO ;;
  migrate) exec .venv/bin/alembic upgrade head ;;
  doctor) exec .venv/bin/paperctl doctor ;;
  *) echo '用法: bash start-local.sh {api|worker|beat|migrate|doctor}' >&2; exit 2 ;;
esac
