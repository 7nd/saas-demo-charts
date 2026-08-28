# Интеграция с кластером: HelmRelease как экземпляр SaaS-приложения

Этот документ — не про то, как устроен чарт (см. корневой
[README.md](../README.md)), а про то, как **внешняя система** (бэкенд
твоего SaaS, панель управления, provisioning-сервис) заводит, меняет и
сносит экземпляры приложения для конкретных клиентов, разговаривая
напрямую с Kubernetes API — без `kubectl`, без git, без доступа к
инфраструктурному репозиторию.

Модель простая: **один клиент — один `HelmRelease`**. Пользователь
регистрируется в твоём SaaS → бэкенд создаёт `HelmRelease` → через
несколько секунд у клиента поднят под с его инстансом **и рабочий HTTPS
URL** (`ingress` у чарта включён по умолчанию; `IngressClass` на стенде
уже дефолтный, а TLS закрывает один wildcard-сертификат на всех клиентов
разом — отдельно указывать ни то, ни другое не нужно). Апгрейд тарифа —
патч того же объекта. Отключение клиента — удаление объекта. Для примера
берём чарт `charts/deployment-demo` из этого репозитория: он не про
конкретный продукт, зато честно показывает env-переменные, ресурсы и
персистентность, которые в реальном SaaS будут отличаться по тарифу.

## Что уже должно быть в кластере (и почему это не тема этого документа)

Чтобы `HelmRelease` вообще собрался, в кластере уже должен стоять Flux
(`helm-controller`, `source-controller`) и должен быть заведён источник
чарта — `GitRepository` на этот репозиторий (или `HelmRepository`, если
чарт публикуется). Это уровень платформенной команды, разовая настройка,
и в контексте интеграции внешнего бэкенда — чёрный ящик: провижининг-код
ниже про это ничего не знает и знать не должен, он просто ссылается на
уже существующий источник по имени (`sourceRef`).

Всё, что нужно провижининг-бэкенду для *использования* уже настроенного
кластера — это четыре вещи, которые платформенная команда выдаёт ему
один раз:

1. **URL API-сервера** — `https://<адрес кластера>:6443`.
2. **CA-сертификат кластера** — чтобы TLS-соединению можно было доверять
   без `--insecure`.
3. **Токен ServiceAccount**, у которого есть права **только** на
   `HelmRelease` в одном namespace — не kubeconfig администратора.
4. **Namespace**, в котором бэкенду разрешено создавать `HelmRelease`, и
   имя TLS-секрета в этом namespace, которым уже закрыт HTTPS для всех
   клиентов сразу (подробнее — в разделе про Ingress ниже).

Второе-четвёртое — то, что платформенная команда заводит **один раз**
(не на каждого клиента), применив вот такой манифест `kubectl apply`'ом
(или его же — через тот самый Flux, но это уже другой документ). Ниже
используем существующий на стенде namespace `demo` — именно там уже
лежит нужный wildcard-сертификат:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: saas-provisioner
  namespace: demo
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: saas-provisioner
  namespace: demo
