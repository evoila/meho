// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

//go:build unix

package bind9

import "syscall"

// mkFifo is a thin wrapper around syscall.Mkfifo so the unit tests can
// construct a named pipe to exercise the non-regular-file rejection in
// assembleViewsBundle. Gated to unix builds because Windows lacks the
// POSIX mkfifo syscall; on non-unix targets the symlink test still
// proves the regular-file-only contract.
func mkFifo(path string, mode uint32) error {
	return syscall.Mkfifo(path, mode)
}
