#!/usr/bin/env bash
set -euo pipefail

# Prevent Git Bash (MSYS2) from converting Docker paths like /bin/bash
export MSYS2_ARG_CONV_EXCL='*'

# Build a fresh Lambda deployment package using Docker (Git Bash friendly)

ROOT_DIR="$(pwd)"
PACKAGE_DIR="$ROOT_DIR/package"
ZIP_PATH="$ROOT_DIR/deployment.zip"

echo "Cleaning old build artifacts..."
rm -rf "$PACKAGE_DIR" "$ZIP_PATH"

echo "Rebuilding package with Docker..."
docker run --rm -v "$ROOT_DIR":/var/task --entrypoint /bin/bash public.ecr.aws/lambda/python:3.13 \
  -c "pip install -r requirements.txt -t package && cp lambda_function.py tts.py podcast_feed.py prompt.txt package/"

echo "Creating deployment.zip..."
docker run --rm -v "$ROOT_DIR":/var/task --entrypoint /bin/bash public.ecr.aws/lambda/python:3.13 \
  -c "cd package && python -m zipfile -c /var/task/deployment.zip ."

echo "Done. Created deployment.zip"