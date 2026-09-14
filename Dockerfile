FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md license ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["python", "-m", "hefeng_qweather_mcp.cloud"]
