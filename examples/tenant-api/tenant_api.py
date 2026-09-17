"""
HTTP-обёртка над той же логикой, что в ../manage_tenant.py (см. его
докстринг и ../../docs/kubernetes-api-integration.md за разбором самого
паттерна HelmRelease-провижининга) — но деплоящаяся и вызываемая по
REST, а не гоняемая руками одним сценарием.

Модель авторизации — сквозной Bearer-токен, не своя проверка. Подробно,
с почему и конкретными curl-примерами — ../README.md, раздел
"tenant-api". Коротко: каждый запрос обязан нести `Authorization:
Bearer <K8s-токен>`; этот токен идёт В K8s API как есть — прошёл он
или нет, решает API-сервер кластера (RBAC), не этот файл. Поэтому
`k8s/deployment.yaml` рядом ставит `automountServiceAccountToken:
false` — сервису НЕЧЕМ было бы авторизовать запрос сам, даже если бы
захотел.

Namespace для тенантов — не хардкод (в отличие от NAMESPACE="demo" в
manage_tenant.py, который живёт в НАШЕМ общем демо-namespace) — берётся
из namespace, в котором сам под запущен: это ровно та область, где
self-service-токен клиента (Bearer-заголовок выше) реально имеет права
(saas-provisioner Role, см. docs/onboarding/README.md).
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Header, HTTPException
from kubernetes import client
from kubernetes.client.rest import ApiException

GROUP, VERSION, PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"

app = FastAPI(title="tenant-api")


def _own_namespace() -> str:
    # automountServiceAccountToken: false (k8s/deployment.yaml) значит
    # Kubernetes НЕ монтирует вообще ничего из serviceaccount — ни
    # токен, ни ca.crt, ни файл namespace (проверено живьём:
    # FileNotFoundError на /var/run/secrets/.../namespace — весь этот
    # projected volume просто не создаётся). Поэтому namespace идёт
    # через Downward API как обычная env-переменная (см. env.TENANT_NAMESPACE
    # в k8s/deployment.yaml, fieldRef: metadata.namespace) — она не
    # требует смонтированного serviceaccount вообще.
    return os.environ["TENANT_NAMESPACE"]


def _api_for_token(token: str) -> client.CustomObjectsApi:
    """Клиент K8s API с ЧУЖИМ (вызывающего) токеном — не с токеном этого пода, у пода его и нет
    (automountServiceAccountToken: false). По той же причине нет и ca.crt пода — TLS верификация
    выключена (verify_ssl=False), тот же компромисс, что и --insecure-skip-tls-verify в
    .forgejo/workflows/tenant-api.yml (клиенту нигде в этом проекте не выдаётся CA-сертификат
    кластера отдельно)."""
    cfg = client.Configuration()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ["KUBERNETES_SERVICE_PORT"]
    cfg.host = f"https://{host}:{port}"
    cfg.verify_ssl = False
    cfg.api_key = {"authorization": f"Bearer {token}"}
    return client.CustomObjectsApi(client.ApiClient(cfg))


def _require_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "нужен заголовок Authorization: Bearer <k8s-токен>")
    return authorization.split(" ", 1)[1]


def _as_http(e: ApiException) -> HTTPException:
    # 1-в-1 то, что вернул K8s API — если токен невалиден или без прав на этот
    # namespace/ресурс, это будет 401/403 отсюда, не откуда-то из нашего кода.
    return HTTPException(status_code=e.status, detail=e.reason)


def _helmrelease_spec(namespace: str, slug: str, plan: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "HelmRelease",
        "metadata": {"name": f"tenant-{slug}", "namespace": namespace},
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
            "values": {"env": {"plan": plan}},
        },
    }


def _status_dict(obj: dict) -> dict:
    status = obj.get("status", {})
    ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready"), None)
    return {
        "slug": obj["metadata"]["name"].removeprefix("tenant-"),
        "plan": obj.get("spec", {}).get("values", {}).get("env", {}).get("plan"),
        "ready": bool(ready and ready.get("status") == "True"),
        "message": ready.get("message") if ready else None,
    }


def _wait_ready(api: client.CustomObjectsApi, namespace: str, slug: str, generation: int, timeout: int = 60) -> dict:
    name = f"tenant-{slug}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = api.get_namespaced_custom_object(group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL, name=name)
        status = obj.get("status", {})
        ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready"), None)
        if ready and status.get("observedGeneration") == generation and ready["status"] == "True":
            return _status_dict(obj)
        time.sleep(2)
    raise HTTPException(504, f"{name} не стал Ready за {timeout}s")


@app.post("/tenants", status_code=201)
def create_tenant(body: dict, authorization: str | None = Header(None)):
    token = _require_token(authorization)
    namespace = _own_namespace()
    api = _api_for_token(token)
    slug, plan = body["slug"], body["plan"]
    try:
        obj = api.create_namespaced_custom_object(
            group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL,
            body=_helmrelease_spec(namespace, slug, plan),
        )
        return _wait_ready(api, namespace, slug, obj["metadata"]["generation"])
    except ApiException as e:
        raise _as_http(e)


@app.get("/tenants/{slug}")
def get_tenant(slug: str, authorization: str | None = Header(None)):
    token = _require_token(authorization)
    namespace = _own_namespace()
    api = _api_for_token(token)
    try:
        obj = api.get_namespaced_custom_object(group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL, name=f"tenant-{slug}")
        return _status_dict(obj)
    except ApiException as e:
        raise _as_http(e)


@app.patch("/tenants/{slug}")
def update_tenant(slug: str, body: dict, authorization: str | None = Header(None)):
    token = _require_token(authorization)
    namespace = _own_namespace()
    api = _api_for_token(token)
    patch = {"spec": {"values": {"env": {"plan": body["plan"]}}}}
    try:
        obj = api.patch_namespaced_custom_object(group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL, name=f"tenant-{slug}", body=patch)
        return _wait_ready(api, namespace, slug, obj["metadata"]["generation"])
    except ApiException as e:
        raise _as_http(e)


@app.delete("/tenants/{slug}", status_code=204)
def delete_tenant(slug: str, authorization: str | None = Header(None)):
    token = _require_token(authorization)
    namespace = _own_namespace()
    api = _api_for_token(token)
    try:
        api.delete_namespaced_custom_object(group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL, name=f"tenant-{slug}")
    except ApiException as e:
        raise _as_http(e)
