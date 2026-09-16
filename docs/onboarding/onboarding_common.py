"""
Общие хелперы для onboard_client.py / offboard_client.py.

Почему Python, а не bash: `/bin/bash` 3.2 на macOS ломает командную
подстановку `$(python3 -c ...)`, когда она не единственная правая часть
присваивания переменной — раньше приходилось обходить это отдельными
строками `VAR="$(...)"`. Здесь этот класс багов просто не существует —
JSON/YAML собираются обычными Python-структурами.

Базовая инфра клиента (namespace/certificate/pull-secret/его собственный
GitRepository+HelmRelease/RBAC/персональный docker-хост в Nexus)
раскатывается ЧЕРЕЗ ОДИН HelmRelease (чарт `ops/client-infra` на
git.${BASE_DOMAIN}) — тем же способом, каким демонстрируем клиенту его
SaaS (GitRepository+HelmRelease поверх чарта в git, не сырые манифесты).
Подробности — README.md рядом.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

HR_GROUP, HR_VERSION, HR_PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"
CLIENT_INFRA_NS = "flux-system"


@dataclass
class Config:
    base_domain: str
    forgejo_url: str
    forgejo_internal: str
    nexus_url: str
    canonical_owner: str
    canonical_repo: str
    token_duration: str
    forgejo_admin_token: str
    nexus_admin_user: str
    nexus_admin_password: str

    @classmethod
    def from_env(cls) -> "Config":
        import os

        base_domain = os.environ.get("BASE_DOMAIN", "hightps.online")

        def required(name: str) -> str:
            val = os.environ.get(name)
            if not val:
                sys.exit(f"export {name}")
            return val

        return cls(
            base_domain=base_domain,
            forgejo_url=os.environ.get("FORGEJO_URL", f"https://git.{base_domain}"),
            forgejo_internal=os.environ.get(
                "FORGEJO_INTERNAL", "http://forgejo-http.forgejo.svc.cluster.local:3000"
            ),
            nexus_url=os.environ.get("NEXUS_URL", f"https://nexus.{base_domain}"),
            canonical_owner=os.environ.get("CANONICAL_OWNER", "showcase"),
            canonical_repo=os.environ.get("CANONICAL_REPO", "sqas-demo-chart"),
            token_duration=os.environ.get("TOKEN_DURATION", "720h"),
            forgejo_admin_token=required("FORGEJO_ADMIN_TOKEN"),
            nexus_admin_user=required("NEXUS_ADMIN_USER"),
            nexus_admin_password=required("NEXUS_ADMIN_PASSWORD"),
        )


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        sys.exit("slug должен быть DNS-safe: строчные латинские буквы/цифры/дефис, до 32 симв.")
    return slug


# ── Forgejo ──────────────────────────────────────────────────────────────


def forgejo_session(cfg: Config) -> requests.Session:
    s = requests.Session()
    s.headers["Authorization"] = f"token {cfg.forgejo_admin_token}"
    return s


def forgejo_create_user(cfg: Config, s: requests.Session, username: str, email: str, password: str) -> None:
    r = s.post(
        f"{cfg.forgejo_url}/api/v1/admin/users",
        json={"username": username, "email": email, "password": password, "must_change_password": False},
    )
    r.raise_for_status()


def forgejo_create_repo(cfg: Config, s: requests.Session, owner: str, repo: str, *, private: bool = False) -> None:
    r = s.post(
        f"{cfg.forgejo_url}/api/v1/admin/users/{owner}/repos",
        json={"name": repo, "private": private, "auto_init": False},
    )
    r.raise_for_status()


def forgejo_delete_repo(cfg: Config, s: requests.Session, owner: str, repo: str) -> None:
    r = s.delete(f"{cfg.forgejo_url}/api/v1/repos/{owner}/{repo}")
    if r.status_code not in (204, 404):
        r.raise_for_status()


def forgejo_delete_user(cfg: Config, s: requests.Session, username: str) -> None:
    r = s.delete(f"{cfg.forgejo_url}/api/v1/admin/users/{username}")
    if r.status_code not in (204, 404):
        r.raise_for_status()


def forgejo_mirror_import(cfg: Config, client_user: str, client_password: str, client_repo: str) -> None:
    """Разовый mirror-импорт канонического чарта в репозиторий клиента (НЕ живой fork/sync)."""
    workdir = tempfile.mkdtemp(prefix="onboard-mirror-")
    try:
        src = f"{cfg.forgejo_url}/{cfg.canonical_owner}/{cfg.canonical_repo}.git"
        dst_host = cfg.forgejo_url.split("://", 1)[1]
        dst = f"https://{client_user}:{client_password}@{dst_host}/{client_user}/{client_repo}.git"
        bare = f"{workdir}/repo.git"
        subprocess.run(["git", "clone", "--mirror", "--quiet", src, bare], check=True)
        subprocess.run(["git", "-C", bare, "push", "--mirror", "--quiet", dst], check=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── Nexus ────────────────────────────────────────────────────────────────


def nexus_auth(cfg: Config) -> tuple[str, str]:
    return (cfg.nexus_admin_user, cfg.nexus_admin_password)


def next_free_docker_port(cfg: Config, *, start: int = 5001) -> int:
    """max(httpPort) + 1 среди уже созданных docker hosted-репозиториев.

    Нет bulk-эндпоинта, отдающего httpPort сразу для всех репозиториев —
    список репозиториев отдаёт только name/format/type, порт есть только
    в GET одного конкретного репозитория. Количество клиентов на demo/trial
    масштабе (десятки) делает N+1 запросов не проблемой.
    """
    resp = requests.get(f"{cfg.nexus_url}/service/rest/v1/repositories", auth=nexus_auth(cfg))
    resp.raise_for_status()
    used_ports = []
    for repo in resp.json():
        if repo.get("format") != "docker" or repo.get("type") != "hosted":
            continue
        detail = requests.get(
            f"{cfg.nexus_url}/service/rest/v1/repositories/docker/hosted/{repo['name']}",
            auth=nexus_auth(cfg),
        )
        detail.raise_for_status()
        port = detail.json().get("docker", {}).get("httpPort")
        if port:
            used_ports.append(port)
    return max(used_ports, default=start - 1) + 1


def nexus_create_docker_hosted_repo(cfg: Config, name: str, port: int) -> None:
    r = requests.post(
        f"{cfg.nexus_url}/service/rest/v1/repositories/docker/hosted",
        auth=nexus_auth(cfg),
        json={
            "name": name,
            "online": True,
            "storage": {"blobStoreName": "default", "strictContentTypeValidation": True, "writePolicy": "ALLOW"},
            "docker": {"v1Enabled": False, "forceBasicAuth": True, "httpPort": port},
        },
    )
    r.raise_for_status()


def nexus_delete_repo(cfg: Config, name: str) -> None:
    r = requests.delete(f"{cfg.nexus_url}/service/rest/v1/repositories/{name}", auth=nexus_auth(cfg))
    if r.status_code not in (204, 404):
        r.raise_for_status()


def nexus_create_docker_push_role(cfg: Config, repo_name: str, role_id: str) -> None:
    r = requests.post(
        f"{cfg.nexus_url}/service/rest/v1/security/roles",
        auth=nexus_auth(cfg),
        json={
            "id": role_id,
            "name": role_id,
            "privileges": [f"nx-repository-view-docker-{repo_name}-{p}" for p in ("browse", "read", "add", "edit")],
            "roles": [],
        },
    )
    r.raise_for_status()


def nexus_delete_role(cfg: Config, role_id: str) -> None:
    r = requests.delete(f"{cfg.nexus_url}/service/rest/v1/security/roles/{role_id}", auth=nexus_auth(cfg))
    if r.status_code not in (204, 404):
        r.raise_for_status()


def nexus_create_user(cfg: Config, user_id: str, first_name: str, email: str, password: str, roles: list[str]) -> None:
    r = requests.post(
        f"{cfg.nexus_url}/service/rest/v1/security/users",
        auth=nexus_auth(cfg),
        json={
            "userId": user_id,
            "firstName": first_name,
            "lastName": "client",
            "emailAddress": email,
            "password": password,
            "status": "active",
            "source": "default",
            "roles": roles,
        },
    )
    r.raise_for_status()


def nexus_delete_user(cfg: Config, user_id: str) -> None:
    r = requests.delete(f"{cfg.nexus_url}/service/rest/v1/security/users/{user_id}", auth=nexus_auth(cfg))
    if r.status_code not in (204, 404):
        r.raise_for_status()


def docker_config_json(host: str, username: str, password: str) -> str:
    return json.dumps({"auths": {host: {"username": username, "password": password}}})


# ── Kubernetes: HelmRelease client-infra-<slug> ─────────────────────────


def k8s_custom_api() -> client.CustomObjectsApi:
    config.load_kube_config()
    return client.CustomObjectsApi()


def client_infra_helmrelease(
    slug: str, cfg: Config, *, docker_port: int, docker_auth_json: str, client_repo_url: str
) -> dict:
    return {
        "apiVersion": f"{HR_GROUP}/{HR_VERSION}",
        "kind": "HelmRelease",
        "metadata": {"name": f"client-infra-{slug}", "namespace": CLIENT_INFRA_NS},
        "spec": {
            "interval": "30m",
            "chart": {
                "spec": {
                    "chart": "charts/client-infra",
                    "sourceRef": {"kind": "GitRepository", "name": "client-infra-chart", "namespace": "flux-system"},
                    "reconcileStrategy": "Revision",
                }
            },
            "install": {"remediation": {"retries": 3}},
            "upgrade": {"remediation": {"retries": 3}},
            "values": {
                "slug": slug,
                "baseDomain": cfg.base_domain,
                "nexusDockerPort": docker_port,
                "dockerAuthJson": docker_auth_json,
                "clientRepoURL": client_repo_url,
            },
        },
    }


def apply_helmrelease(api: client.CustomObjectsApi, body: dict) -> int:
    name = body["metadata"]["name"]
    namespace = body["metadata"]["namespace"]
    try:
        obj = api.create_namespaced_custom_object(
            group=HR_GROUP, version=HR_VERSION, namespace=namespace, plural=HR_PLURAL, body=body
        )
    except ApiException as e:
        if e.status != 409:
            raise
        obj = api.patch_namespaced_custom_object(
            group=HR_GROUP, version=HR_VERSION, namespace=namespace, plural=HR_PLURAL, name=name, body=body
        )
    return obj["metadata"]["generation"]


def wait_helmrelease_ready(api: client.CustomObjectsApi, name: str, generation: int, *, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = api.get_namespaced_custom_object(
            group=HR_GROUP, version=HR_VERSION, namespace=CLIENT_INFRA_NS, plural=HR_PLURAL, name=name
        )
        status = obj.get("status", {})
        ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready"), None)
        if ready and status.get("observedGeneration") == generation:
            if ready["status"] == "True":
                return
            if ready["status"] == "False" and "retries exhausted" in ready.get("message", ""):
                raise RuntimeError(f"HelmRelease/{name} не смог примениться: {ready['message']}")
        time.sleep(3)
    raise TimeoutError(f"HelmRelease/{name} не стал Ready за {timeout}s")


def delete_helmrelease(api: client.CustomObjectsApi, name: str) -> None:
    try:
        api.delete_namespaced_custom_object(
            group=HR_GROUP, version=HR_VERSION, namespace=CLIENT_INFRA_NS, plural=HR_PLURAL, name=name
        )
    except ApiException as e:
        if e.status != 404:
            raise


def wait_helmrelease_gone(api: client.CustomObjectsApi, name: str, *, timeout: int = 180) -> None:
    """helm-controller снимает finalizer только после helm uninstall — дожидаемся реального исчезновения объекта,
    иначе Nexus/Forgejo-креды клиента можно снести раньше, чем реально удалятся ресурсы, которые их использовали."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api.get_namespaced_custom_object(
                group=HR_GROUP, version=HR_VERSION, namespace=CLIENT_INFRA_NS, plural=HR_PLURAL, name=name
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(3)
    raise TimeoutError(f"HelmRelease/{name} не удалился за {timeout}s")


# ── Kubernetes: self-service токен ──────────────────────────────────────


def create_service_account_token(namespace: str, *, duration: str) -> str:
    out = subprocess.run(
        ["kubectl", "create", "token", "saas-provisioner", "-n", namespace, f"--duration={duration}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def k8s_api_server() -> str:
    out = subprocess.run(
        ["kubectl", "config", "view", "--minify", "--raw", "-o", "jsonpath={.clusters[0].cluster.server}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()
