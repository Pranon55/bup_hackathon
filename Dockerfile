FROM python:3.11-slim

# Install system dependencies including CBC solver for PuLP
RUN apt-get update && apt-get install -y --no-install-recommends \
    coinor-cbc \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Set default port if not provided by Cloud Run / Render
ENV PORT=8080
EXPOSE 8080

# Run uvicorn on 0.0.0.0 and dynamically bind to $PORT
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
