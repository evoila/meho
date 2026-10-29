// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

const (
	opMountLibraryISO   = "vmware.composite.vm.cdrom.mount_library_iso"
	opUnmountLibraryISO = "vmware.composite.vm.cdrom.unmount_library_iso"
)

// newVMCdromCmd returns the `meho vmware vm cdrom` parent with the two
// content-library ISO verbs (#3576). Both are thin wrappers: they resolve
// the VM name to its id on the client, like `vm info`, and dispatch the
// composite op_id through the shared dispatch path. The backend waits for
// approval; the approval names the VM id, never a name.
func newVMCdromCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "cdrom",
		Short:        "VM CD drive verbs (mount-library-iso / unmount-library-iso)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newVMCdromMountLibraryISOCmd())
	cmd.AddCommand(newVMCdromUnmountLibraryISOCmd())
	return cmd
}

type vmCdromMountOpts struct {
	TargetName        string
	LibraryItem       string
	LibraryItemName   string
	LibraryName       string
	JSONOut           bool
	BackplaneOverride string
}

// newVMCdromMountLibraryISOCmd returns `meho vmware vm cdrom mount-library-iso`.
func newVMCdromMountLibraryISOCmd() *cobra.Command {
	var opts vmCdromMountOpts
	cmd := &cobra.Command{
		Use:   "mount-library-iso <vm-name-or-id>",
		Short: "Mount an ISO from a content library on a VM, as a new CD drive",
		Long: "mount-library-iso dispatches op_id=\"" + opMountLibraryISO + "\"\n" +
			"against the connector_id=\"vmware-rest-9.0\" connector. vCenter adds a\n" +
			"new CD drive to the VM, backed by the ISO file of the library item,\n" +
			"and the answer carries the new drive id (cdrom). Keep it: the\n" +
			"unmount needs it.\n\n" +
			"<vm-name-or-id> accepts a VM name (resolved client-side to a moid)\n" +
			"or a moid directly. Give the library item by id (--library-item) or\n" +
			"by name (--library-item-name, optionally inside one library with\n" +
			"--library-name). A name that fits no item, or more than one, is\n" +
			"refused before anything is sent.\n\n" +
			"The call waits for approval. The approval preview shows the item id\n" +
			"the name resolved to; the approved run mounts exactly that item.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware vm cdrom mount-library-iso web-01 --target rdc-vcenter --library-item 0b6e3e0c-1f2a-4c55-9a51-2f6d7a8b9c01\n" +
			"  meho vmware vm cdrom mount-library-iso vm-42 --target rdc-vcenter --library-item-name boot.iso --library-name isos",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runVMCdromMountLibraryISO(cmd, args[0], opts)
		},
	}
	cmd.Flags().StringVar(&opts.TargetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&opts.LibraryItem, "library-item", "",
		"id of the content-library item that holds the ISO file")
	cmd.Flags().StringVar(&opts.LibraryItemName, "library-item-name", "",
		"name of the library item, looked up on the backend; refused when it fits 0 or >1 items")
	cmd.Flags().StringVar(&opts.LibraryName, "library-name", "",
		"library to look --library-item-name up in (optional)")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.MarkFlagsOneRequired("library-item", "library-item-name")
	cmd.MarkFlagsMutuallyExclusive("library-item", "library-item-name")
	return cmd
}

func runVMCdromMountLibraryISO(cmd *cobra.Command, nameOrID string, opts vmCdromMountOpts) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	moid, err := resolveName(cmd.Context(), backplaneURL, opts.TargetName, "vm", nameOrID)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			&output.StructuredError{Code: "resolve_failed", Detail: err.Error(), Exit: 1},
			opts.JSONOut)
	}
	params := map[string]any{"vm": moid}
	if opts.LibraryItem != "" {
		params["library_item"] = opts.LibraryItem
	} else {
		params["library_item_name"] = opts.LibraryItemName
		if opts.LibraryName != "" {
			params["library_name"] = opts.LibraryName
		}
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, opMountLibraryISO, opts.TargetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	return conn.Render(cmd, opMountLibraryISO, r, opts.JSONOut, nil)
}

// newVMCdromUnmountLibraryISOCmd returns `meho vmware vm cdrom unmount-library-iso`.
func newVMCdromUnmountLibraryISOCmd() *cobra.Command {
	var (
		targetName        string
		cdrom             string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "unmount-library-iso <vm-name-or-id>",
		Short: "Unmount a content-library ISO from a VM and remove its CD drive",
		Long: "unmount-library-iso dispatches op_id=\"" + opUnmountLibraryISO + "\"\n" +
			"against the connector_id=\"vmware-rest-9.0\" connector. vCenter removes\n" +
			"the CD drive that mount-library-iso added.\n\n" +
			"<vm-name-or-id> accepts a VM name (resolved client-side to a moid)\n" +
			"or a moid directly. --cdrom is the drive id the mount returned;\n" +
			"vmware.vm.hardware.show lists it too.\n\n" +
			"The call waits for approval.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho vmware vm cdrom unmount-library-iso web-01 --target rdc-vcenter --cdrom 16002",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runVMCdromUnmountLibraryISO(cmd, args[0], targetName, cdrom, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&cdrom, "cdrom", "", "id of the CD drive, as mount-library-iso returned it; required")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("cdrom")
	return cmd
}

func runVMCdromUnmountLibraryISO(cmd *cobra.Command, nameOrID, targetName, cdrom string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	moid, err := resolveName(cmd.Context(), backplaneURL, targetName, "vm", nameOrID)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			&output.StructuredError{Code: "resolve_failed", Detail: err.Error(), Exit: 1},
			jsonOut)
	}
	params := map[string]any{"vm": moid, "cdrom": cdrom}
	r, err := conn.Call(cmd.Context(), backplaneURL, opUnmountLibraryISO, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opUnmountLibraryISO, r, jsonOut, nil)
}
