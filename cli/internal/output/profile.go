// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import "strings"

// loginProfile is the profile that the "meho login" hints of this CLI
// run must name, or "" when a plain `meho login` already uses the
// right profile. One process runs one command, so one value is
// enough; the root command sets it before any command runs.
var loginProfile string

// SetLoginProfile records the profile that every "run `meho login ...`"
// hint of this CLI run names. Pass "" to keep the hints as they were.
func SetLoginProfile(name string) {
	loginProfile = name
}

// NameLoginProfile returns text with each "meho login" turned into
// "meho --profile <name> login". An empty name returns text unchanged.
func NameLoginProfile(text, name string) string {
	if name == "" {
		return text
	}
	return strings.ReplaceAll(text, "meho login", "meho --profile "+name+" login")
}

// withLoginProfile returns err with each "meho login" in its detail
// turned into "meho --profile <name> login" when SetLoginProfile set a
// name. Following the hint then fills that profile's login, not
// another one. Every command renders its errors through RenderError,
// so this one place covers them all.
func withLoginProfile(err *StructuredError) *StructuredError {
	if loginProfile == "" || !strings.Contains(err.Detail, "meho login") {
		return err
	}
	return &StructuredError{
		Code:   err.Code,
		Detail: NameLoginProfile(err.Detail, loginProfile),
		Exit:   err.Exit,
	}
}
