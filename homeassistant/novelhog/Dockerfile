# syntax=docker/dockerfile:1

# The Home Assistant add-on image: the published novelhog image plus the add-on's startup script.
# Home Assistant cannot override a container's command, so the script is this image's entrypoint.
# The base is two arguments so a locally built image can stand in for the published one.
ARG BASE_IMAGE=ghcr.io/t3hk0d3/novelhog
ARG BASE_TAG=latest
FROM ${BASE_IMAGE}:${BASE_TAG}

# The Supervisor-managed /data is root-owned and the add-on container is already isolated.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 run.sh /run.sh

LABEL org.opencontainers.image.title="novelhog (Home Assistant add-on)" \
      org.opencontainers.image.description="Self-hosted web-novel downloader and reader" \
      org.opencontainers.image.source="https://github.com/t3hk0d3/novelhog"

# Setting ENTRYPOINT clears the base image's CMD, so run.sh starts with no arguments.
ENTRYPOINT ["tini", "--", "/run.sh"]
