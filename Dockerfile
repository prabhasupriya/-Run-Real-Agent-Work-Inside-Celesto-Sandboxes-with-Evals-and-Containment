FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Celesto CLI/SDK is only needed to drive real guests. The ledger API works without it,
# so a failed install must not break the build (mock executor still works).
RUN pip install --no-cache-dir celesto || echo "celesto not installed: mock executor only"

COPY . .
ENV API_PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${API_PORT:-8080}"]