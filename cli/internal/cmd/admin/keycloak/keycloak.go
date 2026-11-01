// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package keycloak hosts the cobra commands under
// `meho admin keycloak ...` for install-time Keycloak realm
// provisioning. The first verb (G0.9.1-T11 / #791) is
// `bootstrap-clients`, which idempotently provisions:
//
//  1. The public `meho-cli` device-code client + 8 protocol mappers
//     + 4 default client scopes (`basic`, `roles`, `web-origins`,
//     `acr`).
//  2. The public `meho-mcp` authorization-code+PKCE client +
//     the same 8 mappers + 4 default scopes.
//  3. The five MEHO role groups. Their attributes give each person
//     their tenant and role (docs/decisions/login-role-groups.md).
//     Also `MEHO-Platform-Admins`, and on request the five groups of
//     one more tenant (`--add-tenant`).
//  4. An admin user in `MEHO-Admins` and `MEHO-Platform-Admins` with a
//     password.
//
// The verb encodes the 5-step recipe documented in
// deploy/values-examples/README.md § Auth onramp recipe. It does NOT
// provision the confidential `meho-backplane` resource-server client
// or rotate any existing secrets — those are explicitly refused at
// the `--cli-client-id meho-backplane` boundary.
//
// Idempotency: every step does a "does this exist?" check before
// mutating. Re-runs produce `[skip] ...` lines for unchanged
// resources and `[updated] ...` for drift; never duplicates and never
// errors on a re-run against a realm in the desired state.
package keycloak

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"golang.org/x/term"
)

// defaultCLIOfflineSessionIdleSeconds is the offline-session idle
// timeout (48h) set on the CLI device-code client by default, so a
// `meho login` renews itself. Daily use never asks for a new login; a
// weekend without use costs one new login on Monday.
// `--cli-offline-access=0` turns the long CLI login off.
const defaultCLIOfflineSessionIdleSeconds = 172800

// NewRootCmd returns the `meho admin keycloak` parent command, ready
// for cmd/admin/admin.go to graft onto the admin tree.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "keycloak",
		Short: "Provision Keycloak realm resources for the MEHO auth onramp",
		Long: "keycloak hosts install-time provisioning verbs that " +
			"talk to a Keycloak admin REST API directly. The first " +
			"verb is `bootstrap-clients`, which idempotently " +
			"provisions the public device-code client, the public " +
			"MCP browser-flow client, their 8 protocol mappers, " +
			"their 4 default client scopes, the five MEHO role " +
			"groups, MEHO-Platform-Admins, and an admin user — the realm-side prerequisites " +
			"for `meho login` and the MCP onramp.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newBootstrapClientsCmd())
	return cmd
}

