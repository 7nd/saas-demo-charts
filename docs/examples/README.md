# Examples — быстрый kubectl apply стенда

Готовые к применению манифесты, чтобы поднять одноразовый demo-стенд на
`*.demo.hightps.online`, не проходя через git/PR/ожидание Flux-синка.
Для того, как это делать программно (провижининг из бэкенда, а не руками)
— см. [../kubernetes-api-integration.md](../kubernetes-api-integration.md),
это его YAML/kubectl-эквивалент.

Оба варианта ниже полагаются на то, что на кластере уже настроено (см.
`infrastructure/apps/demo-stands` в инфраструктурном репозитории
unitum-demo-k8s-infra): namespace `demo` и `Secret/wildcard-demo-tls`
(`*.demo.hightps.online`). Сами эти манифесты туда не входят и живут
только здесь — предполагается, что применяет их человек, а не Flux.

## Вариант 1 — `helmrelease.yaml` (нужен Flux)

Самый ленивый: использует уже собранный чарт `charts/deployment-demo`
целиком (PVC, requests/limits, env-таблица на странице, всё как есть).
Требует, чтобы в кластере крутился `helm-controller` и был заведён
`GitRepository saas-demo-charts` — но НЕ требует, чтобы сам файл прошёл
через git этого репозитория: `HelmRelease` — обычный Custom Resource,
`helm-controller` подхватит его, откуда бы он ни появился в кластере.

```sh
# скопируй файл и замени оба "stand-example" на своё имя/хост, затем:
kubectl apply -f helmrelease.yaml
kubectl get helmrelease stand-example -n demo -w   # ждать READY=True
curl -s https://stand-example.demo.hightps.online/ | grep DEMO_PLAN

# снести:
kubectl delete -f helmrelease.yaml
```

## Вариант 2 — `raw-manifests.yaml` (без Flux/Helm вообще)

Голые `Deployment`/`Service`/`Ingress`. Не показывает requests/limits/PVC
на странице (это просто `nginx:alpine`), зато не требует вообще ничего,
кроме namespace `demo` и готового TLS-секрета — полезно, если под рукой
нет прав на `HelmRelease`, или просто нужно быстро проверить, что
ingress+wildcard-TLS живы.

```sh
kubectl apply -f raw-manifests.yaml
curl -s https://stand-example-raw.demo.hightps.online/ | head -5

kubectl delete -f raw-manifests.yaml
```

## Ещё ленивее — совсем без файла из этой папки

Если под рукой уже есть локальный клон этого репозитория и `helm` —
можно вообще не создавать `HelmRelease`, а поставить чарт напрямую (Flux
тут ни при чём, просто `helm` говорит с кластером сам):

```sh
helm upgrade --install stand-example ../../charts/deployment-demo -n demo \
  --set ingress.host=stand-example.demo.hightps.online \
  --set ingress.tls.enabled=true \
  --set ingress.tls.secretName=wildcard-demo-tls \
  --set env.plan=lazy-demo

helm uninstall stand-example -n demo
```

## На что обратить внимание

- **Имя стенда — это сразу и хост в DNS.** `external-dns` в этом
  кластере смотрит на все `Ingress` (`policy: sync`) и заведёт/уберёт
  CNAME автоматически вместе с созданием/удалением твоего `Ingress`
  (`demo.hightps.online` — единственное исключение, `external-dns` его
  не трогает, но сами стенды под ним — уже обычные хосты). Не оставляй
  такие стенды висеть просто так — это не GitOps, ничто не подчистит их
  за тебя.
- Оба варианта используют один и тот же `wildcard-demo-tls` секрет — это
  общий сертификат на весь `demo.hightps.online`, не персональный на
  стенд (см. предупреждения в
  [../kubernetes-api-integration.md](../kubernetes-api-integration.md)
  про rate limit и про то, что скомпрометированный ключ задевает всех
  сразу).
