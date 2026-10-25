// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// runCLI builds a fresh command tree for argv, runs it, and returns
// stdout, stderr and the exit code main() would use.
func runCLI(t *testing.T, argv ...string) (stdout, stderr string, exit int) {
	t.Helper()
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	defer restore()

	root := newRootCmdFor(argv)
	var out, errOut bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetArgs(argv)
	if err := root.Execute(); err != nil {
		exit = 1
		var coder output.ExitCoder
		if errors.As(err, &coder) {
			exit = coder.ExitCode()
		}
	}
	return out.String(), errOut.String(), exit
}

// commandPaths lists the path (without "meho") of every command in
// the tree, parents included.
func commandPaths(c *cobra.Command, prefix []string, into *[][]string) {
	for _, sub := range c.Commands() {
		path := append(append([]string{}, prefix...), sub.Name())
		*into = append(*into, path)
		commandPaths(sub, path, into)
	}
}

// TestUnknownFlagPrintsErrorForEveryCommand walks the whole command
// tree. An unknown flag must print a message that names the flag and
// must exit non-zero, for every command.
func TestUnknownFlagPrintsErrorForEveryCommand(t *testing.T) {
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	tree := newRootCmdFor(nil)
	restore()

	var paths [][]string
	commandPaths(tree, nil, &paths)
	if len(paths) < 100 {
		t.Fatalf("walked only %d commands; the tree walk is broken", len(paths))
	}
	for _, path := range paths {
		argv := append(append([]string{}, path...), "--no-such-flag")
		stdout, stderr, exit := runCLI(t, argv...)
		name := "meho " + strings.Join(path, " ")
		if exit == 0 {
			t.Errorf("%s --no-such-flag: exit 0, want non-zero", name)
		}
		if !strings.Contains(stderr, "unknown flag: --no-such-flag") {
			t.Errorf("%s --no-such-flag: stderr %q does not name the flag", name, stderr)
		}
		if stdout != "" {
			t.Errorf("%s --no-such-flag: stdout %q, want empty", name, stdout)
		}
	}
}

func TestUsageErrorsPlainAndJSON(t *testing.T) {
	cases := []struct {
		name string
		argv []string
		want string
	}{
		{
			name: "unknown flag",
			argv: []string{"operation", "search", "conn", "query", "--no-such-flag"},
			want: "unknown flag: --no-such-flag (run 'meho operation search --help' for usage)",
		},
		{
			name: "wrong argument count",
			argv: []string{"operation", "call", "onlyonearg"},
			want: "accepts 2 arg(s), received 1 (run 'meho operation call --help' for usage)",
		},
		{
			name: "wrong argument count with a custom check",
			argv: []string{"topology", "unannotate", "a", "b"},
			want: "unannotate requires either <edge-id> or <from> <kind> <to> (got 2 args) " +
				"(run 'meho topology unannotate --help' for usage)",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name+"/plain", func(t *testing.T) {
			stdout, stderr, exit := runCLI(t, tc.argv...)
			if exit != output.ExitUsage {
				t.Errorf("exit = %d, want %d", exit, output.ExitUsage)
			}
			if want := "meho: usage_error: " + tc.want + "\n"; stderr != want {
				t.Errorf("stderr = %q, want %q", stderr, want)
			}
			if stdout != "" {
				t.Errorf("stdout = %q, want empty", stdout)
			}
		})
		t.Run(tc.name+"/json", func(t *testing.T) {
			// --json after the bad flag: cobra never parses it.
			argv := append(append([]string{}, tc.argv...), "--json")
			stdout, stderr, exit := runCLI(t, argv...)
			if exit != output.ExitUsage {
				t.Errorf("exit = %d, want %d", exit, output.ExitUsage)
			}
			var got struct {
				Error    string `json:"error"`
				Detail   string `json:"detail"`
				ExitCode int    `json:"exit_code"`
			}
			if err := json.Unmarshal([]byte(stderr), &got); err != nil {
				t.Fatalf("stderr is not one JSON document: %v\n%s", err, stderr)
			}
			if got.Error != output.ErrCodeUsage || got.Detail != tc.want || got.ExitCode != output.ExitUsage {
				t.Errorf("JSON error = %+v, want usage_error / %q / %d", got, tc.want, output.ExitUsage)
			}
			if stdout != "" {
				t.Errorf("stdout = %q, want empty", stdout)
			}
		})
	}
}

