// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// hostCheckJSON is a fixed answer of `meho-host check --json`, in the
// shape of print_checks_json in deploy/linux-server/meho-host.
const hostCheckJSON = `{"checks":[` +
	`{"name":"node","status":"ok","detail":"Ready","hint":""},` +
	`{"name":"pods","status":"ok","detail":"all 21 pods in meho, kube-system, local-path-storage are fine","hint":""},` +
	`{"name":"disk","status":"warn","detail":"/var/lib/rancher has 18 % free.","hint":"free disk space: df -h /var/lib/rancher"},` +
	`{"name":"certificates","status":"ok","detail":"all 24 RKE2 certificates are valid for at least 30 days","hint":""}]}` + "\n"

// hostRun is what the fake meho-host answers.
type hostRun struct {
	stdout, stderr string
	err            error
}

// serverDeps returns doctor deps for a machine where meho-host is
// installed (or not) and the doctor runs with the user id uid. The
// environment is empty, so no offline check fails and no network call
// is made. run answers in place of meho-host; nil fails the test when
// meho-host is run.
func serverDeps(t *testing.T, installed bool, uid int, run func(ctx context.Context) hostRun) doctorDeps {
	t.Helper()
	withTempXDG(t)
	d := offlineDeps(t, t.TempDir())
	d.getenv = func(string) string { return "" }
	d.stat = func(name string) (fs.FileInfo, error) {
		if name != mehoHostPath {
			t.Errorf("stat %q, want %q", name, mehoHostPath)
		}
		if !installed {
			return nil, fs.ErrNotExist
		}
		return nil, nil
	}
	d.geteuid = func() int { return uid }
	d.runCommand = func(ctx context.Context, name string, args ...string) ([]byte, []byte, error) {
		if run == nil {
			t.Errorf("meho-host must not run")
			return nil, nil, errors.New("not allowed")
		}
		if name != mehoHostPath || strings.Join(args, " ") != "check --json" {
			t.Errorf("ran %s %v, want %s check --json", name, args, mehoHostPath)
		}
		got := run(ctx)
		return []byte(got.stdout), []byte(got.stderr), got.err
	}
	return d
}

func fixedAnswer(stdout string, err error) func(context.Context) hostRun {
	return func(context.Context) hostRun { return hostRun{stdout: stdout, err: err} }
}

func runTestDoctor(t *testing.T, opts doctorOptions, deps doctorDeps) *doctorReport {
	t.Helper()
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	return runDoctor(cmd, opts, deps)
}

func serverChecks(r *doctorReport) []doctorCheck {
	var out []doctorCheck
	for _, c := range r.Checks {
		if c.Layer == layerServer {
			out = append(out, c)
		}
	}
	return out
}

// assertServerLast checks that every server line comes after every
// line of the doctor's own layers.
func assertServerLast(t *testing.T, checks []doctorCheck) {
	t.Helper()
	seen := false
	for _, c := range checks {
		if c.Layer == layerServer {
			seen = true
		} else if seen {
			t.Errorf("doctor line %q after a server line: %+v", c.Name, checks)
		}
	}
}

func render(t *testing.T, r *doctorReport, jsonOut bool) (string, error) {
	t.Helper()
	var buf bytes.Buffer
	err := renderDoctor(&buf, r, jsonOut)
	return buf.String(), err
}

