#!/bin/sh
# Run Go tests inside Docker (no local Go toolchain needed).
cd "$(dirname "$0")" && exec docker run --rm -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e GOCACHE=/cache/build -e GOPATH=/cache/gopath \
  -v szalona-beata-gocache:/cache -v "$PWD":/src -w /src \
  golang:1.26-bookworm go test "$@" ./...