rules:
  - apiGroups: ["helm.toolkit.fluxcd.io"]
    resources: ["helmreleases"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: saas-provisioner
  namespace: demo
subjects:
  - kind: ServiceAccount
    name: saas-provisioner
    namespace: demo
roleRef:
  kind: Role
  name: saas-provisioner
  apiGroup: rbac.authorization.k8s.io
```

Обрати внимание: это `Role`, не `ClusterRole` — бэкенд не видит и не
может тронуть ничего за пределами `demo`, и даже внутри неё — только
`helmreleases`. Он физически не способен прочитать чужой Secret (включая
тот самый TLS-секрет — читать его бэкенду и не нужно, только *ссылаться*
на имя в спеке `HelmRelease`) или полезть в `Pod` соседнего namespace,
даже если в его коде будет баг или он будет скомпрометирован.

Токен для этого `ServiceAccount` (в проде — через `TokenRequest`
API/проецируемый том, который сам ротируется; для примера — разовый
`kubectl create token`, живёт ограниченное время) и CA кластера (это
открытый сертификат, не секрет) отдаём бэкенду как обычные переменные
окружения — никакого файла kubeconfig ему для этого не нужно. Все три
значения достаются из уже настроенного kubeconfig (текущий `context`,
`kubectl config current-context`, должен смотреть на нужный кластер):

```sh
# 1. URL API-сервера — берём из текущего кластера в kubeconfig,
# а не вписываем руками
export K8S_API_SERVER=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')

# 2. CA-сертификат кластера — публичный сертификат, не секрет,
# поэтому просто сохраняем его в файл
kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > ca.crt
export K8S_CA_CERT_PATH=ca.crt

# 3. Токен ServiceAccount saas-provisioner (см. Role/RoleBinding выше) —
# НЕ kubeconfig админа, а токен с правами только на helmreleases в demo
export K8S_TOKEN=$(kubectl create token saas-provisioner -n demo --duration=2h)
```

## Клиент: официальная библиотека, без kubeconfig

`pip install kubernetes` — официальный Python-клиент
([kubernetes-client/python](https://github.com/kubernetes-client/python)).
`HelmRelease` — это CRD Flux'а, а не встроенный тип, поэтому работаем не
через `AppsV1Api`/`CoreV1Api`, а через универсальный
`CustomObjectsApi` с `group`/`version`/`plural` этого CRD.

### Авторизация: два случая, один код

У провижининг-бэкенда есть ровно два варианта, где он физически
работает, и это меняет способ авторизации:

- **Внутри кластера** (сам бэкенд — под в том же или другом кластере) —
  Kubernetes уже подложил ему токен ServiceAccount и CA-сертификат в
  файлы под `/var/run/secrets/kubernetes.io/serviceaccount/`, и выставил
  переменные окружения `KUBERNETES_SERVICE_HOST`/`_PORT` в каждый под
  автоматически, без твоего участия. Библиотека умеет прочитать всё это
  сама — `config.load_incluster_config()`.
- **Снаружи кластера** (наш сценарий выше: отдельный бэкенд, ему выдали
  токен и CA руками) — авторизация собирается вручную из токена/CA,
  которые лежат, например, в переменных окружения.

Проверять на глаз (например, наличие `KUBERNETES_SERVICE_HOST`) не
нужно — та же проверка уже зашита в саму библиотеку:
`config.load_incluster_config()` сам смотрит на переменные окружения и
файл токена под `/var/run/secrets/kubernetes.io/serviceaccount/`, и если
их нет — не молчит, а кидает `ConfigException`. На этом и строим
`make_api()`: пробуем incluster, а если это не под в кластере — падаем в
`except` и собираем конфиг из токена явно:

```python
import os

from kubernetes import client, config
from kubernetes.config import ConfigException

GROUP, VERSION, PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"
NAMESPACE = "demo"


def make_api() -> client.CustomObjectsApi:
    try:
        # бэкенд сам — под в кластере: токен, CA и namespace уже
        # смонтированы Kubernetes'ом, ничего не читаем и не задаём сами
        config.load_incluster_config()
        return client.CustomObjectsApi()
    except ConfigException:
        pass  # не под в кластере — авторизуемся токеном, см. ниже

    cfg = client.Configuration()
    cfg.host = os.environ["K8S_API_SERVER"]           # https://<адрес кластера>:6443
    cfg.ssl_ca_cert = os.environ["K8S_CA_CERT_PATH"]  # путь до файла с CA
    cfg.api_key = {"authorization": f"Bearer {os.environ['K8S_TOKEN']}"}
    return client.CustomObjectsApi(client.ApiClient(cfg))
```

Ветка "снаружи" — ровно те три переменные окружения из прошлого раздела
(`K8S_API_SERVER`, `K8S_CA_CERT_PATH`, `K8S_TOKEN`). Дальше по коду
CRUD-функции ничего не знают, какая из двух веток отработала — обе
возвращают одинаковый `CustomObjectsApi`.

## CRUD

Тело `HelmRelease` на одного клиента — то же самое, что и в
GitOps-раскатке, просто собирается кодом, а не лежит файлом в git.

Ingress с HTTPS без единого лишнего действия на клиента: на стенде уже
выпущен wildcard-сертификат на `*.demo.hightps.online`
(`Certificate/wildcard-demo` → `Secret/wildcard-demo-tls`, оба в
namespace `demo`). Мы просто ссылаемся на этот `Secret` в
`ingress.tls.secretName` — Kubernetes не позволяет `Ingress` брать
TLS-секрет из чужого namespace, поэтому и `HelmRelease` тенанта, и
сертификат должны жить в одном и том же `demo`. cert-manager при этом
вообще не вызывается — ни на создание тенанта, ни на его удаление:

```python
BASE_DOMAIN = "demo.hightps.online"
WILDCARD_TLS_SECRET = "wildcard-demo-tls"

def helmrelease_spec(name: str, slug: str, plan: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "HelmRelease",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "interval": "5m",
            "chart": {
                "spec": {
                    "chart": "charts/deployment-demo",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "name": "saas-demo-charts",
                        "namespace": "flux-system",
                    },
                    "reconcileStrategy": "Revision",
                }
            },
            "install": {"remediation": {"retries": 3}},
            "upgrade": {"remediation": {"retries": 3}},
            "values": {
                # ingress.className не трогаем — на стенде nginx уже дефолтный IngressClass
                "ingress": {
                    "host": f"tenant-{slug}.{BASE_DOMAIN}",
                    "tls": {"enabled": True, "secretName": WILDCARD_TLS_SECRET},
                },
                "env": {"plan": plan},
            },
        },
    }
