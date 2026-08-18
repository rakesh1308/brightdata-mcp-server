FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Upgrade pip first to avoid any old resolver issues
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install dependencies with verbose output so build logs show any failures
RUN pip install --no-cache-dir -r requirements.txt 2>&1 | tail -20

# Verify fastmcp is importable BEFORE we commit to the rest of the build
RUN python -c "from mcp.server.fastmcp import FastMCP; print('FastMCP import OK')"

# Copy application code
COPY brightdata_mcp.py .

# Create non-root user for security
RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

# Expose HTTP port
EXPOSE 8080

# Default environment — HTTP transport for Zeabur (stateless, JSON responses)
ENV PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080 \
    MCP_PATH=/mcp \
    MCP_STATELESS=true

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

# Run the MCP server over HTTP
CMD ["python", "brightdata_mcp.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
