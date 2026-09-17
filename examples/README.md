# examples/

Пример провижининга SaaS-тенантов через `HelmRelease` (Flux) — тот же код,
что разобран по кусочкам в
[`../docs/kubernetes-api-integration.md`](../docs/kubernetes-api-integration.md),
но одним файлом с комментариями, чтобы проследить логику от начала до конца.

## Установка

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Доступ к кластеру

Если скрипт запускается не подом внутри кластера (обычный случай для этого
примера), ему нужны три переменные окружения — как их получить через
`kubectl`, см. раздел «Что уже должно быть в кластере» в
[`../docs/kubernetes-api-integration.md`](../docs/kubernetes-api-integration.md):

```sh
export K8S_API_SERVER=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')
kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > ca.crt
export K8S_CA_CERT_PATH=ca.crt
export K8S_TOKEN=$(kubectl create token saas-provisioner -n demo --duration=2h)
```

Если у стенда другой namespace/домен/имя TLS-секрета — это константы
`NAMESPACE`/`BASE_DOMAIN`/`WILDCARD_TLS_SECRET` в начале `manage_tenant.py`,
поправь их прямо там.

`K8S_TOKEN` из `kubectl create token` живёт ограниченное время (`--duration`)
— если скрипт вдруг перестал авторизовываться, скорее всего токен просто
истёк, перевыпусти командой выше.

## Использование

```sh
python manage_tenant.py
```

Скрипт внизу файла (`if __name__ == "__main__":`) сам проходит весь
жизненный цикл одного тестового клиента — `create` → дождаться `Ready` →
`update` тарифа → дождаться `Ready` → `delete` — и печатает, что происходит
на каждом шаге. Чтобы поэкспериментировать, правь вызовы прямо там (`slug`,
`plan`) или зови функции `create_tenant`/`update_tenant_plan`/`delete_tenant`
из своего кода/интерпретатора — это и есть весь API этого примера.

## `tenant-api/` — тот же провижининг, но как деплоящийся REST API

`manage_tenant.py` выше — код, который гоняют руками. `tenant-api/` —
то же самое, но уже готовый к развёртыванию HTTP-сервис: свой
`Dockerfile`, свой K8s-манифест, и — важно — **своя CI**, которая сама
собирает и деплоит его при каждом пуше. Ничего собирать/пушить/апплаить
руками не нужно.

### Что это и зачем

Это референс того, как **вы сами** могли бы построить контрол-плейн для
провижининга СВОИХ тенантов — HTTP API поверх той же логики, что в
`manage_tenant.py` (создать/посмотреть/поменять тариф/удалить), которым
могло бы дёргать ваше приложение, биллинг, админка и т.д. Это НЕ часть
того, как мы провизионим вам стенд — это то, что вы могли бы взять за
основу для СВОЕГО продукта. Работает целиком внутри вашего же
namespace, вашим же self-service доступом — ничего нового заводить не
нужно.

### Как деплоится

Просто `git push` с изменением внутри `examples/tenant-api/**` (даже
пустой коммит подойдёт, чтобы попробовать) — `.forgejo/workflows/tenant-api.yml`
сама:
1. собирает образ (BuildKit, тот же приём, что и для `app/` — см.
   `docs/onboarding/README.md`, "CI-сборка образа")
2. пушит его в ваш personal registry
3. коммитит обратно `k8s/deployment.yaml` с реальным тегом образа —
   то, что в git, и то, что применено, всегда одно и то же
4. `kubectl apply` этим манифестом — **вашим же self-service токеном**
   (`K8S_TOKEN`/`K8S_API_SERVER`, теми же, что вы получили в сводке
   онбординга и которыми могли бы пользоваться в `manage_tenant.py`) — в
   ваш же `<slug>-saas` namespace

Никакого отдельного секрета/доступа заводить не нужно — всё уже лежит в
Action secrets репозитория с момента онбординга.

### Авторизация — сквозной токен, не своя проверка

Каждый запрос к API обязан нести `Authorization: Bearer <k8s-токен>`.
Сервис **сам этот токен не проверяет** — он строит клиента
Kubernetes API С ЭТИМ токеном и идёт прямо в K8s API (`kubectl`, только
из Python-кода). Значит, авторизует запрос не наш код, а API-сервер
кластера — тот же RBAC (`saas-provisioner` Role, см.
`docs/onboarding/README.md`, раздел про self-service), который уже
ограничивает права одним namespace на клиента. «Неотключаемая» в
буквальном смысле: в коде физически нет пути, которым запрос без
валидного токена дошёл бы до какого-то действия — сама проверка
происходит не у нас. Отсюда и `automountServiceAccountToken: false` в
`k8s/deployment.yaml` — сервису нечем было бы авторизовать запрос сам,
даже если бы захотел, у него просто нет своего токена.

Живой пример границы:
- без заголовка `Authorization` — `401`, до K8s API запрос не доходит
- с ВАЛИДНЫМ, но ЧУЖИМ self-service токеном (другого клиента) — K8s
  сам вернёт `403` (RBAC этого токена не покрывает ваш namespace),
  сервис отдаёт этот `403` наружу как есть

### Эндпоинты

| Метод | Путь | Тело | Что делает |
|---|---|---|---|
| `POST` | `/tenants` | `{"slug": "acme", "plan": "pro"}` | создать тенанта, дождаться `Ready`, вернуть статус |
| `GET` | `/tenants/{slug}` | — | текущий статус (`ready`, `plan`, `message`) |
| `PATCH` | `/tenants/{slug}` | `{"plan": "enterprise"}` | сменить тариф, дождаться `Ready` |
| `DELETE` | `/tenants/{slug}` | — | удалить |

### Полный цикл через curl

```sh
API=http://tenant-api.<slug>-saas.svc.cluster.local:8080   # или port-forward: kubectl port-forward svc/tenant-api 8080:8080
TOKEN=<K8S_TOKEN из сводки онбординга>

curl -X POST "$API/tenants" -H "Authorization: Bearer $TOKEN" \
  -d '{"slug":"acme","plan":"pro"}'

curl "$API/tenants/acme" -H "Authorization: Bearer $TOKEN"

curl -X PATCH "$API/tenants/acme" -H "Authorization: Bearer $TOKEN" \
  -d '{"plan":"enterprise"}'

curl -X DELETE "$API/tenants/acme" -H "Authorization: Bearer $TOKEN"
```

### Где заканчивается сервис и начинается K8s RBAC

Сервис — тонкая HTTP-обёртка, ни разу не решает «можно/нельзя» сам.
Реальная граница прав — та же `saas-provisioner` Role, что уже выдана
вашему namespace при онбординге (полное описание — self-service раздел
в [`../docs/onboarding/README.md`](../docs/onboarding/README.md)).
Что этот токен может и не может — уже проверено живьём там же
(`kubectl auth can-i` на чужой namespace → `no`).
