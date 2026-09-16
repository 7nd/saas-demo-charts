#!/usr/bin/env bash
# Симметрично onboard-client.sh: сносит K8s-namespace целиком (Certificate/
# Secret/HelmRelease/RBAC — всё в одном namespace, один kubectl delete ns),
# GitRepository в flux-system, Forgejo-аккаунт+репозиторий клиента, Nexus-
# пользователя. Ничего из канонического (${CANONICAL_OWNER}/${CANONICAL_REPO})
# не трогает.
#
# Требуемые переменные окружения — те же, что у onboard-client.sh
# (FORGEJO_ADMIN_TOKEN, NEXUS_ADMIN_USER, NEXUS_ADMIN_PASSWORD, KUBECONFIG).
#
# Использование:
#   FORGEJO_ADMIN_TOKEN=... NEXUS_ADMIN_USER=... NEXUS_ADMIN_PASSWORD=... \
#     ./offboard-client.sh acme
set -euo pipefail

BASE_DOMAIN="${BASE_DOMAIN:-hightps.online}"
FORGEJO_URL="${FORGEJO_URL:-https://git.${BASE_DOMAIN}}"
NEXUS_URL="${NEXUS_URL:-https://nexus.${BASE_DOMAIN}}"

SLUG="${1:?usage: offboard-client.sh <slug>}"
: "${FORGEJO_ADMIN_TOKEN:?export FORGEJO_ADMIN_TOKEN}"
: "${NEXUS_ADMIN_USER:?export NEXUS_ADMIN_USER}"
: "${NEXUS_ADMIN_PASSWORD:?export NEXUS_ADMIN_PASSWORD}"

CLIENT_USER="client-${SLUG}"
NAMESPACE="${SLUG}-saas"
GITREPO_NAME="client-${SLUG}"

echo "==> офбординг клиента: slug=${SLUG}"

echo "==> удаляю namespace ${NAMESPACE} (Certificate/Secret/HelmRelease/RBAC разом)"
kubectl delete namespace "${NAMESPACE}" --ignore-not-found --wait=false

echo "==> удаляю GitRepository ${GITREPO_NAME}"
kubectl delete gitrepository "${GITREPO_NAME}" -n flux-system --ignore-not-found

# Forgejo не даёт удалить пользователя, пока за ним числится репозиторий
# ("user still has ownership of repositories") — сносим репозиторий явно,
# отдельным вызовом, ПЕРЕД удалением аккаунта (не полагаться на каскад).
echo "==> удаляю репозиторий ${CLIENT_USER}/sqas-demo-chart"
curl -sf -X DELETE "${FORGEJO_URL}/api/v1/repos/${CLIENT_USER}/sqas-demo-chart" \
  -H "Authorization: token ${FORGEJO_ADMIN_TOKEN}" \
  || echo "!! Forgejo repo ${CLIENT_USER}/sqas-demo-chart — не найден или уже удалён"

echo "==> удаляю Forgejo-аккаунт ${CLIENT_USER}"
curl -sf -X DELETE "${FORGEJO_URL}/api/v1/admin/users/${CLIENT_USER}" \
  -H "Authorization: token ${FORGEJO_ADMIN_TOKEN}" \
  || echo "!! Forgejo user ${CLIENT_USER} — не найден или уже удалён"

echo "==> удаляю Nexus-пользователя ${CLIENT_USER}"
curl -sf -X DELETE "${NEXUS_URL}/service/rest/v1/security/users/${CLIENT_USER}" \
  -u "${NEXUS_ADMIN_USER}:${NEXUS_ADMIN_PASSWORD}" \
  || echo "!! Nexus user ${CLIENT_USER} — не найден или уже удалён"

echo "==> готово: ${SLUG} снесён"
