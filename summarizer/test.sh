#!/bin/sh
# Run summarizer unit tests inside Docker (stdlib unittest, no dependencies).
cd "$(dirname "$0")" && exec docker run --rm -u "$(id -u):$(id -g)" \
  -v "$PWD":/app:ro -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  python:3.12-slim python -m unittest "$@"