```

### Create — регистрация нового клиента

```python
def create_tenant(api: client.CustomObjectsApi, slug: str, plan: str) -> int:
    name = f"tenant-{slug}"
    obj = api.create_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL,
        body=helmrelease_spec(name, slug, plan),
    )
    return obj["metadata"]["generation"]  # пригодится, чтобы дождаться именно этой версии спеки
```

Реальный прогон на живом кластере (`slug="wildcard"`, тариф `pro`):

```
[create] HelmRelease/tenant-wildcard создан, plan=pro, host=tenant-wildcard.demo.hightps.online
```

### Read — ждём, пока `helm-controller` реально раскатит инстанс

Создание объекта в API — это не то же самое, что готовый под: между
`create` и реальным `Running` проходит время на `helm install`. Опрашиваем
`status.conditions` того же объекта:

```python
def wait_ready(api: client.CustomObjectsApi, slug: str, generation: int, timeout: int = 60) -> None:
    name = f"tenant-{slug}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = api.get_namespaced_custom_object(
            group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name,
        )
        status = obj.get("status", {})
        ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready"), None)
        if ready and status.get("observedGeneration") == generation and ready["status"] == "True":
            print(f"[wait] generation={generation} Ready=True: {ready['message']}")
            return
        time.sleep(2)
    raise TimeoutError(f"{name} не готов за {timeout}s")
