// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// cdromBackplane records every operation call. It behaves like a fresh
// install with no ingested catalog: every raw `METHOD:/path` op_id answers
// `unknown_op`, the typed `vmware.vm.info` lookup answers with the given
// envelope, and every other op_id answers `ok` with the given result.
func cdromBackplane(t *testing.T, vmInfo, result map[string]any) (*bytes.Buffer, *[]map[string]any, string) {
	t.Helper()
	var calls []map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			calls = append(calls, body)
			opID, _ := body["op_id"].(string)
			if strings.Contains(opID, ":/") {
				writeJSON(t, w, 200, map[string]any{
					"status": "error", "op_id": opID, "error": "unknown_op: " + opID,
				})
				return
			}
			if opID == opVMInfo {
				writeJSON(t, w, 200, vmInfo)
				return
			}
			writeJSON(t, w, 200, map[string]any{"status": "ok", "op_id": opID, "result": result})
		},
	})
	t.Cleanup(srv.Close)
	primeToken(t, srv.URL)
	return &bytes.Buffer{}, &calls, srv.URL
}

// vmInfoFound is the typed lookup answer for the VM named web-01.
var vmInfoFound = map[string]any{
	"status": "ok", "op_id": opVMInfo,
	"result": map[string]any{"vm": "vm-7", "name": "web-01", "power_state": "poweredOn"},
}

func runCdrom(t *testing.T, cmd *cobra.Command, out *bytes.Buffer, args ...string) error {
	t.Helper()
	cmd.SetOut(out)
	cmd.SetErr(out)
	cmd.SetArgs(args)
	return cmd.Execute()
}

func lastParams(t *testing.T, calls []map[string]any) map[string]any {
	t.Helper()
	if len(calls) == 0 {
		t.Fatal("no operation call reached the backplane")
	}
	params, _ := calls[len(calls)-1]["params"].(map[string]any)
	return params
}

// TestVMCdromMountLibraryISOByItemID — a moid passes through without a
// lookup, and the item id lands as `library_item`.
func TestVMCdromMountLibraryISOByItemID(t *testing.T) {
	out, calls, url := cdromBackplane(t, vmInfoFound, map[string]any{"status": "mounted", "cdrom": "16002"})
	err := runCdrom(t, newVMCdromMountLibraryISOCmd(), out,
		"vm-42", "--target", "vc", "--library-item", "item-1", "--backplane", url)
	if err != nil {
		t.Fatalf("mount-library-iso: %v\n%s", err, out.String())
	}
	if len(*calls) != 1 {
		t.Fatalf("expected one call (no name lookup for a moid), got %d", len(*calls))
	}
	call := (*calls)[0]
	if call["op_id"] != opMountLibraryISO || call["connector_id"] != ConnectorID {
		t.Fatalf("op_id / connector_id: %v / %v", call["op_id"], call["connector_id"])
	}
	params := lastParams(t, *calls)
	if params["vm"] != "vm-42" || params["library_item"] != "item-1" {
		t.Fatalf("params: %v", params)
	}
	if _, present := params["library_item_name"]; present {
		t.Fatalf("library_item_name must not be sent with --library-item: %v", params)
	}
}

// TestVMCdromMountLibraryISOByNamesResolvesTheVM — a VM name is resolved
// to its moid first through the typed vmware.vm.info read (the raw
// GET:/vcenter/vm op is unavailable on the mock backplane), and the item
// name plus library name are passed on.
func TestVMCdromMountLibraryISOByNamesResolvesTheVM(t *testing.T) {
	out, calls, url := cdromBackplane(t, vmInfoFound, map[string]any{"status": "mounted", "cdrom": "16002"})
	err := runCdrom(t, newVMCdromMountLibraryISOCmd(), out,
		"web-01", "--target", "vc", "--library-item-name", "boot.iso", "--library-name", "isos",
		"--backplane", url)
	if err != nil {
		t.Fatalf("mount-library-iso: %v\n%s", err, out.String())
	}
	if len(*calls) != 2 || (*calls)[0]["op_id"] != opVMInfo {
		t.Fatalf("expected the typed VM lookup then the mount, got %v", *calls)
	}
	if lookup, _ := (*calls)[0]["params"].(map[string]any); lookup["name"] != "web-01" {
		t.Fatalf("lookup params: %v", lookup)
	}
	params := lastParams(t, *calls)
	if params["vm"] != "vm-7" || params["library_item_name"] != "boot.iso" || params["library_name"] != "isos" {
		t.Fatalf("params: %v", params)
	}
}

