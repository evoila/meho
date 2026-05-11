# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# meho CLI build targets. Kept independent from the top-level
# Python backplane build — invoke via `make cli` from the repo root
# (which delegates here) or run targets directly from this directory.

.PHONY: help build test lint tidy install clean generate snapshot-openapi tools

# Version metadata gets baked into the binary via -ldflags -X.
# Override on the command line for release builds:
#   make build VERSION=v0.1.0 COMMIT=$(git rev-parse --short HEAD)
# When unset, the defaults below produce a build identifiable as
# locally-produced (VERSION=dev) but pinned to the current commit.
VERSION    ?= dev
COMMIT     ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
DATE       ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

MODULE     := github.com/evoila/meho/cli
LDFLAGS    := -X $(MODULE)/internal/version.Version=$(VERSION) \
              -X $(MODULE)/internal/version.Commit=$(COMMIT) \
              -X $(MODULE)/internal/version.Date=$(DATE)

# Strip the symbol table (-s) and DWARF debug info (-w) from
# release-style binaries; matches the convention used by gh, argocd,
# flux. Adds ~30% to the binary's shrink ratio without affecting
# panics (Go's runtime emits PC-only stack traces).
GOFLAGS    := -trimpath
GO_LDFLAGS := -s -w $(LDFLAGS)

BIN_DIR    := bin
BINARY     := $(BIN_DIR)/meho

# oapi-codegen pinned to v2.5.0 — the last v2 release that still
# supports Go 1.22 as the minimum (v2.6.0+ bumps the module go
# directive to 1.24). The runtime dep
# (github.com/oapi-codegen/runtime) is pinned in go.mod at a version
# compatible with this generator.
OAPI_CODEGEN_VERSION := v2.5.0
OAPI_CODEGEN         := $(BIN_DIR)/oapi-codegen

# Backplane source directory (relative to cli/). Used by
# snapshot-openapi to drive the FastAPI app and re-emit
# api/openapi.json.
BACKEND_DIR := ../backend

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

build: ## Build the meho binary into bin/.
	@mkdir -p $(BIN_DIR)
	go build $(GOFLAGS) -ldflags "$(GO_LDFLAGS)" -o $(BINARY) ./cmd/meho

test: ## Run the test suite with race detector + coverage.
	go test -race -cover ./...

lint: ## Run golangci-lint with the project's config.
	golangci-lint run

tidy: ## Sync go.mod / go.sum with imports.
	go mod tidy

install: ## Install meho into $$(go env GOBIN) (or $$GOPATH/bin).
	go install $(GOFLAGS) -ldflags "$(GO_LDFLAGS)" ./cmd/meho

clean: ## Remove build artefacts (keeps oapi-codegen under bin/).
	rm -rf dist/ $(BINARY)

tools: $(OAPI_CODEGEN) ## Install local code-gen tooling under bin/.

$(OAPI_CODEGEN):
	@mkdir -p $(BIN_DIR)
	GOBIN=$(CURDIR)/$(BIN_DIR) go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@$(OAPI_CODEGEN_VERSION)

generate: tools ## Regenerate the typed API client from api/openapi.json.
	$(OAPI_CODEGEN) --config api/oapi.config.yaml api/openapi.json

snapshot-openapi: ## Re-snapshot api/openapi.json from the running backplane source.
	@command -v uv >/dev/null 2>&1 || { echo "snapshot-openapi requires uv (see backend/README.md)"; exit 2; }
	cd $(BACKEND_DIR) && uv run python3 $(CURDIR)/api/snapshot-openapi.py --out $(CURDIR)/api/openapi.json
