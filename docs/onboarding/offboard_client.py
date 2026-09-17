#!/usr/bin/env python3
"""
Офбординг клиента — симметрично onboard_client.py, полный снос без
архивирования (namespace, docker-образы клиента и т.д. теряются).

Порядок важен:
  1. Удалить HelmRelease client-infra-<slug> и ДОЖДАТЬСЯ его реального
     исчезновения — helm-controller каскадно сносит всё, что чарт создал
     (namespace, certificate, secret, GitRepository/HelmRelease
     клиентского приложения, RBAC, персональный docker-хост в Nexus).
     Ждём, а не просто отправляем delete: иначе можно снести Nexus/Forgejo-
     креды клиента раньше, чем реально удалятся ресурсы, которые их
     использовали.
  2. Nexus: удалить docker-репозиторий клиента + push-роль + пользователя.
  3. Forgejo: удалить репозиторий клиента (ДО аккаунта — Forgejo не даёт
     удалить пользователя, пока за ним есть репозиторий, "user still has
     ownership of repositories", 422), затем аккаунт. Удаление репозитория
     каскадно уносит и его repo-scoped CI-раннера/секреты — отдельного API
     на удаление раннера в этой версии Forgejo нет (только UI), поэтому
     явного шага здесь тоже нет.

Требуемые переменные окружения — те же, что у onboard_client.py.

Использование:
  FORGEJO_ADMIN_TOKEN=... NEXUS_ADMIN_USER=... NEXUS_ADMIN_PASSWORD=... \
    ./offboard_client.py acme
"""
from __future__ import annotations

import sys

from onboarding_common import (
    Config,
    delete_helmrelease,
    forgejo_delete_repo,
    forgejo_delete_user,
    forgejo_session,
    k8s_custom_api,
    nexus_delete_repo,
    nexus_delete_role,
    nexus_delete_user,
    validate_slug,
    wait_helmrelease_gone,
)

CLIENT_REPO = "saas-demo-provider"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: offboard_client.py <slug>")
    slug = validate_slug(sys.argv[1])
    cfg = Config.from_env()

    client_user = f"client-{slug}"
    docker_repo_name = f"docker-{slug}"
    docker_push_role = f"docker-{slug}-push"

    print(f"==> офбординг клиента: slug={slug}")

    print(f"==> удаляю HelmRelease client-infra-{slug} и жду каскадного сноса")
    api = k8s_custom_api()
    delete_helmrelease(api, f"client-infra-{slug}")
    wait_helmrelease_gone(api, f"client-infra-{slug}")

    print(f"==> удаляю Nexus-репозиторий {docker_repo_name} и роль {docker_push_role}")
    nexus_delete_repo(cfg, docker_repo_name)
    nexus_delete_role(cfg, docker_push_role)

    print(f"==> удаляю Nexus-пользователя {client_user}")
    nexus_delete_user(cfg, client_user)

    fg = forgejo_session(cfg)
    print(f"==> удаляю репозиторий {client_user}/{CLIENT_REPO}")
    forgejo_delete_repo(cfg, fg, client_user, CLIENT_REPO)

    print(f"==> удаляю Forgejo-аккаунт {client_user}")
    forgejo_delete_user(cfg, fg, client_user)

    print(f"==> готово: {slug} снесён")


if __name__ == "__main__":
    main()
