# Онбординг клиента — скрипты

`onboard-client.sh <slug>` / `offboard-client.sh <slug>` — весь процесс из
плана (Forgejo-аккаунт + разовый импорт чарта, свой namespace и wildcard
`*.<slug>-saas.hightps.online`, свой Nexus-логин, свой K8s self-service
токен), одной командой. Оба прогнаны end-to-end на реальном стенде
(`slug=test1`) — deploy-on-push, RBAC-изоляция и docker push/pull
подтверждены живыми запросами, не только по коду.

## Предварительно (один раз, не на каждого клиента)

Всё это уже настроено на демо-стенде — здесь для справки/на случай
переноса на другой кластер.

### Forgejo (`git.${BASE_DOMAIN}`)

Отдельный инстанс (`infrastructure/apps/git-stands` в
`unitum-demo-k8s-infra`) — **не** тот же, что у agents-стенда. Канонический
чарт живёт там как `showcase/sqas-demo-chart` — разовый mirror-импорт с
GitHub (не живая синхронизация, GitHub остаётся основным репозиторием
разработки):

```sh
git clone --mirror https://github.com/7nd/saas-demo-charts /tmp/canon.git
git -C /tmp/canon.git push --mirror \
  "https://<forgejo-admin>:<pass>@git.${BASE_DOMAIN}/showcase/sqas-demo-chart.git"
```

(Организация `showcase` и репозиторий заводятся один раз через Forgejo API
— `POST /api/v1/orgs`, `POST /api/v1/orgs/showcase/repos`.)

API-токен админа для `onboard-client.sh` — `git.${BASE_DOMAIN}` → Settings
→ Applications → Generate New Token (или через API: `POST
/api/v1/users/<admin>/tokens`), нужны scopes `write:admin`,
`write:repository`, `write:user`, `write:organization`.

### Nexus (`nexus.${BASE_DOMAIN}` / docker на `docker.${BASE_DOMAIN}:5000`)

Docker registry API включается values-ключом
`nexus.docker.registries[]` в HelmRelease (уже в
`infrastructure/apps/nexus/release.yaml`) — чарт сам заводит Service+Ingress
под отдельный порт. Сам docker-репозиторий внутри Nexus чарт не создаёт —
разово через REST API (админ-креды — `nexus_admin_password` в SOPS):

```sh
AUTH="admin:<пароль>"
BASE="https://nexus.${BASE_DOMAIN}/service/rest/v1"

# Docker Bearer Token Realm — не обязателен при forceBasicAuth:true (ниже),
# но не мешает, включаем для порядка:
curl -u "$AUTH" -X PUT "$BASE/security/realms/active" \
  -H "Content-Type: application/json" -d '["NexusAuthenticatingRealm","DockerToken"]'

curl -u "$AUTH" -X POST "$BASE/repositories/docker/hosted" -H "Content-Type: application/json" -d '{
  "name": "docker-clients", "online": true,
  "storage": {"blobStoreName": "default", "strictContentTypeValidation": true, "writePolicy": "ALLOW"},
  "docker": {"v1Enabled": false, "forceBasicAuth": true, "httpPort": 5000}
}'

# ОДНА общая роль на всех клиентов — см. "Про изоляцию push" ниже, почему
# не per-client Content Selector:
curl -u "$AUTH" -X POST "$BASE/security/roles" -H "Content-Type: application/json" -d '{
  "id": "docker-clients-push", "name": "docker-clients-push",
  "privileges": [
    "nx-repository-view-docker-docker-clients-browse",
    "nx-repository-view-docker-docker-clients-read",
    "nx-repository-view-docker-docker-clients-add",
    "nx-repository-view-docker-docker-clients-edit"
  ],
  "roles": []
}'
```

**Важно:** admin-пароль Nexus генерируется случайно при первом старте
(`/nexus-data/admin.password` в поде) и **нигде не сохраняется чартом** —
забрать его сразу после первого деплоя (`kubectl exec ... cat
/nexus-data/admin.password`) и сохранить в SOPS (`nexus_admin_password`,
как остальные секреты в `cluster-secrets.yaml`). Если пароль потерян —
единственный способ восстановить доступ — остановить под и удалить
`/nexus-data/db/security` (сбрасывает ВСЮ security-конфигурацию Nexus,
включая роли/пользователей — их придётся завести заново).

### Про изоляцию push в Nexus

