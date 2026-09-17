# sqas-demo-chart

Наш собственный onboarding-тулинг — не клиентский контент. Заводит и
сносит демо-клиентов (Forgejo-аккаунт, приватный репозиторий,
Nexus-доступ, K8s self-service) через `docs/onboarding/`.

То, что видит и деплоит сам клиент (чарт, CI, self-service API примеры)
— отдельный репозиторий,
[`saas-demo-provider`](https://git.hightps.online/showcase/saas-demo-provider)
(канонический источник для `onboard_client.py`).

См. [`docs/onboarding/README.md`](docs/onboarding/README.md).
