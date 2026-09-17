# Тривиальный образ специально: цель — показать «отредактировал(-а) ->
# запушил(-а) -> CI сама собрала и задеплоила», а не какой-то конкретный
# рантайм. Правь index.html и пушь — дальше берёт на себя
# .forgejo/workflows/build.yml (см. docs/onboarding в этом репозитории).
FROM nginx:alpine
COPY index.html /usr/share/nginx/html/index.html