// TestVMCdromMountLibraryISONeedsExactlyOneItemFlag — the item is given
// by id or by name, never neither and never both.
func TestVMCdromMountLibraryISONeedsExactlyOneItemFlag(t *testing.T) {
	for _, args := range [][]string{
		{"vm-42", "--target", "vc"},
		{"vm-42", "--target", "vc", "--library-item", "item-1", "--library-item-name", "boot.iso"},
	} {
		out := &bytes.Buffer{}
		if err := runCdrom(t, newVMCdromMountLibraryISOCmd(), out, args...); err == nil {
			t.Fatalf("args %v: expected a flag error", args)
		}
	}
}

// TestVMCdromMountLibraryISOUnknownVMName — a name the typed lookup does
// not find fails before anything is mounted.
func TestVMCdromMountLibraryISOUnknownVMName(t *testing.T) {
	notFound := "vmware.vm.info: no VM named 'ghost'"
	out, calls, url := cdromBackplane(t, map[string]any{
		"status": "not_found", "op_id": opVMInfo, "error": notFound,
	}, map[string]any{"status": "mounted", "cdrom": "16002"})
	err := runCdrom(t, newVMCdromMountLibraryISOCmd(), out,
		"ghost", "--target", "vc", "--library-item", "item-1", "--backplane", url)
	if err == nil {
		t.Fatalf("expected a resolve error\n%s", out.String())
	}
	if len(*calls) != 1 || (*calls)[0]["op_id"] != opVMInfo {
		t.Fatalf("expected only the typed VM lookup, got %v", *calls)
	}
	if !strings.Contains(out.String(), "no vm named \"ghost\"") {
		t.Fatalf("expected the not-found message, got %s", out.String())
	}
}

// TestVMCdromUnmountLibraryISO — the drive id and the resolved VM moid
// are sent to the unmount op through the same dispatch path.
func TestVMCdromUnmountLibraryISO(t *testing.T) {
	out, calls, url := cdromBackplane(t, vmInfoFound, map[string]any{"status": "unmounted", "cdrom": "16002"})
	err := runCdrom(t, newVMCdromUnmountLibraryISOCmd(), out,
		"web-01", "--target", "vc", "--cdrom", "16002", "--backplane", url)
	if err != nil {
		t.Fatalf("unmount-library-iso: %v\n%s", err, out.String())
	}
	call := (*calls)[len(*calls)-1]
	if call["op_id"] != opUnmountLibraryISO || call["connector_id"] != ConnectorID {
		t.Fatalf("op_id / connector_id: %v / %v", call["op_id"], call["connector_id"])
	}
	params := lastParams(t, *calls)
	if params["vm"] != "vm-7" || params["cdrom"] != "16002" {
		t.Fatalf("params: %v", params)
	}
}

// TestVMCdromUnmountLibraryISONeedsCdrom — --cdrom is required.
func TestVMCdromUnmountLibraryISONeedsCdrom(t *testing.T) {
	out := &bytes.Buffer{}
	if err := runCdrom(t, newVMCdromUnmountLibraryISOCmd(), out, "vm-42", "--target", "vc"); err == nil {
		t.Fatal("expected a required-flag error for --cdrom")
	}
}

// TestVMCmdRegistersCdrom — `meho vmware vm cdrom` hangs off the vm tree.
func TestVMCmdRegistersCdrom(t *testing.T) {
	cmd, _, err := newVMCmd().Find([]string{"cdrom", "mount-library-iso"})
	if err != nil || cmd.Name() != "mount-library-iso" {
		t.Fatalf("vm cdrom mount-library-iso not registered: %v", err)
	}
	cmd, _, err = newVMCmd().Find([]string{"cdrom", "unmount-library-iso"})
	if err != nil || cmd.Name() != "unmount-library-iso" {
		t.Fatalf("vm cdrom unmount-library-iso not registered: %v", err)
	}
}