```

Обрати внимание на `status.get("observedGeneration") == generation`, а не
просто `Ready == True`. Это не перестраховка ради красоты кода — без неё
опрос сразу после `patch` (см. Update ниже) может поймать `Ready=True`,
оставшийся от **предыдущей** версии спеки: контроллер ещё не успел
заметить твой патч, а объект уже честно отвечает "готов" — просто готов
он был к прошлому состоянию. `generation` инкрементится Kubernetes'ом на
каждое изменение `spec` того же объекта; `observedGeneration` проставляет
сам `helm-controller`, когда обработал именно эту версию. Сравнение двух
чисел — единственный надёжный способ понять "проверяю актуальное
состояние, а не старое".

```
[wait]   generation=1 Ready=True: Helm install succeeded for release demo/tenant-wildcard.v1 with chart deployment-demo@0.1.0+ab0597d79bfb
```

К этому моменту `Ingress` тоже реально поднят, `external-dns` уже успел
завести CNAME в Cloudflare (`policy: sync` в `infrastructure/controllers/external-dns`
инфраструктурного репозитория), и урл клиента отвечает по HTTPS с
доверенным сертификатом — из интернета, не из кластера:

```sh
curl -sv https://tenant-wildcard.demo.hightps.online/ 2>&1 | grep -i "subject\|issuer\|verify ok"
curl -s  https://tenant-wildcard.demo.hightps.online/ | grep DEMO_PLAN
```
```
*  subject: CN=*.demo.hightps.online
*  subjectAltName: host "tenant-wildcard.demo.hightps.online" matched cert's "*.demo.hightps.online"
*  issuer: C=US; O=Let's Encrypt; CN=YR2
*  SSL certificate verify ok.
<tr><td><code>DEMO_PLAN</code></td><td>pro</td></tr>
```

Один и тот же сертификат `*.demo.hightps.online` честно проходит проверку
для хоста конкретного клиента — это и есть весь смысл wildcard-схемы: TLS
для нового тенанта работает мгновенно, без единого обращения к
cert-manager/ACME.

### Update — смена тарифа

```python
def update_tenant_plan(api: client.CustomObjectsApi, slug: str, plan: str) -> int:
    name = f"tenant-{slug}"
    patch = {"spec": {"values": {"env": {"plan": plan}}}}
    obj = api.patch_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name,
        body=patch,
    )
    return obj["metadata"]["generation"]
```

`patch_namespaced_custom_object` шлёт JSON merge patch — трогает только
переданные ключи (`spec.values.env.plan`), остальная спека (например,
`ingress.tls`) остаётся как была. Реальный прогон, тариф `pro →
enterprise`:

```
[update] HelmRelease/tenant-wildcard -> plan=enterprise
[wait]   generation=2 Ready=True: Helm upgrade succeeded for release demo/tenant-wildcard.v2 with chart deployment-demo@0.1.0+ab0597d79bfb
```

`generation` выросло до 2, `helm-controller` увидел это и прогнал `helm
upgrade` — релиз стал `.v2`. Тот же публичный HTTPS URL клиента, без
единого изменения в Ingress/TLS/DNS, уже отдаёт новое значение:

```sh
curl -s https://tenant-wildcard.demo.hightps.online/ | grep DEMO_PLAN
```
```
<tr><td><code>DEMO_PLAN</code></td><td>enterprise</td></tr>
```

### Delete — отключение клиента

```python
def delete_tenant(api: client.CustomObjectsApi, slug: str) -> None:
    name = f"tenant-{slug}"
    api.delete_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name,
    )
```

`HelmRelease` создаётся с финалайзером (`finalizers.fluxcd.io`) — раньше,
чем объект реально исчезнет из etcd, `helm-controller` успевает выполнить
`helm uninstall`. Поэтому один `delete` на `HelmRelease` убирает не
только его самого, но и всё, что чарт создавал для этого клиента —
`Deployment`, `Service`, `Ingress`, `PVC`, секрет-хранилище релиза:

```
[delete] HelmRelease/tenant-wildcard помечен на удаление
[delete] HelmRelease/tenant-wildcard удалён, ресурсы релиза сняты
```

Вместе с `Ingress` пропадает и повод для DNS-записи — `external-dns`
работает в режиме `policy: sync`, значит на следующей сверке (или сразу,
если форсировать) он сам уберёт CNAME `tenant-wildcard.demo.hightps.online`
из Cloudflare. `Secret/wildcard-demo-tls` при этом никуда не девается —
он не принадлежит удалённому `Ingress`, это общий ресурс на весь
namespace, который переживает любого отдельного клиента:

```sh
kubectl get secret wildcard-demo-tls -n demo
```
```
NAME                TYPE                DATA   AGE
wildcard-demo-tls   kubernetes.io/tls   2      4m42s
```

(Если нужно дождаться именно физического исчезновения перед тем, как
считать клиента отключённым — опрашивай `get_namespaced_custom_object` до
`ApiException` со `status == 404`, как в примере выше с `wait_ready`.)

## Полный скрипт

```python
import os
import time