// newBootstrapClientsCmd returns the cobra command for
// `meho admin keycloak bootstrap-clients`.
//
// The command reads everything except the two passwords from flags +
// env vars. Passwords (the master-realm admin password and the new
// admin user's password) come from:
//
//  1. KEYCLOAK_ADMIN_PASSWORD / KEYCLOAK_ADMIN_USER_PASSWORD env
//     vars (preferred — survives terminal history, suits CI), or
//  2. Stdin (one line per password, prompted in order, when neither
//     env var is set and stdin is a TTY).
//
// They are never accepted via command-line flags so they cannot land
// in shell history or `ps` output.
func newBootstrapClientsCmd() *cobra.Command {
	var (
		keycloakBaseURL       string
		realm                 string
		adminUsername         string
		cliClientID           string
		mcpClientID           string
		backplaneAudience     string
		mcpResourceURI        string
		tenantID              string
		tenantName            string
		addTenantName         string
		addTenantID           string
		adminUserUsername     string
		adminUserEmail        string
		skipUserProvisioning  bool
		mcpRedirectURIs       []string
		mcpWebOrigins         []string
		keycloakCABundle      string
		insecureSkipTLS       bool
		dryRun                bool
		cliOfflineSessionIdle int
		mcpOfflineSessionIdle int
	)

	cmd := &cobra.Command{
		Use:   "bootstrap-clients",
		Short: "Idempotently provision the public CLI + MCP clients in a Keycloak realm",
		Long: "bootstrap-clients provisions every realm resource " +
			"`meho login` and the MCP browser-flow onramp need:\n\n" +
			"  * the public device-code client (default name `meho-cli`)\n" +
			"  * the public authorization-code+PKCE MCP client (default name `meho-mcp`)\n" +
			"  * 8 protocol mappers on each client (audience-meho-backplane, meho-mcp-audience, groups-claim, and the User Attribute mappers tenant-id, tenant-role, approver, platform-admin and meho-tenants)\n" +
			"  * 4 default client scopes on each client (basic, roles, web-origins, acr) — including the load-bearing `basic` scope that carries the `sub` claim in Keycloak 25+\n" +
			"  * the `mcp:read`, `mcp:execute` and `mcp:admin` client scopes, plus `offline_access`, as optional scopes on the MCP client — MEHO's MCP clients ask for them at login, and Keycloak refuses a login that asks for a scope the client does not offer\n" +
			"  * `offline_access` as an optional scope on the CLI client, with a 48-hour idle limit, so `meho login` gets a login that renews itself (`--cli-offline-access=0` turns it off)\n" +
			"  * the five MEHO role groups, each with a `tenant_id`, a `tenant_role` and a `meho_tenant` attribute:\n" +
			"      MEHO-Readers (read_only), MEHO-Operators (operator), MEHO-SA-Operators (operator),\n" +
			"      MEHO-Admins (tenant_admin), MEHO-Approvers (read_only, plus approver=true)\n" +
			"  * the group MEHO-Platform-Admins (platform_admin=true): rights across all tenants\n" +
			"  * an admin user in MEHO-Admins and MEHO-Platform-Admins with a password\n\n" +
			"A person gets their tenant and role from their group. Put each " +
			"person in exactly one of the five groups. A person in no group " +
			"gets no tenant, and MEHO refuses the login (401 " +
			"missing_tenant_claim). `--tenant-id` sets the `tenant_id` " +
			"attribute on all five groups.\n\n" +
			"More tenants: `--add-tenant <name>` makes the five groups " +
			"MEHO-<name>-Readers, -Operators, -SA-Operators, -Admins and " +
			"-Approvers. They carry only `meho_tenant` " +
			"(`<tenant-uuid>:<name>:<Role>`). Give the tenant's UUID with " +
			"`--add-tenant-id`; without it the command reuses the UUID the " +
			"groups already carry, or makes a new one and prints it. A person " +
			"may be in one role group per tenant. The `meho-tenants` mapper " +
			"lists all of a person's tenants in the `meho_tenants` claim. Never " +
			"set `meho_tenant` on a person.\n\n" +
			"Idempotent: re-runs detect existing resources, " +
			"reconcile drift via PUT, and never duplicate. " +
			"Confidential clients (e.g. `meho-backplane`) are " +
			"explicitly refused — this verb provisions PUBLIC " +
			"clients only.\n\n" +
			"Passwords are read from env vars or stdin; they are " +
			"never accepted via command-line flags.",
		Example: "  # provision against a realm\n" +
			"  KEYCLOAK_ADMIN_PASSWORD=$(vault kv get -field=password secret/.../admin) \\\n" +
			"  KEYCLOAK_ADMIN_USER_PASSWORD='changeme123' \\\n" +
			"  meho admin keycloak bootstrap-clients \\\n" +
			"      --keycloak-base-url https://keycloak.example.com \\\n" +
			"      --realm meho \\\n" +
			"      --admin-username admin \\\n" +
			"      --mcp-resource-uri https://meho.example.com/mcp \\\n" +
			"      --tenant-id 9b7c2e10-3d44-4f6a-91b5-1de8c7a92f04 \\\n" +
			"      --admin-user-username admin@example.com\n",
		// No positional args — flags only. Also guards the
		// --cli-offline-access optional-value flag: pflag's NoOptDefVal
		// treats `--cli-offline-access 3600` (space form) as a
		// positional arg, so NoArgs rejects it instead of dropping it.
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			// Resolve passwords (env first, stdin fallback).
			adminPassword := os.Getenv("KEYCLOAK_ADMIN_PASSWORD")
			adminUserPassword := os.Getenv("KEYCLOAK_ADMIN_USER_PASSWORD")
			if adminPassword == "" {
				p, err := readPassword(
					cmd.InOrStdin(), cmd.ErrOrStderr(),
					"Keycloak master-realm admin password: ",
				)
				if err != nil {
					return fmt.Errorf("read admin password: %w", err)
				}
				adminPassword = p
			}
			if !skipUserProvisioning && adminUserPassword == "" {
				p, err := readPassword(
					cmd.InOrStdin(), cmd.ErrOrStderr(),
					fmt.Sprintf("Password for new user %q: ", adminUserUsername),
				)
				if err != nil {
					return fmt.Errorf(
						"read admin user password: %w", err)
				}
				adminUserPassword = p
			}

			opts := BootstrapOptions{
				KeycloakBaseURL:              keycloakBaseURL,
				Realm:                        realm,
				AdminUsername:                adminUsername,
				AdminPassword:                adminPassword,
				CLIClientID:                  cliClientID,
				MCPClientID:                  mcpClientID,
				BackplaneAudience:            backplaneAudience,
				MCPResourceURI:               mcpResourceURI,
				TenantID:                     tenantID,
				TenantName:                   tenantName,
				AddTenantName:                addTenantName,
				AddTenantID:                  addTenantID,
				AdminUserUsername:            adminUserUsername,
				AdminUserEmail:               adminUserEmail,
				AdminUserPassword:            adminUserPassword,
				SkipUserProvisioning:         skipUserProvisioning,
				CLIOfflineSessionIdleSeconds: cliOfflineSessionIdle,
				MCPOfflineSessionIdleSeconds: mcpOfflineSessionIdle,
				MCPRedirectURIs:              mcpRedirectURIs,
				MCPWebOrigins:                mcpWebOrigins,
				DryRun:                       dryRun,
				Out:                          cmd.OutOrStdout(),
				Err:                          cmd.ErrOrStderr(),
			}

			// TLS trust selection (F08 / #270). The package-level
			// Bootstrap uses defaultHTTPClient() (system trust store)
			// unless we stash a custom client via the package-internal
			// httpClientOverride indirection. The two overrides are
			// mutually exclusive: a CA-bundle pin keeps verification on
			// (preferred), a blanket skip turns it off (escape hatch).
			if keycloakCABundle != "" && insecureSkipTLS {
				return errors.New(
					"--keycloak-ca-bundle and --insecure-skip-tls-verify are " +
						"mutually exclusive: pin the realm CA (verified TLS) or " +
						"skip verification entirely, not both")
			}
			if keycloakCABundle != "" {
				// Verified TLS against the pinned realm CA — the
				// admin-password grant and every Bearer-token request
				// run over a checked certificate chain + hostname.
				client, cerr := newCABundleClient(keycloakCABundle)
				if cerr != nil {
					return cerr
				}
				httpClientOverride = client
				defer func() { httpClientOverride = nil }()
			}
			if insecureSkipTLS {
				// Escape hatch: verification off. Loud, opt-in, and never
				// the default — the master-realm admin password and the
				// minted admin token cross an unverified connection, so
				// an active intermediary could capture both.
				fmt.Fprintln(cmd.ErrOrStderr(),
					"WARNING: --insecure-skip-tls-verify disables TLS "+
						"certificate AND hostname verification for every "+
						"Keycloak request. The master-realm admin password and "+
						"the minted admin token will be sent over an unverified "+
						"connection. Prefer --keycloak-ca-bundle=<realm-ca.pem> "+
						"to pin the realm CA with verification left on.")
				httpClientOverride = newInsecureClient()
				defer func() { httpClientOverride = nil }()
			}

			res, err := Bootstrap(cmd.Context(), opts)
			if err != nil {
				return err
			}

			printSummary(cmd.OutOrStdout(), opts, res)
			return nil
		},
	}

	cmd.Flags().StringVar(&keycloakBaseURL, "keycloak-base-url", "",
		"Keycloak base URL, e.g. https://keycloak.example.com")
	cmd.Flags().StringVar(&realm, "realm", "",
		"target realm name (NOT the master realm — that's where the admin token is minted)")
	cmd.Flags().StringVar(&adminUsername, "admin-username",
		os.Getenv("KEYCLOAK_ADMIN_USER"),
		"master-realm admin username (or set KEYCLOAK_ADMIN_USER)")
	cmd.Flags().StringVar(&cliClientID, "cli-client-id", "meho-cli",
		"public client_id for the device-code flow (matches chart's `config.keycloakCliClientId`)")
	cmd.Flags().StringVar(&mcpClientID, "mcp-client-id", "meho-mcp",
		"public client_id for the MCP browser-flow client")
	cmd.Flags().StringVar(&backplaneAudience, "backplane-audience", "meho-backplane",
		"audience claim the `audience-meho-backplane` mapper emits (matches chart's `config.keycloakAudience`)")
	cmd.Flags().StringVar(&mcpResourceURI, "mcp-resource-uri", "",
		"audience the `meho-mcp-audience` mapper emits, e.g. https://meho.example.com/mcp (no trailing slash)")
	cmd.Flags().StringVar(&tenantID, "tenant-id", "",
		"tenant UUID written as the `tenant_id` attribute on the five MEHO role groups "+
			"(default: the all-zero UUID). Changing it later moves everyone to a new tenant")
	cmd.Flags().StringVar(&tenantName, "tenant-name", defaultTenantName,
		"the first tenant's short name (lowercase letters, digits, dashes), written into "+
			"the `meho_tenant` attribute of the five MEHO role groups")
	cmd.Flags().StringVar(&addTenantName, "add-tenant", "",
		"also make the five groups `MEHO-<name>-<Role>` of one more tenant with this short name")
	cmd.Flags().StringVar(&addTenantID, "add-tenant-id", "",
		"the UUID of the `--add-tenant` tenant (default: the UUID its groups already carry, "+
			"else a new one, printed at the end)")
	cmd.Flags().StringVar(&adminUserUsername, "admin-user-username", "",
		"username of the admin user to provision in MEHO-Admins and MEHO-Platform-Admins "+
			"(required unless --skip-user-provisioning)")
	cmd.Flags().StringVar(&adminUserEmail, "admin-user-email", "",
		"optional email for the new admin user")
	cmd.Flags().BoolVar(&skipUserProvisioning, "skip-user-provisioning", false,
		"skip the admin user step (use when people come from a directory or another login system); "+
			"the five role groups are still created")
	cmd.Flags().StringSliceVar(&mcpRedirectURIs, "mcp-redirect-uri", nil,
		"redirect URI(s) for the MCP browser-flow client (default: loopback localhost + 127.0.0.1, any port/path)")
	cmd.Flags().StringSliceVar(&mcpWebOrigins, "mcp-web-origin", nil,
		"CORS web origin(s) for the MCP browser-flow client (default: `+` — allow the redirect-URI origins)")
	cmd.Flags().StringVar(&keycloakCABundle, "keycloak-ca-bundle", "",
		"path to a PEM CA bundle to verify the Keycloak server certificate against "+
			"(keeps chain + hostname verification ON while trusting an internal realm CA). "+
			"Preferred over --insecure-skip-tls-verify; the two are mutually exclusive")
	cmd.Flags().BoolVar(&insecureSkipTLS, "insecure-skip-tls-verify", false,
		"escape hatch: skip TLS certificate AND hostname verification for every "+
			"Keycloak request (sends the admin password + token over an unverified "+
			"connection; prints a loud warning). Prefer --keycloak-ca-bundle")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"print what would be provisioned without making any API calls")
	cmd.Flags().IntVar(&cliOfflineSessionIdle, "cli-offline-access",
		defaultCLIOfflineSessionIdleSeconds,
		"how many seconds an unused CLI login stays valid: assigns "+
			"`offline_access` as an optional client scope on the device-code "+
			"CLI client and sets its `client.offline.session.idle.timeout` "+
			"attribute, so `meho login` gets a login that renews itself. "+
			"Default "+strconv.Itoa(defaultCLIOfflineSessionIdleSeconds)+
			" seconds (48h). Use the equals form for another value, e.g. "+
			"`--cli-offline-access=86400`. `--cli-offline-access=0` turns "+
			"the long CLI login off. The refresh token is a long-lived "+
			"secret on the operator's disk (OS keyring first)")
	cmd.Flags().IntVar(&mcpOfflineSessionIdle, "mcp-offline-session-idle",
		defaultMCPOfflineSessionIdleSeconds,
		"how many seconds an unused MCP login stays valid: sets the "+
			"`client.offline.session.idle.timeout` attribute on the MCP "+
			"browser-flow client. Default 48h. Without it the realm's "+
			"Offline Session Idle applies (30 days by default)")
	// Optional-value flag: a bare `--cli-offline-access` also yields the
	// 48h default, so older scripts keep working; a custom value MUST
	// use the equals form (`--cli-offline-access=SECONDS`). pflag's
	// NoOptDefVal treats a space-separated value as a positional arg,
	// which the cobra.NoArgs guard above then rejects rather than
	// silently dropping.
	cmd.Flags().Lookup("cli-offline-access").NoOptDefVal =
		strconv.Itoa(defaultCLIOfflineSessionIdleSeconds)

	// `--keycloak-base-url` and `--realm` and `--admin-username` are
	// mandatory; mark them so cobra-generated --help is precise.
	_ = cmd.MarkFlagRequired("keycloak-base-url")
	_ = cmd.MarkFlagRequired("realm")

	return cmd
}

