"""
Провижининг SaaS-тенантов через Flux HelmRelease.

Модель простая: один клиент — один объект HelmRelease в одном и том же
namespace. Всё, чем клиенты отличаются друг от друга (домен, тариф,
ресурсы) — это разница в spec.values того же объекта, не отдельные
механизмы. HelmRelease — это CRD Flux'а, а не встроенный тип Kubernetes,
поэтому работаем не через AppsV1Api/CoreV1Api, а через универсальный
CustomObjectsApi с group/version/plural этого CRD.

Подробный разбор, почему каждый шаг устроен именно так — в
../docs/kubernetes-api-integration.md. Здесь тот же код одним куском, без
CLI и прочей обвязки, чтобы проще было проследить логику от начала до конца.
"""
import os
import time

from kubernetes import client, config
from kubernetes.config import ConfigException

# HelmRelease — CRD из helm.toolkit.fluxcd.io: CustomObjectsApi нужно явно
# сказать, group/version/plural какого именно ресурса имеются в виду.
GROUP, VERSION, PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"

# Где на демо-стенде живут тенанты, на каком домене у них хосты и каким
# TLS-секретом закрыт HTTPS. Если стенд другой — правь прямо здесь.
NAMESPACE = "demo"
BASE_DOMAIN = "demo.hightps.online"
WILDCARD_TLS_SECRET = "wildcard-demo-tls"


def make_api() -> client.CustomObjectsApi:
    """Собирает клиента Kubernetes API — по-разному, в зависимости от того, где выполняется код."""
    try:
        # Если этот код сам работает подом в кластере — Kubernetes уже
        # подложил токен ServiceAccount и CA-сертификат в файлы пода и
        # выставил переменные окружения KUBERNETES_SERVICE_HOST/_PORT.
        # load_incluster_config() читает всё это сама, без нашего участия.
        config.load_incluster_config()
        return client.CustomObjectsApi()
    except ConfigException:
        pass  # не под в кластере — собираем конфиг вручную ниже

    # Снаружи кластера авторизация собирается из трёх значений, которые
    # платформенная команда выдаёт провижининг-сервису один раз: адрес
    # API-сервера, CA-сертификат кластера и токен ServiceAccount с правами
    # ТОЛЬКО на helmreleases в одном namespace — не kubeconfig администратора
    # (как получить эти три значения через kubectl — см. docs/).
    cfg = client.Configuration()
    cfg.host = os.environ["K8S_API_SERVER"]
    cfg.ssl_ca_cert = os.environ["K8S_CA_CERT_PATH"]
    cfg.api_key = {"authorization": f"Bearer {os.environ['K8S_TOKEN']}"}
    return client.CustomObjectsApi(client.ApiClient(cfg))


def helmrelease_spec(name: str, slug: str, plan: str) -> dict:
    """Тело HelmRelease на одного клиента — то же самое, что лежало бы файлом в git при GitOps-раскатке."""
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
                    # Чарт живёт в git без бампа version на каждый коммит —
                    # берём его по git-ревизии, а не по version в Chart.yaml.
                    "reconcileStrategy": "Revision",
                }
            },
            "install": {"remediation": {"retries": 3}},
            "upgrade": {"remediation": {"retries": 3}},
            "values": {
                # HTTPS без единого лишнего действия на клиента: ссылаемся
                # на уже готовый wildcard-секрет, cert-manager тут вообще
                # не вызывается — ни на создание тенанта, ни на удаление.
                "ingress": {
                    "host": f"tenant-{slug}.{BASE_DOMAIN}",
                    "tls": {"enabled": True, "secretName": WILDCARD_TLS_SECRET},
                },
                # Вот тут и только тут клиенты отличаются друг от друга.
                "env": {"plan": plan},
            },
        },
    }


def create_tenant(api: client.CustomObjectsApi, slug: str, plan: str) -> int:
    """Регистрация нового клиента: создаёт HelmRelease, дальше им займётся helm-controller."""
    name = f"tenant-{slug}"
    obj = api.create_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL,
        body=helmrelease_spec(name, slug, plan),
    )
    # generation растёт на каждое изменение spec — пригодится в wait_ready,
    # чтобы понять, какую именно версию объекта уже обработал контроллер.
    return obj["metadata"]["generation"]


def wait_ready(api: client.CustomObjectsApi, slug: str, generation: int, timeout: int = 60) -> None:
    """Создание объекта в API — это не то же самое, что готовый под: ждём, пока helm-controller реально раскатит инстанс."""
    name = f"tenant-{slug}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = api.get_namespaced_custom_object(
            group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name,
        )
        status = obj.get("status", {})
        ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready"), None)
        # Сравниваем именно с той generation, что вернул create/update, а не
        # просто смотрим на Ready == True: иначе можно поймать "готов",
        # оставшийся от ПРЕДЫДУЩЕЙ версии спеки — контроллер ещё не успел
        # заметить наш последний patch, а объект уже честно отвечает "готов".
        if ready and status.get("observedGeneration") == generation and ready["status"] == "True":
            print(f"[wait]   generation={generation} Ready=True: {ready['message']}")
            return
        time.sleep(2)
    raise TimeoutError(f"{name} не готов за {timeout}s")


def update_tenant_plan(api: client.CustomObjectsApi, slug: str, plan: str) -> int:
    """Смена тарифа — обычный patch того же объекта, а не отдельный механизм апгрейда."""
    name = f"tenant-{slug}"
    # JSON merge patch — трогает только spec.values.env.plan, остальная
    # спека (например, ingress.tls) остаётся как была.
    patch = {"spec": {"values": {"env": {"plan": plan}}}}
    obj = api.patch_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name,
        body=patch,
    )
    return obj["metadata"]["generation"]


def delete_tenant(api: client.CustomObjectsApi, slug: str) -> None:
    """Отключение клиента. HelmRelease создаётся с finalizer'ом, поэтому один delete убирает разом
    всё, что чарт создавал для этого клиента — Deployment, Service, Ingress, PVC, историю релиза."""
    name = f"tenant-{slug}"
    api.delete_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=NAMESPACE, plural=PLURAL, name=name,
    )


if __name__ == "__main__":
    # Полный жизненный цикл одного клиента: регистрация -> дождались, что
    # инстанс реально поднят -> апгрейд тарифа -> дождались повторно ->
    # отключение. Поменяй slug/plan ниже, чтобы поэкспериментировать.
    api = make_api()

    gen = create_tenant(api, "acme", plan="pro")
    print("[create] HelmRelease/tenant-acme создан, plan=pro")
    wait_ready(api, "acme", gen)

    gen = update_tenant_plan(api, "acme", plan="enterprise")
    print("[update] HelmRelease/tenant-acme -> plan=enterprise")
    wait_ready(api, "acme", gen)

    delete_tenant(api, "acme")
    print("[delete] HelmRelease/tenant-acme помечен на удаление")
