// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// installUsageErrors makes a wrong command line print an error for
// every command in the tree under root.
//
// Most subcommands set SilenceErrors so they can print their own
// errors (also as JSON). cobra checks flags and the argument count
// before a command's RunE runs, so for those commands an unknown flag
// or a wrong argument count used to print nothing and only exit 1.
//
// The fix has two parts. The root's flag error function is inherited
// by every child command. The argument check of every command is
// wrapped. Both print one usage_error through output.RenderError, as
// JSON when the command has a --json flag and the command line asks
// for it. Call this after every command (also the discovered ones) is
// added to the tree. args is the command line without the program
// name; cobra stops parsing at an unknown flag, so a --json after it
// is found only in args.
func installUsageErrors(root *cobra.Command, args []string) {
	root.SetFlagErrorFunc(func(c *cobra.Command, err error) error {
		return renderUsageError(c, err, args)
	})
	wrapArgErrors(root, args)
}

// wrapArgErrors wraps the argument check of c and of all its children.
func wrapArgErrors(c *cobra.Command, args []string) {
	if validate := c.Args; validate != nil {
		c.Args = func(cc *cobra.Command, posArgs []string) error {
			if err := validate(cc, posArgs); err != nil {
				return renderUsageError(cc, err, args)
			}
			return nil
		}
	}
	for _, sub := range c.Commands() {
		wrapArgErrors(sub, args)
	}
}

// renderUsageError prints err for c as a usage_error and returns an
// error that carries exit code 1 and no text. It also silences cobra's
// own printer for c, so a command without SilenceErrors does not print
// the error a second time.
func renderUsageError(c *cobra.Command, err error, args []string) error {
	c.SilenceErrors = true
	detail := fmt.Sprintf("%v (run '%s --help' for usage)", err, c.CommandPath())
	return output.RenderError(c.ErrOrStderr(), output.Usage(detail), wantsJSON(c, args))
}

// wantsJSON reports whether c has a --json flag and the command line
// turns it on. It reads the whole command line up to "--" and keeps
// the last --json setting, as normal flag parsing does. The parsed
// flag value alone is not enough: cobra stops at an unknown flag, so
// a --json after it is found only in args. A word that is the value
// of another flag of c (for example --query --json) is skipped, so
// it does not count as a setting. Both callers run after cobra parsed
// the flags, so c.Flags() already holds the inherited persistent
// flags.
func wantsJSON(c *cobra.Command, args []string) bool {
	if c.Flags().Lookup("json") == nil {
		return false
	}
	on := false
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			break
		}
		if a == "--json" {
			on = true
			continue
		}
		if v, ok := strings.CutPrefix(a, "--json="); ok {
			b, err := strconv.ParseBool(v)
			on = err == nil && b
			continue
		}
		if takesNextWord(c, a) {
			i++
		}
	}
	return on
}

// takesNextWord reports whether a is a known flag of c that reads its
// value from the next command line word, as pflag does for
// "--name value" and "-n value". A flag written as --name=value,
// -n=value or -nvalue carries its value in the same word. A flag with
// an optional value (every bool flag) never reads the next word.
// Shorthands can be grouped (-vp value): the first one that needs a
// value takes the rest of the word, or the next word when the word
// ends there.
func takesNextWord(c *cobra.Command, a string) bool {
	if name, ok := strings.CutPrefix(a, "--"); ok {
		if strings.Contains(name, "=") {
			return false
		}
		f := c.Flags().Lookup(name)
		return f != nil && f.NoOptDefVal == ""
	}
	shorts, ok := strings.CutPrefix(a, "-")
	if !ok {
		return false
	}
	for i := 0; i < len(shorts); i++ {
		f := c.Flags().ShorthandLookup(shorts[i : i+1])
		if f == nil {
			return false
		}
		if f.NoOptDefVal != "" {
			continue
		}
		return i == len(shorts)-1
	}
	return false
}
