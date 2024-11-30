FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    wget \
    fontconfig \
    libxrender1 \
    libx11-dev \
    x11-utils \
    wkhtmltopdf && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]