// httpClientOverride is the package-internal indirection used by the
// --insecure-skip-tls-verify flag. nil → defaultHTTPClient() is used.
// Set by RunE, consumed by mintAdminToken / newAdminClient via the
// Bootstrap wrapper at the top of bootstrap.go.
//
// We avoid plumbing the *http.Client through every public type
// because the only legitimate need today is the insecure-skip flag,
// and a per-call argument bloats the surface area for callers that
// don't care. Tests use httptest.Server with default TLS (no skip
// needed) and inject via the exported `BootstrapWithClient` shim
// below.
var httpClientOverride *http.Client

// newInsecureClient builds an http.Client that skips TLS verification.
// Used by the --insecure-skip-tls-verify flag at install-time
// bootstrap, mirroring the reference shell script's `curl -k`. Not
// safe for general use.
func newInsecureClient() *http.Client {
	c := defaultHTTPClient()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = newSkipVerifyTLSConfig()
	c.Transport = transport
	return c
}

// newCABundleClient builds an http.Client that verifies the Keycloak
// server certificate against the PEM CA bundle at caBundlePath, with
// certificate-chain and hostname verification left ON. Used by the
// --keycloak-ca-bundle flag — the verified-TLS alternative to
// --insecure-skip-tls-verify for a realm fronted by an internal CA.
// Returns an error when the bundle can't be read or holds no PEM
// certificates.
func newCABundleClient(caBundlePath string) (*http.Client, error) {
	tlsConfig, err := newCABundleTLSConfig(caBundlePath)
	if err != nil {
		return nil, err
	}
	c := defaultHTTPClient()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = tlsConfig
	c.Transport = transport
	return c, nil
}

