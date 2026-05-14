// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Disable is a thin sibling of enable. Both verbs share the same
// shape (single positional connector_id, --confirm, --json,
// --backplane) and identical request/response handling — the only
// per-verb difference is the URL suffix and the prompt prose, which
// the transitionParams struct in enable.go threads through.
//
// This file deliberately holds no logic of its own; the constructor
// is in enable.go alongside the shared transition machinery.
package connector
