FROM python:3.14-slim

ARG OBSCURA_VERSION=v0.1.9

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ── Layer 1: system deps (cached until apt packages change) ──────────────────
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    libpq-dev \
    libffi-dev \
    curl \
    libcairo2 \
    libcairo2-dev \
    libpango-1.0-0 \
    libpango1.0-dev \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libgdk-pixbuf-2.0-dev \
    tar \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) obscura_asset="obscura-x86_64-linux.tar.gz" ;; \
      arm64) obscura_asset="obscura-aarch64-linux.tar.gz" ;; \
      *) echo "Unsupported architecture for Obscura: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/h4ckf0r0day/obscura/releases/download/${OBSCURA_VERSION}/${obscura_asset}" -o /tmp/obscura.tar.gz; \
    tar -xzf /tmp/obscura.tar.gz -C /tmp; \
    install -m 0755 /tmp/obscura /usr/local/bin/obscura; \
    if [ -f /tmp/obscura-worker ]; then install -m 0755 /tmp/obscura-worker /usr/local/bin/obscura-worker; fi; \
    rm -f /tmp/obscura.tar.gz /tmp/obscura /tmp/obscura-worker

# ── Layer 2: Python deps (cached until requirements.txt changes) ──────────────
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Layer 3: Node deps (cached until package-lock.json changes) ───────────────
# Copy ONLY the package files so this layer is not invalidated by code changes.
COPY theme/static_src/package.json theme/static_src/package-lock.json /app/theme/static_src/
RUN cd /app/theme/static_src && npm ci

# ── Layer 4: app code + Tailwind build (invalidated on any code change) ───────
# npm run build must come after COPY so Tailwind scans the actual templates.
COPY . /app/
RUN cd /app/theme/static_src && npm run build

ENV DJANGO_SETTINGS_MODULE=config.settings

RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
