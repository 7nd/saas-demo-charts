# HelmRelease — шпаргалка

Команды на каждый день для `helmrelease.yaml` (стенд `stand-example` в
namespace `demo`). Замени `stand-example` на реальное имя своего стенда
везде ниже. Общая логика и предупреждения — в [README.md](README.md) рядом
и в [../kubernetes-api-integration.md](../kubernetes-api-integration.md).

## Создать

```sh
kubectl apply -f helmrelease.yaml
```

`HelmRelease` появляется в API мгновенно, но это не значит, что под уже
поднят — `helm install` идёт асинхронно, см. «Проверить» ниже.

## Проверить

Быстрый статус (`READY`/`STATUS` одной строкой):

```sh
kubectl get helmrelease stand-example -n demo
```

Следить за изменением статуса вживую:

```sh
kubectl get helmrelease stand-example -n demo -w
```

Полный `status.conditions` + связанные события — если `READY` не `True`
дольше пары минут, ответ почти всегда здесь:

```sh
kubectl describe helmrelease stand-example -n demo
```

Хронология по факту (Pod/ReplicaSet/PVC/Helm) — полезно, когда
`describe` намекает, но не показывает первопричину:

```sh
kubectl get events -n demo --sort-by=.lastTimestamp | grep stand-example
```

Сырые логи helm-controller, если и события не объясняют:

```sh
kubectl -n flux-system logs deploy/helm-controller | grep stand-example
```

Сам под и что он реально отдаёт:

```sh
kubectl get pods -n demo -l app.kubernetes.io/instance=stand-example
curl -s https://stand-example.demo.hightps.online/ | grep DEMO_PLAN
```

Форсировать реконсиляцию, не дожидаясь `spec.interval` (5m в примере):

```sh
flux reconcile helmrelease stand-example -n demo --with-source
```

## Патчить

Три равноценных способа поменять `spec.values` — выбирай по ситуации.

**Точечный патч** (не трогает остальную спеку, JSON merge patch) — самый
безопасный вариант для одного значения:

```sh
kubectl patch helmrelease stand-example -n demo --type merge \
  -p '{"spec":{"values":{"env":{"plan":"enterprise"}}}}'
```

**Интерактивно** — открыть в `$EDITOR`, поправить руками, сохранить:

```sh
kubectl edit helmrelease stand-example -n demo
```

**Из файла** — поправить `helmrelease.yaml` (например, сменить
`ingress.host` или `values.env`) и переприменить:

```sh
kubectl apply -f helmrelease.yaml
```

После любого из трёх — `spec.generation` увеличился, но `helm upgrade`
снова асинхронный. Дождаться нужно именно этой версии, а не старого
`Ready=True`:

```sh
kubectl get helmrelease stand-example -n demo \
  -o jsonpath='{.metadata.generation} {.status.observedGeneration} {.status.conditions[?(@.type=="Ready")].status}{"\n"}'
```

Три числа/значения должны совпасть: `generation == observedGeneration` и
`Ready == True`. Если `generation` больше `observedGeneration` —
`helm-controller` ещё не добрался до твоего патча, это не зависший
апгрейд, а просто ещё не начавшийся.

## Удалить

```sh
kubectl delete -f helmrelease.yaml
# или, что то же самое:
kubectl delete helmrelease stand-example -n demo
```

`HelmRelease` создаётся с финалайзером — `helm-controller` успевает
выполнить `helm uninstall` до того, как объект реально пропадёт из
etcd. Одна команда убирает не только `HelmRelease`, но и всё, что чарт
создал (`Deployment`, `Service`, `Ingress`, `PVC`, историю релиза).
Убедиться, что ничего не осталось:

```sh
kubectl get all,pvc -n demo -l app.kubernetes.io/instance=stand-example
```

Пустой вывод — норма. `Secret/wildcard-demo-tls` при этом никуда не
девается — это общий ресурс на namespace, не принадлежит удалённому
стенду.
