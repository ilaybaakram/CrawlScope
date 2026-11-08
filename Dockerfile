# Optimized production Dockerfile for CrawlScope FastAPI backend
FROM python:3.11-slim

# Prevent Python from writing bytecode and enable unbuffered log output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Create a non-privileged user and group for container security
RUN addgroup --system --gid 1001 crawlscope && \
    adduser --system --uid 1001 --ingroup crawlscope crawlscope

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py crawler.py exporter.py ./

# Secure file permissions
RUN chown -R crawlscope:crawlscope /app

# Run as non-root user
USER crawlscope

EXPOSE 8000

# Periodic healthcheck using Python's built-in urllib
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", 8000)}/health')" || exit 1

# Launch uvicorn dynamically bound to $PORT for cloud PaaS compatibility
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
