// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// applyProfile picks the named login for this command: the global
// --profile flag, else MEHO_PROFILE, else the default profile. It puts
// the name on the command context. Every place that reads or writes a
// login (login, the authed client, status --watch) takes the name from
// there, so each profile has its own stored login. Error hints that
// say "run `meho login`" name the profile too: a named profile always,
// the default profile only when MEHO_PROFILE names another one.
func applyProfile(cmd *cobra.Command) error {
	flagValue, changed := "", false
	if f := cmd.Root().PersistentFlags().Lookup("profile"); f != nil {
		flagValue, changed = f.Value.String(), f.Changed
	}
	name, err := auth.ResolveProfile(flagValue, changed, strings.TrimSpace(os.Getenv(auth.ProfileEnv)))
	if err != nil {
		jsonOut := false
		if f := cmd.Flags().Lookup("json"); f != nil {
			jsonOut = f.Value.String() == "true"
		}
		return output.RenderError(cmd.ErrOrStderr(), output.Usage(err.Error()), jsonOut)
	}
	cmd.SetContext(auth.WithProfile(cmd.Context(), name))
	switch {
	case name != auth.DefaultProfile:
		output.SetLoginProfile(name)
	case envNamesOtherProfile():
		output.SetLoginProfile(auth.DefaultProfile)
	default:
		output.SetLoginProfile("")
	}
	return nil
}

// envNamesOtherProfile reports whether MEHO_PROFILE names a profile
// other than the default one. Then a plain `meho login` fills that
// profile, so a hint for the default login must say --profile default.
func envNamesOtherProfile() bool {
	env := strings.TrimSpace(os.Getenv(auth.ProfileEnv))
	return env != "" && env != auth.DefaultProfile
}

// defaultLoginHints makes the "meho login" hints of a report that
// checked the default profile name it when MEHO_PROFILE names another
// profile.
func defaultLoginHints(r *doctorReport) {
	if !envNamesOtherProfile() {
		return
	}
	for i := range r.Checks {
		r.Checks[i].Detail = output.NameLoginProfile(r.Checks[i].Detail, auth.DefaultProfile)
		r.Checks[i].Hint = output.NameLoginProfile(r.Checks[i].Hint, auth.DefaultProfile)
	}
}

// printProfile writes the active profile as the first line of the
// human `meho status` output. The default profile prints nothing, so
// the output of a person who never uses profiles is exactly as before
// (scripts read it).
func printProfile(w io.Writer, cmd *cobra.Command) {
	if name := auth.ProfileFrom(cmd.Context()); name != auth.DefaultProfile {
		fmt.Fprintf(w, "Profile: %s\n", name)
	}
}
