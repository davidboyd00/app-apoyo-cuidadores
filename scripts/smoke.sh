#!/usr/bin/env bash
# Smoke test post-deploy. Verifica que la API está viva y que la
# seguridad visible desde afuera funciona.
#
# Uso:
#   ./scripts/smoke.sh https://caregivers-backend.onrender.com
#
# Sale con exit 0 si todo verde, 1 si algo falla.

set -euo pipefail

BASE="${1:-}"
if [[ -z "$BASE" ]]; then
  echo "Uso: $0 <BASE_URL>" >&2
  echo "Ejemplo: $0 https://caregivers-backend.onrender.com" >&2
  exit 2
fi

ok=0
fail=0

check() {
  local nombre="$1"; shift
  local esperado="$1"; shift
  local url="$1"; shift
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" "$@" "$url")
  if [[ "$status" == "$esperado" ]]; then
    echo "  OK   [$status] $nombre"
    ok=$((ok+1))
  else
    echo "  FAIL [$status esperado $esperado] $nombre  ($url)"
    fail=$((fail+1))
  fi
}

echo "== Smoke test contra: $BASE =="

# 1) Liveness
check "/health responde 200"        200 "$BASE/health"

# 2) OpenAPI vivo
check "/docs responde 200"          200 "$BASE/docs"
check "/openapi.json responde 200"  200 "$BASE/openapi.json"

# 3) Seguridad visible: endpoint protegido sin token → 401
check "/patients sin token → 401"   401 "$BASE/patients"

# 4) Seguridad visible: endpoint protegido con token basura → 401
check "/patients con token basura → 401" 401 "$BASE/patients" \
  -H "Authorization: Bearer aaa.bbb.ccc"

# 5) Endpoint centers también requiere token (por get_db); exento solo de
#    require_consent, no de auth. Debería dar 401 sin auth.
check "/centers sin token → 401"    401 "$BASE/centers"

echo
echo "== $ok pasaron, $fail fallaron =="
if (( fail > 0 )); then exit 1; fi
