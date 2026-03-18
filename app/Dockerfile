# syntax=docker/dockerfile:1

# Use specific version for reproducibility
FROM python:3.11-slim AS base

# Metadata
LABEL maintainer="maheshbabugorantla@gmail.com" \
      description="LLM Recipe Generator with OpenLLMetry observability" \
      version="1.0.0"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# Set working directory
WORKDIR /app

# Install system dependencies in a single layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Create non-root user for security
RUN groupadd -r appuser && \
    useradd -r -g appuser -u 1000 -m -s /sbin/nologin appuser && \
    chown -R appuser:appuser /app

# Copy requirements as appuser
COPY --chown=appuser:appuser requirements.txt .

# Install Python dependencies with mount cache for better performance
# This uses BuildKit cache mount to speed up rebuilds
RUN --mount=type=cache,target=/root/.cache/pip,uid=0 \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=appuser:appuser . .

# Switch to non-root user
USER appuser

# Expose Flask port
EXPOSE 5000

# Run the application with proper signal handling
CMD ["python", "-u", "app.py"]
