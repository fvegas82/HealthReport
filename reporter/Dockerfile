FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias de sistema para matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY report.py .

# Crear directorio de salida
RUN mkdir -p /reports

CMD ["python", "report.py"]
