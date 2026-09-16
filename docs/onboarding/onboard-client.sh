#!/usr/bin/env bash
# Онбординг одного потенциального клиента: свой Forgejo-аккаунт с разовым
# импортом чарта для деплоя SaaS (копия живёт на этой же Forgejo,
# ${CANONICAL_OWNER}/${CANONICAL_REPO} — см. docs/onboarding/README.md,
# как она туда попадает и как обновляется), свой namespace, свой wildcard
# *.<slug>-saas.${BASE_DOMAIN}, свой стартовый HelmRelease (deploy-on-push
# из ЕГО ЖЕ репозитория), свой Nexus-логин (push+pull на docker-clients,
# см. README.md про то, почему это не жёсткая изоляция), свой K8s-токен с
# правом самому провизионить HelmRelease в своём namespace. Сам
# GitHub-репозиторий (7nd/saas-demo-charts, основная разработка чарта,
# включая эти скрипты) НЕ трогаем — только копия для раздачи клиентам
# живёт на Forgejo. Ничего из провижининга не проходит через
# unitum-demo-k8s-infra/git — сознательно развязано от корневого
# app-of-apps (см. план), прямой kubectl apply + Forgejo/Nexus REST API.
#
# Предполагает уже настроенное на кластере (unitum-demo-k8s-infra):
#   - отдельная Forgejo на git.${BASE_DOMAIN} (infrastructure/apps/git-stands)
#   - канонический чарт (копия) в ${CANONICAL_OWNER}/${CANONICAL_REPO} на этой Forgejo
#   - ClusterIssuer letsencrypt (для нового wildcard-сертификата клиента)
#   - Nexus с настроенным docker-репозиторием docker-clients + ролью
#     docker-clients-push (infrastructure/apps/nexus, см. README.md — как
#     заводится один раз)
#
# Требуемые переменные окружения:
#   FORGEJO_ADMIN_TOKEN  — API-токен админа git.${BASE_DOMAIN} (создать
#                          один раз: git.${BASE_DOMAIN} -> Settings ->
#                          Applications -> Generate New Token, scope write:admin)
#   NEXUS_ADMIN_USER, NEXUS_ADMIN_PASSWORD — админ Nexus (nexus.${BASE_DOMAIN})
#   KUBECONFIG           — доступ к кластеру с правом создавать
#                          Namespace/Certificate/GitRepository/HelmRelease/RBAC/Secret
#
# Использование:
#   FORGEJO_ADMIN_TOKEN=... NEXUS_ADMIN_USER=... NEXUS_ADMIN_PASSWORD=... \
#     ./onboard-client.sh acme
#
# ВНИМАНИЕ про bash 3.2 (дефолтный /bin/bash на macOS): командная
# подстановка "$(python3 ...)", если она НЕ единственное правая часть
# присваивания переменной (например — один из нескольких аргументов
# curl inline), в этой версии bash ломается непредсказуемо (проверено
# живьём: и многострочный, и однострочный python -c внутри `curl -d
# "$(...)"` рвёт JSON на части). Поэтому везде ниже — сначала
# ПЕРЕМЕННАЯ="$(python3 ...)" отдельной строкой, потом curl -d
# "$ПЕРЕМЕННАЯ" — не сворачивать обратно в одну строку.
set -euo pipefail

BASE_DOMAIN="${BASE_DOMAIN:-hightps.online}"
FORGEJO_URL="${FORGEJO_URL:-https://git.${BASE_DOMAIN}}"
FORGEJO_INTERNAL="${FORGEJO_INTERNAL:-http://forgejo-http.forgejo.svc.cluster.local:3000}"
NEXUS_URL="${NEXUS_URL:-https://nexus.${BASE_DOMAIN}}"
DOCKER_HOST="${DOCKER_HOST_OVERRIDE:-docker.${BASE_DOMAIN}}"
CANONICAL_OWNER="${CANONICAL_OWNER:-showcase}"
CANONICAL_REPO="${CANONICAL_REPO:-sqas-demo-chart}"
TOKEN_DURATION="${TOKEN_DURATION:-720h}" # 30 дней — перевыпустить: kubectl create token ...