// readPassword prompts on stderr and reads one line from in, returning
// the trimmed string. When in is a terminal it reads with echo
// suppressed via golang.org/x/term, so a secret typed at the prompt
// never lands in the terminal, scrollback, or a session recording;
// piped / non-TTY input (unit tests, a here-string) falls back to a
// plain buffered line read so the cobra Run never hangs.
func readPassword(in io.Reader, errOut io.Writer, prompt string) (string, error) {
	if _, err := fmt.Fprint(errOut, prompt); err != nil {
		return "", err
	}
	var line string
	if f, ok := in.(*os.File); ok && term.IsTerminal(int(f.Fd())) {
		b, err := term.ReadPassword(int(f.Fd()))
		// ReadPassword consumes the trailing Enter without echoing
		// it, so emit the newline the operator expects to see.
		fmt.Fprintln(errOut)
		if err != nil {
			return "", err
		}
		line = string(b)
	} else {
		raw, err := bufio.NewReader(in).ReadString('\n')
		if err != nil && !errors.Is(err, io.EOF) {
			return "", err
		}
		line = raw
	}
	line = strings.TrimRight(line, "\r\n")
	if line == "" {
		return "", errors.New("empty password")
	}
	return line, nil
}

// printSummary writes the final progress block — the per-mutation
// events + the "set these in your chart" config keys.
func printSummary(out io.Writer, opts BootstrapOptions, res *Result) {
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "==> realm reconciliation events")
	for _, line := range res.MapperEventsLog {
		fmt.Fprintln(out, "    "+line)
	}
	if !opts.SkipUserProvisioning {
		switch {
		case res.AdminUserCreated:
			fmt.Fprintf(out, "    [created] user %s (joined %s and %s)\n",
				opts.AdminUserUsername, adminGroupName, platformAdminGroupName)
		case res.AdminUserJoined:
			fmt.Fprintf(out, "    [updated] user %s (joined %s and %s; password not reset)\n",
				opts.AdminUserUsername, adminGroupName, platformAdminGroupName)
		default:
			fmt.Fprintf(out, "    [skip]    user %s (already exists; password not reset)\n",
				opts.AdminUserUsername)
		}
	}
	if opts.AddTenantName != "" && res.AddedTenantID != "" {
		note := ""
		if res.AddedTenantIDNew {
			note = " (new; keep it)"
		}
		fmt.Fprintf(out, "\n==> tenant %s: id %s%s\n",
			opts.AddTenantName, res.AddedTenantID, note)
	}
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "==> set these on the chart side to match the realm:")
	for _, line := range res.ConfigKeysToSet {
		fmt.Fprintln(out, "    "+line)
	}
	fmt.Fprintln(out, "")
	fmt.Fprintln(out,
		"Ready for: `meho login <backplane-url>` (after the chart redeploys with the new value).")
}
