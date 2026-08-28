# Интеграция с кластером: HelmRelease как экземпляр SaaS-приложения

Этот документ описывает, как **внешняя система** (бэкенд твоего SaaS,
панель управления, provisioning-сервис) заводит, меняет и сносит экземпляры
приложения для конкретных клиентов, разговаривая напрямую с Kubernetes
API — без `kubectl`, без git, без доступа к инфраструктурному репозиторию.

Модель: **один клиент — один `HelmRelease`**. Пользователь регистрируется в
твоём SaaS → бэкенд создаёт `HelmRelease` → через несколько секунд у
клиента поднят под с его инстансом **и рабочий HTTPS URL** (`ingress` у
чарта включён по умолчанию; `IngressClass` на стенде уже дефолтный, а TLS
закрывает один wildcard-сертификат на всех клиентов разом — отдельно
указывать ни то, ни другое не нужно). Апгрейд тарифа — патч того же
объекта. Отключение клиента — удаление объекта. Для примера берём чарт
`charts/deployment-demo` из этого репозитория: он не про конкретный
продукт, зато честно показывает env-переменные, ресурсы и персистентность,
которые в реальном SaaS будут отличаться по тарифу.

Порядок ниже такой: сначала что нужно локально, чтобы вообще подступиться
к кластеру; затем — что на этом кластере уже есть и настроено платформенной
командой; и только потом — что провижининг-код делает и что в результате
получает клиент.

## Что нужно локально

- `kubectl`, а в нём — kubeconfig с контекстом на нужный кластер
  (`kubectl config current-context` должен показывать именно его). Нужен не
  провижининг-бэкенду, а тебе (или платформенной команде) — один раз
  прочитать три значения ниже и один раз применить RBAC-манифест. Сам
  бэкенд в проде этот kubeconfig никогда не увидит.
- `pip install kubernetes` — официальный Python-клиент
  ([kubernetes-client/python](https://github.com/kubernetes-client/python)),
  если собираешься гонять примеры кода ниже. `HelmRelease` — это CRD
  Flux'а, а не встроенный тип Kubernetes, поэтому работаем не через
  `AppsV1Api`/`CoreV1Api`, а через универсальный `CustomObjectsApi` с
  `group`/`version`/`plural` этого CRD.

## Кластер, на котором это работает

Вот что на этом стенде уже стоит и настроено — провижининг-код ничего из
этого не разворачивает и не должен уметь разворачивать, это уровень
платформенной команды:

- **Flux** (`source-controller`, `helm-controller`) — уже установлен и
  раскатывает саму платформу через GitOps (отдельный документ). Он же
  умеет превращать объект `HelmRelease` в реальный `helm install`, откуда
  бы этот объект ни появился.
- **`GitRepository saas-demo-charts`** — уже зарегистрирован в
  `flux-system` и указывает на этот репозиторий. `HelmRelease` тенанта
  просто ссылается на него по имени (`sourceRef`) — провижининг-код не
  знает и не должен знать, как чарт туда попал.
- **Namespace `demo`** — уже существует, и в нём уже применено следующее:

  - `ServiceAccount/saas-provisioner` + `Role`/`RoleBinding`,
    ограничивающие провижининг-бэкенд **только** ресурсом `helmreleases`
    внутри `demo` — не `ClusterRole`, не kubeconfig администратора:

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

    Провижининг-бэкенд физически не способен прочитать чужой `Secret`
    (включая TLS-секрет ниже — читать его не нужно, только *сослаться* на
    имя в спеке `HelmRelease`) или полезть в `Pod` соседнего namespace,
    даже если в его коде будет баг или он будет скомпрометирован.

  - `Certificate/wildcard-demo` → `Secret/wildcard-demo-tls` — wildcard
    TLS на весь `*.demo.hightps.online` уже выпущен и лежит в этом же
    namespace.
  - `ingress-nginx` — уже дефолтный `IngressClass` на кластере, указывать
    его явно в `HelmRelease` не нужно.
  - `external-dns` — уже работает в режиме `policy: sync` и следит за
    каждым `Ingress`: DNS-запись в Cloudflare появляется и пропадает сама.

Из всего этого провижининг-бэкенду в итоге нужны только три значения — и
все три достаются локальным `kubectl` из уже настроенного kubeconfig
(`kubectl config current-context` смотрит на нужный кластер), без
редактирования файлов руками:

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

Эти три переменные (плюс сам namespace `demo` и имя TLS-секрета
`wildcard-demo-tls`) — это всё, чем провижининг-бэкенд пользуется извне.
Он получает их один раз как обычные переменные окружения — никакого файла
kubeconfig ему для этого не нужно.

## Что делаем и что получаем

### Авторизация в коде: два случая, один результат

У провижининг-бэкенда есть ровно два варианта, где он физически работает,
и это меняет способ авторизации:

- **Внутри кластера** (сам бэкенд — под в том же или другом кластере) —
  Kubernetes уже подложил ему токен ServiceAccount и CA-сертификат в
  файлы под `/var/run/secrets/kubernetes.io/serviceaccount/`, и выставил
  переменные окружения `KUBERNETES_SERVICE_HOST`/`_PORT` в каждый под
  автоматически, без твоего участия. Библиотека умеет прочитать всё это
  сама — `config.load_incluster_config()`.
- **Снаружи кластера** (сценарий из раздела выше: отдельный бэкенд, ему
  выдали токен и CA руками) — авторизация собирается вручную из тех же
  трёх переменных окружения.

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

Ветка "снаружи" — ровно те три переменные окружения из предыдущего
раздела (`K8S_API_SERVER`, `K8S_CA_CERT_PATH`, `K8S_TOKEN`). Дальше по
коду CRUD-функции ничего не знают, какая из двух веток отработала — обе
возвращают одинаковый `CustomObjectsApi`.

### Тело HelmRelease на одного клиента

То же самое, что лежало бы файлом в git при GitOps-раскатке — здесь просто
собирается кодом:

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

`Ingress` ссылается на `Secret/wildcard-demo-tls` из раздела выше —
Kubernetes не позволяет `Ingress` брать TLS-секрет из чужого namespace,
поэтому и `HelmRelease` тенанта, и сам секрет обязаны жить в одном и том
же `demo`. cert-manager при этом вообще не вызывается — ни на создание
тенанта, ни на его удаление.

### Делаем create → получаем зарегистрированного клиента

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

Пока это только запись в API — под ещё не поднят.

### Делаем wait → получаем подтверждение, что инстанс реально работает

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
завести CNAME в Cloudflare, и урл клиента отвечает по HTTPS с доверенным
сертификатом — из интернета, не из кластера:

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

### Делаем update → получаем новый тариф на том же URL

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

### Делаем delete → получаем полную очистку

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

## Готовый к запуску пример

Тот же код, что разобран по кусочкам выше, — одним файлом с комментариями,
чтобы проследить логику от начала до конца:
[`../examples/manage_tenant.py`](../examples/manage_tenant.py). Установка и
переменные окружения — в [`../examples/README.md`](../examples/README.md):

```sh
cd examples
pip install -r requirements.txt
python manage_tenant.py
```

Скрипт сам проходит весь жизненный цикл одного тестового клиента —
`create` → дождаться `Ready` → `update` тарифа → дождаться `Ready` →
`delete` — печатая, что происходит на каждом шаге.

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
