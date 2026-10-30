// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package workref holds the --filter-work-ref helper for the read
// commands that list rows by change-ticket reference (audit query,
// approvals list, scheduler list, runbook runs, agent run-list,
// broadcast recent).
//
// The global --work-ref flag tags every backplane request with a
// change-ticket reference. These read commands used to define their
// own --work-ref that filtered the answer instead, and so hid the
// global flag. They now filter only with --filter-work-ref, and the
// global --work-ref tags their request like on every other command.
package workref

import (
	"fmt"

	"github.com/spf13/cobra"
)

// FilterFlag is the name of the flag that filters a list by
// change-ticket reference.
const FilterFlag = "filter-work-ref"

// OldFlagFilters reports whether a --work-ref on cmd still filters the
// answer: cmd has the --filter-work-ref flag, and the flag was not
// given on the command line. Then, for one release, --work-ref is the
// filter (see Filter). When --filter-work-ref was given, also empty,
// --work-ref only tags the request.
func OldFlagFilters(cmd *cobra.Command) bool {
	f := cmd.Flags().Lookup(FilterFlag)
	return f != nil && !f.Changed
}

// Filter returns the change-ticket filter for a read command.
//
// It returns filter (the --filter-work-ref value) when that flag was
// given on the command line, also when it was given empty
// (--filter-work-ref="" means: no filter). Otherwise, for one release,
// a --work-ref given on the command line still filters the answer, as
// it did before. The command then prints one line on stderr that names
// the new flag. The line never goes to stdout, so --json output stays
// clean. MEHO_WORK_REF never filters: it only tags the request.
//
// The fallback is for one release only. The release after it drops
// the fallback, so --work-ref only tags.
func Filter(cmd *cobra.Command, filter string) string {
	if !OldFlagFilters(cmd) {
		return filter
	}
	f := cmd.Flags().Lookup("work-ref")
	if f == nil || !f.Changed || f.Value.String() == "" {
		return ""
	}
	ref := f.Value.String()
	fmt.Fprintf(cmd.ErrOrStderr(),
		"warning: --work-ref filtered this answer to %s. Use --%s to filter; "+
			"in the next release --work-ref only tags the request.\n",
		ref, FilterFlag)
	return ref
}
