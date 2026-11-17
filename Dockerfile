FROM node:22-slim AS frontend

WORKDIR /frontend

COPY terminals/frontend/package.json terminals/frontend/package-lock.json ./
RUN npm ci

COPY terminals/frontend/ ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TERMINALS_DATABASE_URL=sqlite+aiosqlite:////app/data/terminals.db
ENV TERMINALS_DOCKER_DATA_DIR=/app/data/terminals

# e2fsprogs (mkfs.ext4, resize2fs) and util-linux (mount, losetup, nsenter)
# power the home-quota helper; nsenter also lets the orchestrator run
# privileged one-shot helpers in the host mount namespace.
RUN apt-get update \
    && apt-get install -y --no-install-recommends e2fsprogs util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .
COPY --from=frontend /frontend/build /app/terminals/frontend/build

RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chgrp -R 0 /app/data \
    && chmod -R g=u /app/data

EXPOSE 3000

ENTRYPOINT ["terminals"]
CMD ["serve"]