from kubernetes import client, config
from kubernetes.config import ConfigException

NAMESPACE = "demo"
BASE_DOMAIN = "demo.hightps.online"
WILDCARD_TLS_SECRET = "wildcard-demo-tls"
GROUP, VERSION, PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"


def make_api() -> client.CustomObjectsApi:
    try:
        # бэкенд сам — под в кластере: токен/CA уже смонтированы Kubernetes'ом
        config.load_incluster_config()
        return client.CustomObjectsApi()
    except ConfigException:
        pass  # не под в кластере — авторизуемся токеном, см. раздел про RBAC/токен

    cfg = client.Configuration()
    cfg.host = os.environ["K8S_API_SERVER"]
    cfg.ssl_ca_cert = os.environ["K8S_CA_CERT_PATH"]
    cfg.api_key = {"authorization": f"Bearer {os.environ['K8S_TOKEN']}"}
    return client.CustomObjectsApi(client.ApiClient(cfg))


def helmrelease_spec(name: str, slug: str, plan: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "HelmRelease",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "interval": "5m",
            "chart": {
                "spec": {
                    "chart": "charts/deployment-demo",
                    "sourceRef": {"kind": "GitRepository", "name": "saas-demo-charts", "namespace": "flux-system"},
                    "reconcileStrategy": "Revision",
                }
            },
            "install": {"remediation": {"retries": 3}},
            "upgrade": {"remediation": {"retries": 3}},
            "values": {
                "ingress": {
                    "host": f"tenant-{slug}.{BASE_DOMAIN}",
                    "tls": {"enabled": True, "secretName": WILDCARD_TLS_SECRET},
                },
                "env": {"plan": plan},
            },
        },
    }


def create_tenant(api, slug, plan):
    name = f"tenant-{slug}"
    obj = api.create_namespaced_custom_object(group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL,
                                               body=helmrelease_spec(name, slug, plan))
    return obj["metadata"]["generation"]


def wait_ready(api, slug, generation, timeout=60):
    name = f"tenant-{slug}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = api.get_namespaced_custom_object(group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name)
        status = obj.get("status", {})
        ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready"), None)
        if ready and status.get("observedGeneration") == generation and ready["status"] == "True":
            return
        time.sleep(2)
    raise TimeoutError(f"{name} не готов за {timeout}s")


def update_tenant_plan(api, slug, plan):
    name = f"tenant-{slug}"
    patch = {"spec": {"values": {"env": {"plan": plan}}}}
    obj = api.patch_namespaced_custom_object(group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL,
                                              name=name, body=patch)
    return obj["metadata"]["generation"]


def delete_tenant(api, slug):
    name = f"tenant-{slug}"
    api.delete_namespaced_custom_object(group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name)


if __name__ == "__main__":
    api = make_api()
    gen = create_tenant(api, "acme", plan="pro")
    wait_ready(api, "acme", gen)

    gen = update_tenant_plan(api, "acme", plan="enterprise")
    wait_ready(api, "acme", gen)

    delete_tenant(api, "acme")
