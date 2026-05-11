# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Top-level Makefile. The repo is polyglot — Python backplane under
# `backend/`, Go CLI under `cli/`, Helm chart under `deploy/charts/`.
# Each surface has its own canonical toolchain (uv / go / helm), so
# this file only exposes thin delegates that route to the
# subproject's native build system. Don't expand it into a
# meta-orchestrator — let each surface evolve on its own cadence.

.PHONY: help cli cli-build cli-test cli-lint

help:
	@echo "Repo-level shortcuts (delegates to per-surface tooling):"
	@echo "  make cli         # build the meho CLI (cli/Makefile build)"
	@echo "  make cli-build   # alias for `make cli`"
	@echo "  make cli-test    # run cli/ tests"
	@echo "  make cli-lint    # run cli/ linters"
	@echo
	@echo "Backplane builds use uv directly — see backend/README.md."
	@echo "Chart builds use helm directly — see deploy/charts/meho/."

cli cli-build:
	$(MAKE) -C cli build

cli-test:
	$(MAKE) -C cli test

cli-lint:
	$(MAKE) -C cli lint
