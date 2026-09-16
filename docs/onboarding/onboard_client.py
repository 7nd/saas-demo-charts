#!/usr/bin/env python3
"""
Онбординг одного потенциального клиента.

Что делает (симметрично offboard_client.py):
  1. Forgejo-аккаунт client-<slug> + пустой репозиторий + разовый mirror-
     импорт канонического чарта (${CANONICAL_OWNER}/${CANONICAL_REPO}).
     Сам GitHub-репозиторий (основная разработка чарта) не трогаем —
     только копия для раздачи клиентам живёт на Forgejo.
  2. Nexus: пользователь клиента + СВОЙ hosted docker-репозиторий (не
     общий) со свободным портом + роль push только на этот репозиторий +
     read-only роль на общий docker-clients (демо-образы).
  3. Базовая инфра клиента (namespace/wildcard-TLS/registry pull-secret/
     GitRepository+HelmRelease его приложения/self-service RBAC/его
     персональный docker-хост в Nexus) — ОДНИМ HelmRelease
     client-infra-<slug> поверх чарта ops/client-infra. Тот же принцип,
     которым сами демонстрируем клиенту его SaaS: GitRepository+HelmRelease
     поверх чарта в git, не сырые манифесты. Создаётся императивно
     (kubectl/K8s API), в GitOps-дерево unitum-demo-k8s-infra не
     коммитится — сознательно развязано от корневого app-of-apps.
  4. K8s self-service токен на ServiceAccount, который завёл чарт.
  5. Печать сводки (URL стенда, Forgejo/Nexus/K8s-креды).

Предполагает уже настроенное на кластере — см. README.md ("Предварительно"):
  - отдельная Forgejo на git.${BASE_DOMAIN} + канонический чарт там же
  - GitRepository client-infra-chart в flux-system -> ops/client-infra
  - Certificate wildcard-docker (*.docker.${BASE_DOMAIN}) в namespace nexus
  - Nexus: репозиторий docker-clients + роль docker-shared-pull (read-only)

Требуемые переменные окружения:
  FORGEJO_ADMIN_TOKEN, NEXUS_ADMIN_USER, NEXUS_ADMIN_PASSWORD, KUBECONFIG

Использование:
  FORGEJO_ADMIN_TOKEN=... NEXUS_ADMIN_USER=... NEXUS_ADMIN_PASSWORD=... \
    ./onboard_client.py acme
"""
from __future__ import annotations

import secrets
import string
import sys

from onboarding_common import (
    Config,
    client_infra_helmrelease,
    apply_helmrelease,
    create_service_account_token,
    docker_config_json,
    forgejo_create_repo,
    forgejo_create_user,
    forgejo_mirror_import,
    forgejo_session,
    k8s_api_server,
    k8s_custom_api,
    next_free_docker_port,
    nexus_create_docker_hosted_repo,
    nexus_create_docker_push_role,
    nexus_create_user,
    validate_slug,
    wait_helmrelease_ready,
)

CLIENT_REPO = "sqas-demo-chart"


def random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: onboard_client.py <slug>")
    slug = validate_slug(sys.argv[1])
    cfg = Config.from_env()

    client_user = f"client-{slug}"
    namespace = f"{slug}-saas"
    stand_host = f"app.{slug}-saas.{cfg.base_domain}"
    docker_host = f"{slug}.docker.{cfg.base_domain}"
    shared_docker_host = f"docker.{cfg.base_domain}"
    docker_repo_name = f"docker-{slug}"
    docker_push_role = f"docker-{slug}-push"

    print(f"==> онбординг клиента: slug={slug} namespace={namespace}")

    # ── 1. Forgejo: аккаунт клиента + репозиторий + mirror-импорт ────────
    client_password = random_password()
    print(f"==> создаю Forgejo-аккаунт {client_user}")
    fg = forgejo_session(cfg)
    forgejo_create_user(cfg, fg, client_user, f"{slug}@clients.{cfg.base_domain}", client_password)

    print(f"==> создаю {client_user}/{CLIENT_REPO}")
    forgejo_create_repo(cfg, fg, client_user, CLIENT_REPO, private=False)

    print(f"==> импортирую {cfg.canonical_owner}/{cfg.canonical_repo} -> {client_user}/{CLIENT_REPO}")
    forgejo_mirror_import(cfg, client_user, client_password, CLIENT_REPO)

    # ── 2. Nexus: пользователь + СВОЙ docker-репозиторий ──────────────────
    print(f"==> завожу Nexus docker-репозиторий {docker_repo_name}")
    docker_port = next_free_docker_port(cfg)
    nexus_create_docker_hosted_repo(cfg, docker_repo_name, docker_port)
    nexus_create_docker_push_role(cfg, docker_repo_name, docker_push_role)

    print(f"==> завожу Nexus-пользователя {client_user}")
    nexus_password = random_password()
    nexus_create_user(
        cfg,
        client_user,
        slug,
        f"{slug}@clients.{cfg.base_domain}",
        nexus_password,
        roles=[docker_push_role, "docker-shared-pull"],
    )

    # ── 3. Базовая инфра клиента — один HelmRelease поверх ops/client-infra
    print(f"==> раскатываю HelmRelease client-infra-{slug}")
    docker_auth_json = docker_config_json(docker_host, client_user, nexus_password)
    client_repo_url = f"{cfg.forgejo_internal}/{client_user}/{CLIENT_REPO}.git"
    hr = client_infra_helmrelease(
        slug, cfg, docker_port=docker_port, docker_auth_json=docker_auth_json, client_repo_url=client_repo_url
    )
    api = k8s_custom_api()
    generation = apply_helmrelease(api, hr)
    wait_helmrelease_ready(api, f"client-infra-{slug}", generation)

    # ── 4. K8s self-service токен ───────────────────────────────────────
    k8s_token = create_service_account_token(namespace, duration=cfg.token_duration)
    k8s_server = k8s_api_server()

    # ── 5. Сводка ────────────────────────────────────────────────────────
    print(f"""
================================================================
Клиент "{slug}" готов.

Живой стенд:      https://{stand_host}/
Forgejo:           {cfg.forgejo_url}/{client_user}/{CLIENT_REPO}
  логин:           {client_user}
  пароль:          {client_password}

Docker registry (свой, изолирован от других клиентов):
  host:            {docker_host}
  логин:           {client_user}
  пароль:          {nexus_password}
  push:            docker push {docker_host}/<image>:<tag>
  pull:            docker pull {docker_host}/<image>:<tag>
  общие демо-образы (read-only): docker pull {shared_docker_host}/shared/<image>:<tag>
  imagePullSecret "registry-pull-secret" уже лежит в {namespace} — укажи
  values.imagePullSecrets: [{{name: registry-pull-secret}}] в своём HelmRelease,
  если решишь использовать свой образ вместо стокового nginx:alpine.

Kubernetes self-service (namespace {namespace}):
  K8S_API_SERVER={k8s_server}
  K8S_TOKEN={k8s_token}
  (токен на {cfg.token_duration}, перевыпуск: kubectl create token saas-provisioner -n {namespace} --duration=...)

Дальше клиент может:
  - пушить в свой Forgejo-репозиторий -> стенд обновится сам (см. helmrelease-cheatsheet.md)
  - создавать свои HelmRelease в {namespace} через K8S_TOKEN (examples/manage_tenant.py,
    поменять NAMESPACE="{namespace}" и BASE_DOMAIN="{slug}-saas.{cfg.base_domain}")
================================================================
""")


if __name__ == "__main__":
    main()
