FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libfreetype6 \
    libpng-dev \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Clonar desde GitHub
ARG GITHUB_REPO=https://github.com/fvegas82/HealthReport.git
ARG GITHUB_BRANCH=master
RUN git clone --depth 1 --branch ${GITHUB_BRANCH} ${GITHUB_REPO} /tmp/repo && \
    cp -r /tmp/repo/reporter/* /app/ && \
    rm -rf /tmp/repo

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Crear directorios necesarios
RUN mkdir -p /reports /glucose-reports

# Health check
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os; exit(0 if os.path.exists('/reports/healthhub/reports') else 1)" || exit 1

CMD ["python", "report.py"]