```

## Куда смотреть, если что-то пошло не так

Со стороны бэкенда — тот же самый объект, тем же самым API-вызовом:
`status.conditions` внутри `get_namespaced_custom_object` — единственный
источник правды о состоянии клиента, его не нужно дублировать в своей
БД (кроме факта "клиент существует", если это нужно быстрее одного
API-вызова).

Если разбираешься руками (доступ у платформенной команды, не у
бэкенда) — то же самое можно посмотреть `kubectl`, для отладки это
быстрее, чем гонять Python:

| Команда | Что покажет |
|---|---|
| `kubectl get helmrelease -n demo` | `READY`/`STATUS` всех клиентов одной строкой |
| `kubectl describe helmrelease tenant-<slug> -n demo` | `status.conditions` целиком + связанные Events |
| `kubectl get events -n demo --sort-by=.lastTimestamp` | Пошаговая хронология: Pod/ReplicaSet/PVC/Helm |
| `kubectl get secret -n demo -l owner=helm` | История ревизий Helm-релиза (`sh.helm.release.v1.tenant-<slug>.v<N>`) |
| `kubectl -n flux-system logs deploy/helm-controller` | Сырые логи, если `Conditions` не объясняют, что пошло не так |

Если нужно не опрашивать `status` в цикле (как `wait_ready` выше), а
подписаться на изменения — у того же `CustomObjectsApi` есть
`list_namespaced_custom_object` со стримингом через
`kubernetes.watch.Watch()`; для одного объекта на клиента и нечастых
операций (signup/upgrade/offboarding) поллинг раз в 1–2 секунды обычно
проще в реализации и достаточно быстр.

## На что обратить внимание, прежде чем нести в прод

- **TLS-секрет и `Ingress` обязаны жить в одном namespace.** Это не
  особенность этого чарта, а ограничение самого Kubernetes: `Ingress` не
  умеет ссылаться на `Secret` из чужого namespace. Поэтому wildcard-схема
  из этого документа работает только пока все `HelmRelease` тенантов
  живут в том же namespace, что и `Secret/wildcard-demo-tls` (здесь —
  `demo`). Если тенантам по каким-то причинам нужен namespace на
  клиента — придётся либо копировать секрет в каждый (вручную или
  инструментом вроде
  [reflector](https://github.com/emberstack/kubernetes-reflector)),
  либо переходить на ingress-контроллер с поддержкой TLS-терминации на
  уровне, где такого ограничения нет (например, через `Gateway API`).
- **Wildcard — это один сертификат на всех, а не на клиента**, и именно
  поэтому rate limit Let's Encrypt (5 одинаковых сертификатов на hostname
  в неделю) здесь ни при чём: `*.demo.hightps.online` заказывается и
  переиздаётся отдельно от жизненного цикла тенантов
  (`infrastructure/apps` инфраструктурного репозитория), сколько бы
  клиентов ни создавалось и ни удалялось. Платить за это приходится
  меньшей изоляцией: скомпрометированный приватный ключ сертификата
  задевает HTTPS сразу всех клиентов на этом domain, а не одного.
- **DNS следует за Ingress автоматически и без спроса.** `external-dns`
  в этом кластере смотрит на все `Ingress` в `policy: sync` — как только
  `Ingress` появляется, в Cloudflare тут же возникает реальная DNS-запись
  на `ingress.host`, и она же пропадает при удалении. Значит `slug`
  клиента — это сразу и публичное имя хоста: не стоит пускать в него
  что попало, что пришло с формы регистрации, без валидации (буквы,
  цифры, дефис, ограничение длины) — это в итоге строка в DNS.
- **Имена клиентов должны быть уникальны** — `create_namespaced_custom_object`
  вернёт `409 Conflict`, если `tenant-<slug>` уже существует. Это не баг,
  а сигнал: либо клиент уже зарегистрирован (тогда нужен `update`, а не
  `create`), либо `slug` действительно занят.
- **Токен из `kubectl create token` — учебный.** У него ограниченный TTL и
  его нужно перевыпускать руками. В проде провижининг-сервис обычно
  получает токен через смонтированный `projected` том ServiceAccount
  (если сам работает в кластере) или через отдельный механизм выдачи
  креденшлов от платформенной команды — но набор прав (`Role`, только
  `helmreleases`, только один namespace) остаётся тем же самым.
