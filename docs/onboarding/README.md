# Онбординг клиента — скрипты

`onboard_client.py <slug>` / `offboard_client.py <slug>` — весь процесс
одной командой: Forgejo-аккаунт клиента + разовый импорт чарта, живая
базовая инфра (namespace, wildcard TLS, docker-доступ, deploy-on-push,
self-service K8s-токен, персональный CI на сборку своего образа). Прогнаны
end-to-end на реальном стенде (`slug=test3`/`test4`, параллельно, для
честной проверки cross-client изоляции) — стенд, docker push/pull
(включая **реальную** cross-client изоляцию — не соглашение об
именовании, см. ниже), RBAC-изоляция подтверждены живыми запросами, не
только по коду.

Самый простой путь для клиента — вообще не трогать `docker`/`kubectl`
руками: правишь `02-build-your-image/index.html` в своём репозитории и
пушишь, CI (`.forgejo/workflows/build.yml`, свой раннер на каждого
клиента, без Docker/DinD — BuildKit) сама собирает образ, пушит в твой
же personal registry и обновляет стенд.

Базовая инфра клиента раскатывается **тем же способом, которым сами
показываем клиенту его SaaS**: не сырые манифесты в `kubectl apply -f -`,
а `HelmRelease` поверх Helm-чарта в git — `GitRepository client-infra-chart`
+ чарт `ops/client-infra` (приватный репозиторий на этой же Forgejo, НЕ
для клиентов). На каждого клиента онбординг создаёт один маленький
`HelmRelease client-infra-<slug>` в `flux-system` (см. "Что делает
`onboard_client.py`" ниже) — helm-controller на его основе раскатывает
всё остальное. Офбординг — один `kubectl delete helmrelease`, всё
остальное сносится каскадом.

## Предварительно (один раз, не на каждого клиента)

Всё это уже настроено на демо-стенде — здесь для справки/на случай
переноса на другой кластер.

### Forgejo (`git.${BASE_DOMAIN}`)

Отдельный инстанс (`infrastructure/apps/git-stands` в
`unitum-demo-k8s-infra`) — **не** тот же, что у agents-стенда. Закрыт для
неавторизованных целиком (`REQUIRE_SIGNIN_VIEW`, `DISABLE_REGISTRATION` в
`release.yaml`) — ни анонимного браузинга, ни самостоятельной
регистрации; все репозитории клиентов приватные, все аккаунты заводит
только `onboard_client.py` через admin API.

Два репозитория на нём, оба заводятся один раз через Forgejo API
(`POST /api/v1/orgs`, `POST /api/v1/orgs/<org>/repos`):

```sh
# Канонический клиентский референс, раздаётся клиентам — родной Forgejo-
# репозиторий (не mirror откуда-то), пушится напрямую из локального клона:
git push "https://<forgejo-admin>:<pass>@git.${BASE_DOMAIN}/showcase/saas-demo-provider.git"

# Наш собственный чарт базовой инфры клиента — private, НЕ для клиентов
git push --mirror \
  "https://<forgejo-admin>:<pass>@git.${BASE_DOMAIN}/ops/client-infra.git"
```

(`ops/client-infra` — с `"private": true`, `showcase/saas-demo-provider`
— публичный внутри инстанса, но сам инстанс закрыт для анонимов целиком,
см. ниже.)

API-токен админа для `onboard_client.py`/`offboard_client.py` —
`git.${BASE_DOMAIN}` → Settings → Applications → Generate New Token (или
через API: `POST /api/v1/users/<admin>/tokens`), нужны scopes
`write:admin`, `write:repository`, `write:user`, `write:organization`.

### GitRepository на чарт `ops/client-infra` (`unitum-demo-k8s-infra`)

`infrastructure/sources/gitrepositories.yaml` — `GitRepository
client-infra-chart` в `flux-system`, смотрит на `ops/client-infra` через
внутренний Service Forgejo (Flux не должен зависеть от внешнего DNS/TLS).
Приватный репозиторий → `secretRef` на **отдельный** read-only токен
(scope `read:repository`, не тот же, что `FORGEJO_ADMIN_TOKEN` у
скриптов) — `infrastructure/sources/client-infra-chart-repo-auth.yaml`,
пароль в SOPS (`git_stands_flux_read_token`).

### Forgejo Actions (CI на сборку образа клиента)

Сервер-сайд фича включена один раз (`gitea.config.actions.ENABLED: true`
в `release.yaml`) — сам раннер **не общий инстанс-wide**, а свой на
каждого клиента, заводит `ops/client-infra` (шаблоны `ci-runner.yaml`/
`ci-buildkitd.yaml`) при онбординге. Ничего дополнительно бутстрапить не
нужно.

Почему так, а не один shared-раннер: раннер регистрируется **repo-scoped**
токеном (`GET /repos/{owner}/{repo}/actions/runners/registration-token`)
— физически ограничен ОДНИМ репозиторием, поэтому исполняет джобы
`host`-режимом (голые процессы, без Docker/DinD вовсе) безопасно — делить
процесс не с кем. Сборку образа делает не сам раннер, а отдельный
`ci-buildkitd` (BuildKit, rootless, без `privileged`) рядом, в том же
namespace — раннер лишь ходит к нему `buildctl`-ом. Ни разу нигде в этой
цепочке не появляется ни Docker-демон, ни kaniko (архивирован Google в
июне 2025, не рассматривался).

### Nexus (`nexus.${BASE_DOMAIN}` / docker на `docker.${BASE_DOMAIN}:5000`)

Docker registry API на **основном** порту 5000 включается values-ключом
`nexus.docker.registries[]` в HelmRelease (уже в
`infrastructure/apps/nexus/release.yaml`) — под общий,
**pull-only** репозиторий демо-образов `docker-clients` (см. "Про
изоляцию push" ниже — на push туда клиенты больше не получают доступа).
Персональные docker-хосты клиентов (`<slug>.docker.${BASE_DOMAIN}`) —
не через этот механизм, а через `Certificate wildcard-docker`
(`infrastructure/apps/nexus/wildcard-docker-certificate.yaml`,
`*.docker.${BASE_DOMAIN}`) + Service/Ingress, которые заводит сам чарт
`ops/client-infra` на каждого клиента (шаблон `nexus-docker.yaml`).

Сам `docker-clients` репозиторий и read-only роль на него внутри Nexus
чарт не создаёт — разово через REST API (админ-креды —
`nexus_admin_password` в SOPS):

```sh
AUTH="admin:<пароль>"
BASE="https://nexus.${BASE_DOMAIN}/service/rest/v1"

curl -u "$AUTH" -X PUT "$BASE/security/realms/active" \
  -H "Content-Type: application/json" -d '["NexusAuthenticatingRealm","DockerToken"]'

curl -u "$AUTH" -X POST "$BASE/repositories/docker/hosted" -H "Content-Type: application/json" -d '{
  "name": "docker-clients", "online": true,
  "storage": {"blobStoreName": "default", "strictContentTypeValidation": true, "writePolicy": "ALLOW"},
  "docker": {"v1Enabled": false, "forceBasicAuth": true, "httpPort": 5000}
}'

# Read-only — все клиенты получают эту роль на общие демо-образы,
# ничего больше (см. "Про изоляцию push" ниже):
curl -u "$AUTH" -X POST "$BASE/security/roles" -H "Content-Type: application/json" -d '{
  "id": "docker-shared-pull", "name": "docker-shared-pull",
  "privileges": [
    "nx-repository-view-docker-docker-clients-browse",
    "nx-repository-view-docker-docker-clients-read"
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
`docker-clients` репозитория. **Проверено живьём и не работает**: Content
Selector в Nexus не может матчить компонент, которого ещё не
существует — первый `docker push` нового имени образа падает `403`,
потому что selector не в состоянии сопоставить ещё-не-существующую
координату. Задокументированное ограничение Nexus для Docker-формата (в
отличие от Maven/npm, где координата известна из самого пути аплоада).

Следующий вариант — одна общая push-роль на весь `docker-clients` —
тоже был в проекте, но давал только **soft**-изоляцию (соглашение об
именовании, не техническая граница: любой клиент технически может
прочитать/перезаписать образ другого).

Текущая схема даёт **настоящую** изоляцию: у каждого клиента —
отдельный Nexus hosted-репозиторий (`docker-<slug>`, свой httpPort,
`onboard_client.py` подбирает следующий свободный) и отдельный хост
(`<slug>.docker.${BASE_DOMAIN}`, через wildcard-сертификат
`wildcard-docker`). Роль клиента — push+pull только на СВОЙ репозиторий
плюс read-only на общий `docker-clients` (демо-образы). Подтверждено
живьём: кредами клиента A push на хост клиента B → `403`; тот же push на
общий `docker-clients` (без выданного add/edit) → тоже `403`; push на
СВОЙ хост → `202`.

## Онбординг/офбординг клиента

```sh
pip install -r requirements.txt

export FORGEJO_ADMIN_TOKEN=...
export NEXUS_ADMIN_USER=admin
export NEXUS_ADMIN_PASSWORD=...
export KUBECONFIG=...

./onboard_client.py acme     # печатает URL стенда, Forgejo-креды, Nexus-креды, K8s-токен
./offboard_client.py acme    # сносит всё это обратно
```

Оба скрипта читают `BASE_DOMAIN`/`FORGEJO_URL`/`NEXUS_URL`/
`CANONICAL_OWNER`/`CANONICAL_REPO` из окружения с разумными дефолтами
(`hightps.online`, `showcase/saas-demo-provider`) — переопредели, если
стенд другой.

### Что именно делает `onboard_client.py` (по шагам)

1. Forgejo-аккаунт `client-<slug>` (REST API, `must_change_password: false`).
2. **Приватный** репозиторий под этим аккаунтом (виден только ему) +
   **разовый импорт** (`git clone --mirror` канонического
   `showcase/saas-demo-provider` → `git push --mirror` в репозиторий
   клиента; не живой fork/sync — дальше клиент сам решает, что делать со
   своей копией) + отдельный read-only токен (scope `read:repository`)
   **под аккаунтом самого клиента** — не его логин-пароль, узкий токен
   специально для Flux (тот же приём, что `client-infra-chart-repo-auth`
   в `unitum-demo-k8s-infra`) — и repo-scoped токен регистрации CI-раннера
   этого клиента (см. "Forgejo Actions" выше).
3. Nexus: свой hosted docker-репозиторий (`docker-<slug>`, свободный
   порт), push-роль только на него + read-only роль `docker-shared-pull`
   на общие демо-образы, пользователь клиента с обеими — и сразу же три
   Forgejo Action secret'а на его репозитории (`DOCKER_HOST`/
   `DOCKER_USER`/`DOCKER_PASSWORD`, теми же кредами) — без них CI есть, но
   падает на шаге логина в реестр.
4. **Один `HelmRelease client-infra-<slug>`** в `flux-system` (чарт
   `ops/client-infra`, `values`: slug/докер-порт/докер-креды/URL и
   read-only токен репозитория клиента/токен раннера) — helm-controller
   раскатывает из него:
   - `Namespace <slug>-saas`
   - `Certificate` — свой wildcard `*.<slug>-saas.${BASE_DOMAIN}`
   - `Secret registry-pull-secret` (dockerconfigjson, креды из шага 3)
   - `Secret client-<slug>-repo-auth` (в `flux-system`, basic-auth,
     read-only токен клиента из шага 2) + `GitRepository client-<slug>`
     (тоже в `flux-system`, с `secretRef` на этот `Secret`) → приватный
     репозиторий клиента на Forgejo
   - `HelmRelease app` — `sourceRef` на этот `GitRepository`, отсюда
     deploy-on-push (`reconcileStrategy: Revision`)
   - `ServiceAccount`/`Role`/`RoleBinding saas-provisioner` — полный CRUD
     на `helmreleases`, но **только в своём namespace** (проверено
     `kubectl auth can-i --as=system:serviceaccount:<ns>:saas-provisioner`
     на чужой namespace — `no`)
   - персональный docker-хост клиента (`Service`+`Ingress` в `nexus`)
   - персональный CI: `ci-runner` (Forgejo Actions раннер, `host`-режим,
     без Docker/DinD) + `ci-buildkitd` (BuildKit, rootless, без
     `privileged`) — оба только в его namespace, только на его репозиторий

   Скрипт дожидается `Ready=True` у `HelmRelease client-infra-<slug>`,
   прежде чем печатать сводку — установка реально проверена, не просто
   отправлена.
5. K8s-токен (`kubectl create token`, 30 дней) + всё вышеперечисленное —
   единым блоком в stdout.

`offboard_client.py` — в обратном порядке: `kubectl delete helmrelease
client-infra-<slug>` (и дожидается его реального исчезновения — снимается
только после helm uninstall, каскадно уносит namespace/certificate/
secret/GitRepository приложения/RBAC/персональный docker-хост/CI-раннер+
buildkitd), затем Nexus-репозиторий/роль/пользователь клиента, затем
Forgejo (репозиторий **до** аккаунта — Forgejo не даёт удалить
пользователя, пока за ним есть репозиторий, `422 user still has
ownership of repositories`) — удаление репозитория заодно уносит и его
repo-scoped регистрацию CI-раннера (отдельного API на это в этой версии
Forgejo нет, только UI — явного шага для этого в скрипте поэтому тоже нет).

Полный снос без архивирования — офбординг теряет и образы клиента в его
персональном Nexus-репозитории. Согласуется с тем, что офбординг везде
работает так же (namespace, Forgejo-репозиторий).

### Дальше

Со временем `onboarding_common.py` может стать основой admin-консоли или
личного кабинета клиента вместо CLI-скриптов — не в этой итерации, здесь
только зафиксировано намерение.