// TestUsageErrorJSONLastSettingWins: the last --json setting on the
// command line decides the format, as it does for a parsed flag. This
// holds whether the settings sit before the bad flag (cobra parsed
// them) or after it (only the raw command line has them).
func TestUsageErrorJSONLastSettingWins(t *testing.T) {
	cases := []struct {
		name     string
		argv     []string
		wantJSON bool
	}{
		{"json before bad flag", []string{"operation", "search", "conn", "query", "--json", "--no-such-flag"}, true},
		{"off then bad flag then on", []string{"operation", "search", "conn", "query", "--json=false", "--no-such-flag", "--json"}, true},
		{"on then bad flag then off", []string{"operation", "search", "conn", "query", "--json", "--no-such-flag", "--json=false"}, false},
		{"on then off, both parsed", []string{"operation", "search", "conn", "query", "--json", "--json=false", "--no-such-flag"}, false},
		{"off then on, both parsed", []string{"operation", "search", "conn", "query", "--json=false", "--json", "--no-such-flag"}, true},
		{"json after -- does not count", []string{"operation", "search", "conn", "query", "--no-such-flag", "--", "--json"}, false},
		{"wrong argument count, off then on", []string{"operation", "call", "onlyonearg", "--json=false", "--json"}, true},
		{"wrong argument count, on then off", []string{"operation", "call", "onlyonearg", "--json", "--json=false"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, stderr, exit := runCLI(t, tc.argv...)
			if exit != output.ExitUsage {
				t.Errorf("exit = %d, want %d", exit, output.ExitUsage)
			}
			gotJSON := strings.HasPrefix(stderr, `{"error":"usage_error"`)
			gotPlain := strings.HasPrefix(stderr, "meho: usage_error: ")
			if gotJSON != tc.wantJSON || gotPlain == tc.wantJSON {
				t.Errorf("stderr = %q, want JSON=%v", stderr, tc.wantJSON)
			}
		})
	}
}

// TestUsageErrorJSONSkipsFlagValues: a word that is the value of
// another flag is not a --json setting, as in normal flag parsing.
// "meho targets list" has --json, the string flag --product (-p), the
// int flag --limit, the inherited string flag --work-ref and the
// inherited bool flag -v.
func TestUsageErrorJSONSkipsFlagValues(t *testing.T) {
	cases := []struct {
		name     string
		argv     []string
		wantJSON bool
	}{
		{"value of a string flag", []string{"targets", "list", "--json", "--product", "--json=false", "--bogus"}, true},
		{"value of a string flag after the bad flag", []string{"targets", "list", "--bogus", "--product", "--json"}, false},
		{"value in the same word", []string{"targets", "list", "--product=--json", "--bogus"}, false},
		{"value of a short flag", []string{"targets", "list", "-p", "--json", "--bogus"}, false},
		{"value of a short flag in the same word", []string{"targets", "list", "-p--json", "--bogus"}, false},
		{"value of a grouped short flag", []string{"targets", "list", "-vp", "--json", "--bogus"}, false},
		{"bool short flag takes no value", []string{"targets", "list", "-v", "--json", "--bogus"}, true},
		{"value of an int flag", []string{"targets", "list", "--json", "--limit", "--json=false", "--bogus"}, true},
		{"value of an inherited string flag", []string{"targets", "list", "--work-ref", "--json", "--bogus"}, false},
		{"string flag with no value at the end", []string{"targets", "list", "--json", "--product"}, true},
		{"wrong argument count, value of a string flag", []string{"targets", "list", "extra", "--json", "--product", "--json=false"}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, stderr, exit := runCLI(t, tc.argv...)
			if exit != output.ExitUsage {
				t.Errorf("exit = %d, want %d", exit, output.ExitUsage)
			}
			gotJSON := strings.HasPrefix(stderr, `{"error":"usage_error"`)
			gotPlain := strings.HasPrefix(stderr, "meho: usage_error: ")
			if gotJSON != tc.wantJSON || gotPlain == tc.wantJSON {
				t.Errorf("stderr = %q, want JSON=%v", stderr, tc.wantJSON)
			}
		})
	}
}

// TestUsageErrorNoJSONWithoutJSONFlag: a command with no --json flag
// prints the plain form, even when --json is on the command line.
func TestUsageErrorNoJSONWithoutJSONFlag(t *testing.T) {
	_, stderr, exit := runCLI(t, "version", "--no-such-flag", "--json")
	if exit == 0 {
		t.Errorf("exit 0, want non-zero")
	}
	want := "meho: usage_error: unknown flag: --no-such-flag (run 'meho version --help' for usage)\n"
	if stderr != want {
		t.Errorf("stderr = %q, want %q (printed once)", stderr, want)
	}
}

// TestUsageErrorsKeepHelpWorking: --help still prints help and exits 0.
func TestUsageErrorsKeepHelpWorking(t *testing.T) {
	stdout, stderr, exit := runCLI(t, "operation", "call", "--help")
	if exit != 0 || stderr != "" || !strings.Contains(stdout, "call <connector_id> <op_id>") {
		t.Errorf("--help: exit=%d stderr=%q stdout=%q", exit, stderr, stdout)
	}
}
