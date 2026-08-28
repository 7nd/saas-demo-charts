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
