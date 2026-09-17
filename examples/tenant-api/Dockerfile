# Собирается и деплоится сама — CI (.forgejo/workflows/tenant-api.yml,
# триггер на examples/tenant-api/**), не руками. См. ../README.md.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" "kubernetes>=29,<32"
COPY tenant_api.py .
EXPOSE 8080
CMD ["uvicorn", "tenant_api:app", "--host", "0.0.0.0", "--port", "8080"]
