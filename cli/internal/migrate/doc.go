// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package migrate contains the pure-logic helpers for the G5.3 laptop-local
// memory migration flow (Initiative #375). This package is imported only by
// cli/internal/cmd/migrate/ — never by cli/internal/cmd directly — so no
// import cycle is introduced (the cycle the kb.go header documents only
// applies to a shared HTTP helper imported from both root and a per-tree
// package).
//
// T1 (#608) declares this package as a placeholder; flow helpers (scanner,
// picker, submit client, mark-migrated writer) land in T2–T5 (#609–#612).
package migrate
