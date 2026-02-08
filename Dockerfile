# =============================================================================
# Alert Dashboard V2 - Multi-stage Docker build
# Targets: linux/amd64, linux/arm64 (Raspberry Pi 4/5)
# =============================================================================

# Stage 1: Build frontend with Vite
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit
COPY frontend/ ./
# Set base path for Caddy path-based routing (/v2/)
ENV VITE_BASE_PATH=/v2/
RUN npm run build

# Stage 2: Build Python dependencies (needs Rust + C compilers for ARM)
FROM python:3.12-slim AS python-build
WORKDIR /build

# Install build tools: C compiler, Rust (for slixmpp/cryptography), and lib headers
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    python3-dev \
    libgeos-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Rust (required by slixmpp on ARM - no prebuilt wheels)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Upgrade pip
RUN pip install --upgrade pip

# Install Python deps into a prefix we can copy to the runtime image
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 3: Slim runtime image (no compilers, no Rust)
FROM python:3.12-slim
WORKDIR /app

# Install only the runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-c1v5 \
    libjpeg62-turbo \
    zlib1g \
    libffi8 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=python-build /install /usr/local

# Copy backend code
COPY backend/ ./backend/

# Copy built frontend from stage 1
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Copy widgets (vanilla JS ticker overlays for OBS/streaming)
COPY widgets/ ./widgets/

# Create data directory for chase logs, alerts, etc.
RUN mkdir -p /app/data/chase_logs/radar

# Default environment (can be overridden by docker-compose or env_file)
ENV HOST=0.0.0.0
ENV PORT=3074
ENV DEBUG=false

EXPOSE 3074

CMD ["python", "backend/main.py"]
