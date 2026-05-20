// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package connector implements the `meho connector` subcommand tree.
// This file (disable.go) is a thin sibling of enable.go. Both verbs
// share the same shape (single positional connector_id, --confirm,
// --json, --backplane) and identical request/response handling — the
// only per-verb difference is the URL suffix and the prompt prose,
// which the transitionParams struct in enable.go threads through.
//
// This file deliberately holds no logic of its own; the constructor
// is in enable.go alongside the shared transition machinery.
package connector