func TestDoctor_ServerLayer_RootRunsMehoHost(t *testing.T) {
	r := runTestDoctor(t, doctorOptions{}, serverDeps(t, true, 0, fixedAnswer(hostCheckJSON, nil)))

	got := serverChecks(r)
	want := []doctorCheck{
		server("node", doctorOK, "Ready", ""),
		server("pods", doctorOK, "all 21 pods in meho, kube-system, local-path-storage are fine", ""),
		server("disk", doctorWarn, "/var/lib/rancher has 18 % free.", "free disk space: df -h /var/lib/rancher"),
		server("certificates", doctorOK, "all 24 RKE2 certificates are valid for at least 30 days", ""),
	}
	if len(got) != len(want) {
		t.Fatalf("server lines = %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("server line %d = %+v, want %+v", i, got[i], want[i])
		}
	}
	assertServerLast(t, r.Checks)
	findCheck(t, r, "binary")
	findCheck(t, r, "authenticated layer")

	human, err := render(t, r, false)
	if err != nil {
		t.Errorf("warn lines only: exit error %v", err)
	}
	authed, srv := strings.Index(human, "Authenticated layer"), strings.Index(human, "Server layer")
	if authed < 0 || srv < authed {
		t.Errorf("the server layer must come after the doctor's own layers:\n%s", human)
	}
	if !strings.Contains(human[srv:], "[warn] disk               /var/lib/rancher has 18 % free.\n         -> free disk space: df -h /var/lib/rancher") {
		t.Errorf("server line or hint missing:\n%s", human)
	}

	out, _ := render(t, r, true)
	var report doctorReport
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, out)
	}
	assertServerLast(t, report.Checks)
	if js := serverChecks(&report); len(js) != len(want) || js[2] != want[2] {
		t.Errorf("JSON server lines = %+v", js)
	}
}

// TestDoctor_ServerLayer_AfterSummary: the summary of the authenticated
// layer is the doctor's own output, so the server lines follow it.
func TestDoctor_ServerLayer_AfterSummary(t *testing.T) {
	r := &doctorReport{GrantedScopes: []string{"mcp:read"}, MCPSessionID: "0b6c1f7e-1111-2222-3333-444455556666"}
	r.add(authed("token", doctorOK, "", "valid", ""))
	r.add(server("node", doctorOK, "Ready", ""))
	human, _ := render(t, r, false)
	want := "Authenticated layer\n" +
		"  [ok  ] token              valid\n" +
		"Granted scopes (CLI session): mcp:read\n" +
		"MCP session id (for the audit log): 0b6c1f7e-1111-2222-3333-444455556666\n" +
		"Server layer\n" +
		"  [ok  ] node               Ready\n"
	if human != want {
		t.Errorf("human output:\n%s\nwant:\n%s", human, want)
	}
}

// TestDoctor_HumanOutputWithoutServerLayer pins the human output of a
// report without server lines to the layout the doctor printed before
// the server layer existed.
func TestDoctor_HumanOutputWithoutServerLayer(t *testing.T) {
	r := &doctorReport{
		GrantedScopes: []string{"mcp:read", "openid"},
		Surface: &doctorSurface{Listed: 2, Expected: []string{"a", "b", "c"}, Gap: []string{"c"},
			HiddenByServer: []string{"d"}, OperatorSurface: true},
		MCPSessionID: "0b6c1f7e-1111-2222-3333-444455556666",
	}
	r.add(offline("binary", doctorOK, "/opt/meho/bin/meho (version dev)", ""))
	r.add(offline("config", doctorWarn, "no saved backplane URL", "run `meho login <backplane-url>`"))
	r.add(authed("token", doctorFail, classUnauthorized, "401", "run `meho login`"))
	human, _ := render(t, r, false)
	want := "Offline layer\n" +
		"  [ok  ] binary             /opt/meho/bin/meho (version dev)\n" +
		"  [warn] config             no saved backplane URL\n" +
		"         -> run `meho login <backplane-url>`\n" +
		"Authenticated layer\n" +
		"  [fail] token              401 (unauthorized)\n" +
		"         -> run `meho login`\n" +
		"Granted scopes (CLI session): mcp:read openid\n" +
		"Working surface: server expects 3 tools for this session, handshake listed 2 tools\n" +
		"  missing from tools/list: c\n" +
		"  hidden by the server for this session: d\n" +
		"  operator planes: listed (mcp:admin)\n" +
		"MCP session id (for the audit log): 0b6c1f7e-1111-2222-3333-444455556666\n"
	if human != want {
		t.Errorf("human output:\n%s\nwant:\n%s", human, want)
	}
}

