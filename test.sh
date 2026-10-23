#!/bin/sh
# Run all unit tests (Go bridge + Python summarizer) in Docker.
set -e
cd "$(dirname "$0")"
echo "== bridge (Go)" && ./bridge/test.sh
echo "== summarizer (Python)" && ./summarizer/test.sh
