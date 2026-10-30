// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// mehoHostPath is where install.sh of install form 2 (MEHO on a Linux
// server) puts the host command. When this file exists, the doctor runs
// on such a server.
const mehoHostPath = "/usr/local/sbin/meho-host"

// mehoHostTimeout bounds one run of `meho-host check --json`.
// mehoHostWaitDelay bounds the wait for its output after it has ended
// or was stopped: a kubectl or rke2 that meho-host started can keep the
// output open after meho-host itself is gone.
const (
	mehoHostTimeout   = 60 * time.Second
	mehoHostWaitDelay = 5 * time.Second
)

// mehoHostCheckName names the one server line the doctor adds itself:
// when it does not run meho-host, or cannot show its lines.
const mehoHostCheckName = "meho-host check"

// mehoHostRunHint points at the customer's own command, which shows the
// same checks with meho-host's own messages.
const mehoHostRunHint = "run `sudo meho-host check` to see its own messages"

// mehoHostStatuses maps the status words of `meho-host check --json` to
// the doctor's statuses. meho-host uses ok, warn and fail. skip is a
// doctor status too, so it is accepted as well.
var mehoHostStatuses = map[string]string{
	"ok":   doctorOK,
	"warn": doctorWarn,
	"fail": doctorFail,
	"skip": doctorSkip,
}

// mehoHostLine is one line of `meho-host check --json`, which prints
// {"checks":[{"name":...,"status":...,"detail":...,"hint":...}]}
// (print_checks_json in deploy/linux-server/meho-host).
type mehoHostLine struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Detail string `json:"detail"`
	Hint   string `json:"hint"`
}

func server(name, status, detail, hint string) doctorCheck {
	return doctorCheck{Layer: layerServer, Name: name, Status: status, Detail: detail, Hint: hint}
}

// runServerChecks adds the server layer when meho-host is on this
// machine. As root it runs `meho-host check --json` and adds one line
// per check. As another user it adds one warning and runs nothing, so
// no password prompt can appear. On a machine without meho-host (a
// laptop) it adds nothing.
func runServerChecks(ctx context.Context, r *doctorReport, opts doctorOptions, deps doctorDeps) {
	if _, err := deps.stat(mehoHostPath); err != nil {
		return
	}
	if opts.noNetwork {
		r.add(server(mehoHostCheckName, doctorSkip, "skipped (--no-network)",
			"run `sudo meho doctor` without --no-network to see the RKE2 checks"))
		return
	}
	if deps.geteuid() != 0 {
		r.add(server(mehoHostCheckName, doctorWarn,
			mehoHostPath+" is on this machine, but the doctor does not run as root, so it did not run it",
			"RKE2 checks need root: run `sudo meho doctor`"))
		return
	}
	timeout := deps.serverTimeout
	if timeout <= 0 {
		timeout = mehoHostTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	stdout, stderr, err := deps.runCommand(ctx, mehoHostPath, "check", "--json")
	if err != nil && errors.Is(ctx.Err(), context.DeadlineExceeded) {
		r.add(server(mehoHostCheckName, doctorFail,
			fmt.Sprintf("meho-host check --json did not finish within %g seconds", timeout.Seconds()),
			"RKE2 may not answer: see `systemctl status rke2-server`, then "+mehoHostRunHint))
		return
	}
	lines, perr := parseMehoHostChecks(stdout)
	if perr != nil {
		r.add(server(mehoHostCheckName, doctorFail, mehoHostFailure(err, perr, stderr), mehoHostRunHint))
		return
	}
	// meho-host exits 1 when a line is red. With valid JSON that is a
	// normal answer: the lines decide the result, not the exit code.
	for _, l := range lines {
		r.add(mehoHostCheck(l))
	}
}

// parseMehoHostChecks reads the output of `meho-host check --json`. An
// empty list is an error too, so the server layer never passes without
// a line.
func parseMehoHostChecks(stdout []byte) ([]mehoHostLine, error) {
	if len(bytes.TrimSpace(stdout)) == 0 {
		return nil, errors.New("it printed nothing")
	}
	var doc struct {
		Checks []mehoHostLine `json:"checks"`
	}
	if err := json.Unmarshal(stdout, &doc); err != nil {
		return nil, fmt.Errorf("its output is not valid JSON: %w", err)
	}
	if len(doc.Checks) == 0 {
		return nil, errors.New("its output lists no check")
	}
	return doc.Checks, nil
}

// mehoHostFailure says why the lines of meho-host cannot be shown: how
// the run ended, what is wrong with the output, and the first line of
// the error output, where meho-host prints its own [FAIL] message.
func mehoHostFailure(runErr, parseErr error, stderr []byte) string {
	detail := "meho-host check --json ran, but " + parseErr.Error()
	if runErr != nil {
		detail = fmt.Sprintf("meho-host check --json failed (%v), and %v", runErr, parseErr)
	}
	if msg := firstLine(stderr); msg != "" {
		detail += "; its message: " + msg
	}
	return detail
}

func firstLine(b []byte) string {
	for _, line := range strings.Split(string(b), "\n") {
		if s := strings.TrimSpace(line); s != "" {
			return s
		}
	}
	return ""
}

// mehoHostCheck turns one line of meho-host into a doctor line. A status
// word the doctor does not know is a failure, so a line it cannot read
// never counts as passed.
func mehoHostCheck(l mehoHostLine) doctorCheck {
	if status, ok := mehoHostStatuses[l.Status]; ok {
		return server(l.Name, status, l.Detail, l.Hint)
	}
	hint := l.Hint
	if hint == "" {
		hint = mehoHostRunHint
	}
	return server(l.Name, doctorFail, fmt.Sprintf("unknown status %q from meho-host: %s", l.Status, l.Detail), hint)
}

// localCommandRunner returns the function that runs a command on this
// machine and gives back what it printed on standard output and on
// standard error. waitDelay bounds the wait for that output after the
// command has ended or was stopped (exec.Cmd.WaitDelay).
func localCommandRunner(waitDelay time.Duration) func(ctx context.Context, name string, args ...string) ([]byte, []byte, error) {
	return func(ctx context.Context, name string, args ...string) ([]byte, []byte, error) {
		var stdout, stderr bytes.Buffer
		cmd := exec.CommandContext(ctx, name, args...)
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		cmd.WaitDelay = waitDelay
		err := cmd.Run()
		return stdout.Bytes(), stderr.Bytes(), err
	}
}