func TestDoctor_ServerLayer_NotRoot(t *testing.T) {
	r := runTestDoctor(t, doctorOptions{}, serverDeps(t, true, 1000, nil))
	got := serverChecks(r)
	if len(got) != 1 {
		t.Fatalf("want one server line, got %+v", got)
	}
	c := got[0]
	if c.Name != mehoHostCheckName || c.Status != doctorWarn || c.Hint != "RKE2 checks need root: run `sudo meho doctor`" {
		t.Errorf("not-root line: %+v", c)
	}
	if !strings.Contains(c.Detail, mehoHostPath) {
		t.Errorf("the line should name %s: %q", mehoHostPath, c.Detail)
	}
	if _, err := render(t, r, false); err != nil {
		t.Errorf("a warn line must keep exit code 0, got %v", err)
	}
}

func TestDoctor_ServerLayer_NoNetworkSkips(t *testing.T) {
	r := runTestDoctor(t, doctorOptions{noNetwork: true}, serverDeps(t, true, 0, nil))
	got := serverChecks(r)
	if len(got) != 1 || got[0].Status != doctorSkip || got[0].Detail != "skipped (--no-network)" {
		t.Fatalf("want one skip line, got %+v", got)
	}
}

// TestDoctor_ServerLayer_LaptopUnchanged: without meho-host there is no
// server layer, and the output is what the doctor printed before it.
func TestDoctor_ServerLayer_LaptopUnchanged(t *testing.T) {
	for _, noNetwork := range []bool{false, true} {
		deps := serverDeps(t, false, 0, nil)
		deps.geteuid = func() int {
			t.Error("the user id does not matter without meho-host")
			return 0
		}
		opts := doctorOptions{noNetwork: noNetwork}
		r := runTestDoctor(t, opts, deps)
		if got := serverChecks(r); len(got) != 0 {
			t.Fatalf("no-network=%t: server lines on a laptop: %+v", noNetwork, got)
		}

		// The report the doctor built before the server layer existed:
		// the offline layer, then the skipped authenticated layer.
		before := &doctorReport{}
		runOfflineChecks(before, opts, deps)
		skip := doctorCheck{Layer: layerAuthed, Name: "authenticated layer", Status: doctorSkip,
			Detail: "skipped: no usable backplane URL", Hint: "run `meho login <url>` or pass --backplane <url>"}
		if noNetwork {
			skip = doctorCheck{Layer: layerAuthed, Name: "authenticated layer", Status: doctorSkip,
				Detail: "skipped (--no-network)"}
		}
		before.add(skip)
		for _, jsonOut := range []bool{false, true} {
			got, gotErr := render(t, r, jsonOut)
			want, wantErr := render(t, before, jsonOut)
			if got != want || (gotErr == nil) != (wantErr == nil) {
				t.Errorf("no-network=%t json=%t: output changed:\n%s\nwant:\n%s", noNetwork, jsonOut, got, want)
			}
		}
	}
}

func TestDoctor_ServerLayer_ExitCodes(t *testing.T) {
	red := `{"checks":[{"name":"node","status":"fail","detail":"RKE2 does not answer.","hint":"check that RKE2 runs"},` +
		`{"name":"disk","status":"warn","detail":"18 % free.","hint":"free disk space"}]}`
	cases := []struct {
		name   string
		answer hostRun
		want   int
	}{
		{"red line, meho-host exits 1", hostRun{stdout: red, err: errors.New("exit status 1")}, 1},
		{"warn line only", hostRun{stdout: hostCheckJSON}, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			run := func(context.Context) hostRun { return tc.answer }
			r := runTestDoctor(t, doctorOptions{}, serverDeps(t, true, 0, run))
			for _, c := range r.Checks {
				if c.Layer != layerServer && c.Status == doctorFail {
					t.Fatalf("precondition: a doctor line fails: %+v", c)
				}
			}
			if got := doctorExitCode(r); got != tc.want {
				t.Errorf("exit code = %d, want %d: %+v", got, tc.want, serverChecks(r))
			}
			_, err := render(t, r, false)
			var exit *doctorExit
			if tc.want == 0 && err != nil || tc.want != 0 && (!errors.As(err, &exit) || exit.ExitCode() != tc.want) {
				t.Errorf("render error = %v, want exit code %d", err, tc.want)
			}
		})
	}
}

