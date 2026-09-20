# PostgreSQL 16 + pgvector for PaperIntel local development.
# Docker Hub is unreachable in this environment; the base image comes from
# public.ecr.aws and pgvector is installed from Debian bookworm packages.
FROM public.ecr.aws/docker/library/postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