Изначально план предполагал Content Selectors — ограничить каждого
клиента push+pull только на свой префикс `client-<slug>/*` внутри одного
`docker-clients` репозитория, плюс общая pull-only роль на `shared/*`.
**Проверено живьём и не работает**: Content Selector в Nexus не может
матчить компонент, которого ещё не существует — первый `docker push`
нового имени образа падает `403`, потому что selector не в состоянии
сопоставить ещё-не-существующую координату. Это задокументированное
ограничение Nexus для Docker-формата (в отличие от Maven/npm, где
координата известна из самого пути аплоада).

Поэтому — **одна общая роль `docker-clients-push`** на весь репозиторий:
любой клиент может push+pull ЛЮБОЙ образ в `docker-clients`, не только
свой. Изоляция — соглашение об именовании (`docker.${BASE_DOMAIN}/<slug>/...`),
не техническая граница. Приемлемо для демо/trial. Если понадобится
жёсткая изоляция — единственный подтверждённо рабочий вариант в Nexus
это отдельный hosted-репозиторий на клиента (свой порт/Ingress/сертификат
на каждого — ощутимо дороже по инфраструктуре, требует правки
`infrastructure/apps/nexus/release.yaml` на каждого нового клиента, то
есть уже не чисто REST-API-шный онбординг).

## Онбординг/офбординг клиента

```sh
export FORGEJO_ADMIN_TOKEN=...
export NEXUS_ADMIN_USER=admin
export NEXUS_ADMIN_PASSWORD=...
export KUBECONFIG=...

./onboard-client.sh acme     # печатает URL стенда, Forgejo-креды, Nexus-креды, K8s-токен
./offboard-client.sh acme    # сносит всё это обратно
```

Оба скрипта читают `BASE_DOMAIN`/`FORGEJO_URL`/`NEXUS_URL`/
`CANONICAL_OWNER`/`CANONICAL_REPO` из окружения с разумными дефолтами
(`hightps.online`, `showcase/sqas-demo-chart`) — переопредели, если стенд
другой.

### Что именно делает `onboard-client.sh` (по шагам)

1. Forgejo-аккаунт `client-<slug>` (REST API, `must_change_password: false`).
2. Пустой репозиторий под этим аккаунтом.
3. **Разовый импорт** — `git clone --mirror` канонического
   `showcase/sqas-demo-chart` → `git push --mirror` в репозиторий клиента.
   Не живой fork/sync: дальше клиент сам решает, что делать со своей копией.
4. Nexus-пользователь клиента, роль `docker-clients-push`.
5. Kubernetes, всё одним `kubectl apply -f -` (без git-коммита в
   `unitum-demo-k8s-infra` — см. план, сознательно развязано от корневого
   app-of-apps):
   - `Namespace <slug>-saas`
   - `Certificate` — свой wildcard `*.<slug>-saas.${BASE_DOMAIN}` через
     уже существующий `ClusterIssuer letsencrypt` (никакого исключения из
     external-dns для этого поддомена — в отличие от `demo.${BASE_DOMAIN}`,
     клиент должен открыть урл в реальном браузере, DNS-записи нужны
     по-настоящему)
   - `Secret/registry-pull-secret` (`dockerconfigjson`, креды клиента же
     из шага 4) — на случай если решит использовать свой образ
   - `GitRepository client-<slug>` (в `flux-system`) → личный репозиторий
     клиента на Forgejo
   - `HelmRelease app` — `chart.spec.sourceRef` смотрит на ЭТОТ
     `GitRepository`, не на общий `saas-demo-charts` — отсюда
     deploy-on-push: клиент пушит в свой репозиторий, Flux сам подхватывает
     (`reconcileStrategy: Revision`)
   - `ServiceAccount`/`Role`/`RoleBinding saas-provisioner` — полный CRUD
     на `helmreleases`, но **только в своём namespace** (проверено
     `kubectl auth can-i` с реальным токеном на чужой namespace — `no`)
6. K8s-токен (`kubectl create token`, 30 дней) + всё вышеперечисленное —
   единым блоком в stdout.

### Известные грабли

- **bash 3.2 (дефолтный `/bin/bash` на macOS).** Командная подстановка
  `$(python3 -c ...)`, если это не единственная правая часть присваивания
  переменной (например, инлайн внутри `curl -d "$(...)"`), в этой версии
  bash ломает JSON на куски — проверено живьём, не гипотетически. Оба
  скрипта поэтому везде сначала `VAR="$(python3 ...)"` отдельной строкой,
  потом `curl -d "$VAR"` — не сворачивать обратно "для краткости".
- Forgejo не даёт удалить пользователя, пока за ним есть репозиторий
  (`user still has ownership of repositories`, `422`) — `offboard-client.sh`
  явно удаляет репозиторий отдельным вызовом ДО удаления аккаунта.
