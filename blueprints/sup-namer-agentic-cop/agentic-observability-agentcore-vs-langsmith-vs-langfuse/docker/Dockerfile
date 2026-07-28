FROM public.ecr.aws/docker/library/python:3.12-slim

# System deps (git is needed for some Python wheels that pull from VCS).
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so we benefit from Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code.
COPY app/ .

# Run as non-root.
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Default service name (overridable by the CDK WorkloadStack Deployment env).
ARG SERVICE_NAME=langgraph-shopping-agent
ENV OTEL_PYTHON_DISTRO=aws_distro \
    OTEL_PYTHON_CONFIGURATOR=aws_configurator \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=otlp \
    OTEL_SERVICE_NAME=${SERVICE_NAME} \
    OTEL_RESOURCE_ATTRIBUTES="service.name=${SERVICE_NAME}" \
    AGENT_OBSERVABILITY_ENABLED=true

# NOTE: LangSmith and Langfuse env vars (project, API keys, hosts) are injected
# by the CDK WorkloadStack Deployment at deploy time — not baked into the image.

EXPOSE 8000

# Container-level health check hitting the app's /health endpoint. Uses only the
# Python standard library already in the image (no curl / extra dependency).
# Kubernetes uses its own liveness/readiness probes (see the CDK WorkloadStack);
# this HEALTHCHECK makes the image self-describing for plain `docker run` too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else 1)"]

# `opentelemetry-instrument` wires up the AWS Distro auto-instrumentation
# before the Python interpreter loads the app.
CMD ["opentelemetry-instrument", "python", "app.py"]