SLUG="${1:?usage: onboard-client.sh <slug>}"
if [[ ! "$SLUG" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
  echo "slug должен быть DNS-safe: строчные латинские буквы/цифры/дефис, до 32 симв." >&2
  exit 1
fi

: "${FORGEJO_ADMIN_TOKEN:?export FORGEJO_ADMIN_TOKEN (admin API token на ${FORGEJO_URL})}"
: "${NEXUS_ADMIN_USER:?export NEXUS_ADMIN_USER}"
: "${NEXUS_ADMIN_PASSWORD:?export NEXUS_ADMIN_PASSWORD}"

CLIENT_USER="client-${SLUG}"
CLIENT_REPO="sqas-demo-chart"
NAMESPACE="${SLUG}-saas"
STAND_HOST="app.${SLUG}-saas.${BASE_DOMAIN}"
GITREPO_NAME="client-${SLUG}"

echo "==> онбординг клиента: slug=${SLUG} namespace=${NAMESPACE}"

# ── 1. Forgejo: аккаунт клиента ──────────────────────────────────────────
CLIENT_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
echo "==> создаю Forgejo-аккаунт ${CLIENT_USER}"
FORGEJO_USER_JSON="$(python3 -c "import json,sys; print(json.dumps({'username': sys.argv[1], 'email': sys.argv[2], 'password': sys.argv[3], 'must_change_password': False}))" \
  "${CLIENT_USER}" "${SLUG}@clients.${BASE_DOMAIN}" "${CLIENT_PASSWORD}")"
curl -sf -X POST "${FORGEJO_URL}/api/v1/admin/users" \
  -H "Authorization: token ${FORGEJO_ADMIN_TOKEN}" -H "Content-Type: application/json" \
  -d "${FORGEJO_USER_JSON}" >/dev/null

# ── 2. Пустой репозиторий под его аккаунтом ─────────────────────────────
echo "==> создаю ${CLIENT_USER}/${CLIENT_REPO}"
curl -sf -X POST "${FORGEJO_URL}/api/v1/admin/users/${CLIENT_USER}/repos" \
  -H "Authorization: token ${FORGEJO_ADMIN_TOKEN}" -H "Content-Type: application/json" \
  -d "{\"name\":\"${CLIENT_REPO}\",\"private\":false,\"auto_init\":false}" >/dev/null

# ── 3. Разовый импорт (mirror push, НЕ живой fork/sync) ─────────────────
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
echo "==> импортирую ${CANONICAL_OWNER}/${CANONICAL_REPO} -> ${CLIENT_USER}/${CLIENT_REPO}"
git clone --mirror --quiet "${FORGEJO_URL}/${CANONICAL_OWNER}/${CANONICAL_REPO}.git" "${WORKDIR}/repo.git"
FORGEJO_HOST="${FORGEJO_URL#https://}"
git -C "${WORKDIR}/repo.git" push --mirror --quiet \
  "https://${CLIENT_USER}:${CLIENT_PASSWORD}@${FORGEJO_HOST}/${CLIENT_USER}/${CLIENT_REPO}.git"

# ── 4. Nexus: пользователь клиента ───────────────────────────────────────
# Роль docker-clients-push — ОДНА общая на всех клиентов (заведена один раз
# заранее, не здесь): push+pull на весь репозиторий docker-clients целиком.
# Content Selectors (per-client scoping по префиксу пути) проверены живьём
# и НЕ работают для docker-формата на первом push нового имени образа —
# известное ограничение Nexus (координата компонента ещё не существует,
# selector не может её матчить). Изоляция между клиентами здесь —
# СОГЛАШЕНИЕ об именовании (каждый пушит под своим ${SLUG}/), не жёсткая
# техническая граница: любой клиент технически может прочитать/перезаписать
# образ другого. Приемлемо для демо/trial, не для продакшена — см. README.md.
echo "==> завожу Nexus-пользователя ${CLIENT_USER}"
NEXUS_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
NEXUS_USER_JSON="$(python3 -c "import json,sys; print(json.dumps({'userId': sys.argv[1], 'firstName': sys.argv[2], 'lastName': 'client', 'emailAddress': sys.argv[3], 'password': sys.argv[4], 'status': 'active', 'source': 'default', 'roles': ['docker-clients-push']}))" \
  "${CLIENT_USER}" "${SLUG}" "${SLUG}@clients.${BASE_DOMAIN}" "${NEXUS_PASSWORD}")"
curl -sf -u "${NEXUS_ADMIN_USER}:${NEXUS_ADMIN_PASSWORD}" -X POST \
  "${NEXUS_URL}/service/rest/v1/security/users" -H "Content-Type: application/json" \
  -d "${NEXUS_USER_JSON}" >/dev/null \
  || echo "!! Nexus user creation failed — роль docker-clients-push должна существовать заранее, см. docs/onboarding/README.md"

# ── 5. K8s: namespace + wildcard TLS + registry pull-secret + GitRepository
#         + стартовый HelmRelease + self-service RBAC ───────────────────
echo "==> провижиню namespace ${NAMESPACE} и Flux-объекты"
DOCKERCFG_JSON="$(python3 -c "import json,sys; print(json.dumps({'auths': {sys.argv[1]: {'username': sys.argv[2], 'password': sys.argv[3]}}}))" \
  "${DOCKER_HOST}" "${CLIENT_USER}" "${NEXUS_PASSWORD}")"
kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: wildcard-saas
  namespace: ${NAMESPACE}
spec:
  secretName: wildcard-saas-tls
  issuerRef:
    name: letsencrypt
    kind: ClusterIssuer
  dnsNames:
    - "*.${SLUG}-saas.${BASE_DOMAIN}"
    - "${SLUG}-saas.${BASE_DOMAIN}"
---
apiVersion: v1
kind: Secret
metadata:
  name: registry-pull-secret
  namespace: ${NAMESPACE}
type: kubernetes.io/dockerconfigjson
stringData:
  .dockerconfigjson: |
    ${DOCKERCFG_JSON}
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: ${GITREPO_NAME}
  namespace: flux-system
spec:
  interval: 5m
  url: ${FORGEJO_INTERNAL}/${CLIENT_USER}/${CLIENT_REPO}.git
  ref:
    branch: main
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: app
  namespace: ${NAMESPACE}
spec:
  interval: 5m
  chart:
    spec:
      chart: charts/deployment-demo
      sourceRef:
        kind: GitRepository
        name: ${GITREPO_NAME}
        namespace: flux-system
      reconcileStrategy: Revision
  install:
    remediation:
      retries: 3
  upgrade:
    remediation:
      retries: 3
  values:
    ingress:
      host: ${STAND_HOST}
      tls:
        enabled: true
        secretName: wildcard-saas-tls
    env:
      plan: trial
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: saas-provisioner
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: saas-provisioner
  namespace: ${NAMESPACE}
rules:
  - apiGroups: ["helm.toolkit.fluxcd.io"]
    resources: ["helmreleases"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: saas-provisioner
  namespace: ${NAMESPACE}
subjects:
  - kind: ServiceAccount
    name: saas-provisioner
    namespace: ${NAMESPACE}
roleRef:
  kind: Role
  name: saas-provisioner
  apiGroup: rbac.authorization.k8s.io
EOF

# ── 6. Выдать на руки ────────────────────────────────────────────────────
K8S_API_SERVER="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
K8S_TOKEN="$(kubectl create token saas-provisioner -n "${NAMESPACE}" --duration="${TOKEN_DURATION}")"

cat <<SUMMARY

================================================================
Клиент "${SLUG}" готов.

Живой стенд:      https://${STAND_HOST}/
Forgejo:           ${FORGEJO_URL}/${CLIENT_USER}/${CLIENT_REPO}
  логин:           ${CLIENT_USER}
  пароль:          ${CLIENT_PASSWORD}

Docker registry:   ${DOCKER_HOST}
  логин:           ${CLIENT_USER}
  пароль:          ${NEXUS_PASSWORD}
  push:            docker push ${DOCKER_HOST}/${SLUG}/<image>:<tag>
  (по договорённости об именовании — пушь строго под своим "${SLUG}/",
   push доступен на весь репозиторий, не изолирован технически, см. README.md)
  imagePullSecret "registry-pull-secret" уже лежит в ${NAMESPACE} — укажи
  values.imagePullSecrets: [{name: registry-pull-secret}] в своём HelmRelease,
  если решишь использовать свой образ вместо стокового nginx:alpine.

Kubernetes self-service (namespace ${NAMESPACE}):
  K8S_API_SERVER=${K8S_API_SERVER}
  K8S_TOKEN=${K8S_TOKEN}
  (токен на ${TOKEN_DURATION}, перевыпуск: kubectl create token saas-provisioner -n ${NAMESPACE} --duration=...)

Дальше клиент может:
  - пушить в свой Forgejo-репозиторий -> стенд обновится сам (см. helmrelease-cheatsheet.md)
  - создавать свои HelmRelease в ${NAMESPACE} через K8S_TOKEN (examples/manage_tenant.py,
    поменять NAMESPACE="${NAMESPACE}" и BASE_DOMAIN="${SLUG}-saas.${BASE_DOMAIN}")
================================================================
SUMMARY
