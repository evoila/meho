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
	err := root.Execute()
	switch {
	case err == nil:
		exit = 0
	default:
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
			argv: []string{"operation", "search", "onlyonearg"},
			want: "accepts 2 arg(s), received 1 (run 'meho operation search --help' for usage)",
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

// TestUsageErrorJSONFlagBeforeBadFlag covers a --json that cobra did
// parse before it stopped at the unknown flag.
func TestUsageErrorJSONFlagBeforeBadFlag(t *testing.T) {
	_, stderr, _ := runCLI(t, "operation", "search", "conn", "query", "--json", "--no-such-flag")
	if !strings.HasPrefix(stderr, `{"error":"usage_error"`) {
		t.Errorf("stderr = %q, want the JSON error shape", stderr)
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
	stdout, stderr, exit := runCLI(t, "operation", "search", "--help")
	if exit != 0 || stderr != "" || !strings.Contains(stdout, "search <connector_id> <query>") {
		t.Errorf("--help: exit=%d stderr=%q stdout=%q", exit, stderr, stdout)
	}
}