// TestDoctor_ServerLayer_BadAnswers: an answer the doctor cannot read
// becomes one red line with a hint, never a crash.
func TestDoctor_ServerLayer_BadAnswers(t *testing.T) {
	cases := []struct {
		name    string
		answer  hostRun
		wantSub []string
	}{
		{"exit 1, no output", hostRun{stderr: "\n[FAIL] meho-host stopped at meho-host line 300: a command failed with exit code 1.\n", err: errors.New("exit status 1")},
			[]string{"failed (exit status 1), and it printed nothing", "its message: [FAIL] meho-host stopped at meho-host line 300"}},
		{"exit 2, usage text", hostRun{stdout: "usage", stderr: "meho-host: unknown option of check: --json\n", err: errors.New("exit status 2")},
			[]string{"failed (exit status 2), and its output is not valid JSON", "its message: meho-host: unknown option of check: --json"}},
		{"exit 0, invalid JSON", hostRun{stdout: `{"checks":[`},
			[]string{"ran, but its output is not valid JSON"}},
		{"exit 0, empty list", hostRun{stdout: `{"checks":[]}`},
			[]string{"ran, but its output lists no check"}},
		{"exit 0, JSON null", hostRun{stdout: `null`},
			[]string{"ran, but its output lists no check"}},
		{"cannot start", hostRun{err: errors.New("fork/exec " + mehoHostPath + ": permission denied")},
			[]string{"permission denied", "it printed nothing"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			run := func(context.Context) hostRun { return tc.answer }
			r := runTestDoctor(t, doctorOptions{}, serverDeps(t, true, 0, run))
			got := serverChecks(r)
			if len(got) != 1 {
				t.Fatalf("want one server line, got %+v", got)
			}
			c := got[0]
			if c.Name != mehoHostCheckName || c.Status != doctorFail || c.Hint != mehoHostRunHint {
				t.Errorf("line: %+v", c)
			}
			for _, sub := range tc.wantSub {
				if !strings.Contains(c.Detail, sub) {
					t.Errorf("detail %q does not contain %q", c.Detail, sub)
				}
			}
			if doctorExitCode(r) != 1 {
				t.Errorf("exit code = %d, want 1", doctorExitCode(r))
			}
		})
	}
}

func TestDoctor_ServerLayer_UnknownStatus(t *testing.T) {
	answer := `{"checks":[{"name":"node","status":"ok","detail":"Ready","hint":""},` +
		`{"name":"firewall","status":"partly","detail":"some rules are loaded","hint":""},` +
		`{"name":"cis","status":"skip","detail":"no scanner","hint":"","extra":"ignored"}]}`
	r := runTestDoctor(t, doctorOptions{}, serverDeps(t, true, 0, fixedAnswer(answer, nil)))
	got := serverChecks(r)
	if len(got) != 3 {
		t.Fatalf("server lines = %+v", got)
	}
	if got[0] != server("node", doctorOK, "Ready", "") {
		t.Errorf("known line changed: %+v", got[0])
	}
	want := server("firewall", doctorFail, `unknown status "partly" from meho-host: some rules are loaded`, mehoHostRunHint)
	if got[1] != want {
		t.Errorf("unknown status line = %+v, want %+v", got[1], want)
	}
	if got[2].Status != doctorSkip {
		t.Errorf("skip line: %+v", got[2])
	}
}

