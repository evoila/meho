// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import "strings"

// loginProfile is the named profile of this CLI run, or "" for the
// default profile. One process runs one command, so one value is
// enough; the root command sets it before any command runs.
var loginProfile string

// SetLoginProfile records the named profile of this CLI run, so every
// "run `meho login ...`" hint names it. Pass "" for the default
// profile: then the hints stay as they were.
func SetLoginProfile(name string) {
	loginProfile = name
}

// withLoginProfile returns err with each "meho login" in its detail
// turned into "meho --profile <name> login" when a named profile is
// active. Following the hint then fills that profile's login, not the
// default one. Every command renders its errors through RenderError,
// so this one place covers them all.
func withLoginProfile(err *StructuredError) *StructuredError {
	if loginProfile == "" || !strings.Contains(err.Detail, "meho login") {
		return err
	}
	return &StructuredError{
		Code:   err.Code,
		Detail: strings.ReplaceAll(err.Detail, "meho login", "meho --profile "+loginProfile+" login"),
		Exit:   err.Exit,
	}
}
