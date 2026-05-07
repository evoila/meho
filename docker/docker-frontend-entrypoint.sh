#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Frontend container entrypoint:
# 1. Render nginx.conf from template with envsubst (explicit var list preserves nginx's own $uri, $host, etc.)
# 2. Render config.js from template with envsubst (no allowlist — template only contains frontend vars)
# 3. exec nginx so SIGTERM reaches it directly

set -e

echo "Rendering nginx.conf from template..."
envsubst '$ALLOWED_ORIGINS $KEYCLOAK_ORIGIN' \
  < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

echo "Rendering config.js from template..."
envsubst \
  < /usr/share/nginx/html/config.js.template \
  > /usr/share/nginx/html/config.js

echo "Starting nginx..."
exec "$@"