func TestDoctor_ServerLayer_Timeout(t *testing.T) {
	stalled := func(ctx context.Context) hostRun {
		select {
		case <-ctx.Done():
		case <-time.After(10 * time.Second):
			t.Error("the server layer has no time limit")
		}
		return hostRun{stdout: `{"checks":[`, err: errors.New("signal: killed")}
	}
	deps := serverDeps(t, true, 0, stalled)
	deps.serverTimeout = 50 * time.Millisecond
	r := runTestDoctor(t, doctorOptions{}, deps)
	got := serverChecks(r)
	if len(got) != 1 {
		t.Fatalf("want one server line, got %+v", got)
	}
	c := got[0]
	if c.Name != mehoHostCheckName || c.Status != doctorFail || c.Detail != "meho-host check --json did not finish within 0.05 seconds" ||
		!strings.Contains(c.Hint, "systemctl status rke2-server") {
		t.Errorf("timeout line: %+v", c)
	}
}

func TestDoctor_ServerLayer_DefaultLimit(t *testing.T) {
	deps := defaultDoctorDeps()
	if deps.serverTimeout != 60*time.Second || mehoHostTimeout != 60*time.Second {
		t.Errorf("server limit = %s", deps.serverTimeout)
	}
	if deps.stat == nil || deps.geteuid == nil || deps.runCommand == nil {
		t.Error("the server layer seams must be set")
	}
	var seen time.Duration
	d := serverDeps(t, true, 0, func(ctx context.Context) hostRun {
		if dl, ok := ctx.Deadline(); ok {
			seen = time.Until(dl)
		}
		return hostRun{stdout: hostCheckJSON}
	})
	d.serverTimeout = 0
	runTestDoctor(t, doctorOptions{}, d)
	if seen < 55*time.Second || seen > 60*time.Second {
		t.Errorf("a zero serverTimeout must use the 60-second limit, got %s", seen)
	}
}

// writeScript writes an executable shell script for the tests of
// localCommandRunner.
func writeScript(t *testing.T, body string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("needs a POSIX shell")
	}
	path := filepath.Join(t.TempDir(), "meho-host")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

// TestLocalCommandRunner_FakeMehoHost runs a fake meho-host script
// through the real runner: it prints the fixed list and exits 1, as
// meho-host does when a line is red.
func TestLocalCommandRunner_FakeMehoHost(t *testing.T) {
	script := writeScript(t, `[ "$1 $2" = "check --json" ] || exit 2
printf '%s' '`+hostCheckJSON+`'
echo "a note" >&2
exit 1
`)
	deps := serverDeps(t, true, 0, nil)
	runner := localCommandRunner(time.Second)
	deps.runCommand = func(ctx context.Context, name string, args ...string) ([]byte, []byte, error) {
		if name != mehoHostPath {
			t.Errorf("ran %s", name)
		}
		return runner(ctx, script, args...)
	}
	r := runTestDoctor(t, doctorOptions{}, deps)
	got := serverChecks(r)
	if len(got) != 4 || got[2] != server("disk", doctorWarn, "/var/lib/rancher has 18 % free.", "free disk space: df -h /var/lib/rancher") {
		t.Errorf("server lines = %+v", got)
	}

	stdout, stderr, err := runner(context.Background(), script, "check", "--json")
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 1 {
		t.Errorf("err = %v, want exit code 1", err)
	}
	if string(stdout) != hostCheckJSON || string(stderr) != "a note\n" {
		t.Errorf("stdout = %q, stderr = %q", stdout, stderr)
	}
}

// TestLocalCommandRunner_StopsAtDeadline: a child that the script
// started keeps the output open after the script is stopped. The wait
// delay ends the wait anyway.
func TestLocalCommandRunner_StopsAtDeadline(t *testing.T) {
	script := writeScript(t, "sleep 3 &\nsleep 3\n")
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, _, err := localCommandRunner(200*time.Millisecond)(ctx, script)
	if took := time.Since(start); took > 2*time.Second {
		t.Errorf("the runner waited %s", took)
	}
	if err == nil || !errors.Is(ctx.Err(), context.DeadlineExceeded) {
		t.Errorf("err = %v, ctx = %v", err, ctx.Err())
	}
}
