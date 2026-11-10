# Organisation setup for the Claude admin

This page is for the person who runs your company's Claude
organisation. It lists the settings that keep MEHO the only path from
Claude to your systems. One person can do it in one sitting.

After it, Claude reaches your systems only through MEHO. MEHO's audit
names the person who asked.

If you are a single user installing the extension, read
[Claude Desktop](claude-desktop.md) instead.

!!! note "Anthropic changes its products"

    Every statement about an Anthropic product below links to the page
    it comes from. All pages were read on 2026-11-10. Menu names and
    setting names can change. Check the linked page before you act.
    Numbers such as retention periods are not copied here on purpose.

## The four layers

MEHO's safety comes in layers. Trust the strongest layer first.

1. **No route and no credentials where Claude runs.** The machine
   cannot reach your systems and holds no keys for them. Claude has
   nothing to use except MEHO. This layer is the strongest. Only the
   machine's owner can set it, not Claude.
2. **MEHO's gates.** Policy, approvals and the audit run inside MEHO,
   for every call, whoever asks.
3. **Harness settings.** Settings in Claude Desktop and Claude Code
   limit what the app does. They help, but they run on a machine the
   user controls.
4. **Rules in `CLAUDE.md`.** These are advice for the model. They are
   **not** a control. Never rely on them to stop an action.

The rest of this page is mostly layer 1 and layer 3, because those are
the parts your Claude admin owns.

## Step 1: Allow only the MEHO extension

A Team or Enterprise owner can allow only the extensions you choose.
Members then cannot add their own.

1. Turn the allowlist on in Organization settings, under Connectors,
   on the Desktop tab. It is off by default. Until you turn it on,
   members can use every extension in the registry.
2. Add the MEHO extension (the `.mcpb` file, see
   [Claude Desktop](claude-desktop.md)) with "Add custom extension",
   then "Add to team".
3. Members need a recent Claude Desktop version for the allowlist to
   work. The page names the minimum.

Turning the allowlist on removes installed extensions that are not on
the list.

A device policy can block extensions on the machine. It wins over the
settings in the app. Do not set `isDesktopExtensionEnabled` or
`isDesktopExtensionDirectoryEnabled` to false on machines that must run
the MEHO extension. On Enterprise, "Block all desktop extensions" also
turns off the MEHO extension. Do not use it if people need MEHO in
Desktop.

Do not rely on the allowlist alone. Layer 1 matters most.

Source: [Enabling and using the desktop extension
allowlist](https://support.claude.com/en/articles/12592343-enabling-and-using-the-desktop-extension-allowlist)
(read 2026-11-10).

## Step 2: Decide what Desktop can touch

Claude Desktop can do more than chat. Know where each part runs.

| Part | Where it runs | What to do |
|---|---|---|
| Chat with code and file creation | In Anthropic's sandbox, not on the computer | An owner can limit it in the organisation settings. Check the network rules. |
| Cowork | On the user's computer (or in Anthropic's cloud, if you allow it) | Turn it off when the computer could reach a system or holds a credential. |
| Claude Code in Desktop | On the user's computer | Turn it off for the same reason. |

**When to turn Cowork and Claude Code in Desktop off:** turn them off
whenever the machine could reach a system without MEHO, or holds a
credential for one. Both can act on the computer. If the machine has no
route and no credential (Step 3), they cannot get around MEHO, and you
can leave them on.

- An owner turns Cowork off for the whole organisation with the
  "Enable for your organization" switch in Organization settings,
  under Cowork. It is on or off for all members. A separate switch
  controls Cowork in Anthropic's cloud.
  Source: [Use Cowork on Team and Enterprise
  plans](https://support.claude.com/en/articles/13455879-use-claude-cowork-on-team-and-enterprise-plans)
  (read 2026-11-10).
- A device policy can turn off Cowork (`secureVmFeaturesEnabled`),
  Claude Code in Desktop (`isClaudeCodeForDesktopEnabled`) and local
  MCP servers (`isLocalDevMcpEnabled`). On Windows these are registry
  values. On macOS they are a configuration profile. The page does not
  list an organisation switch for Claude Code in Desktop, so use the
  device policy for it. For Cowork, use the organisation switch above
  or the device policy.
  Source: [Enterprise configuration for Claude
  Desktop](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)
  (read 2026-11-10).
- Chat code execution and file creation has its own switch and network
  rules.
  Source: [Create and edit files with
  Claude](https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude)
  (read 2026-11-10).

For Claude Code on its own, use Claude Code's managed settings to set
what it may run. Source: [Claude Code
settings](https://code.claude.com/docs/en/settings) (read
2026-11-10). See also [Claude Code](claude-code.md).

## Step 3: Prepare the machine

This is layer 1. Do it on every machine that runs Claude Desktop or
Claude Code for MEHO.

- **No vendor tools.** Do not install the command line tools or SDKs
  of the systems MEHO manages (for example `kubectl`, `govc`, cloud
  CLIs, `ssh` keys).
- **No stored credentials.** No saved passwords, tokens, kubeconfig
  files or cloud profiles for those systems. Keep secrets in MEHO. See
  [Register targets and secrets](../guides/targets-and-secrets.md).
- **No route to the systems.** The firewall lets the machine reach MEHO
  and the login service, and nothing else on the systems' networks. If
  the machine must be on a VPN, give it a VPN profile that reaches only
  MEHO.
- **Trust the CA of your MEHO.** See
  [TLS and ingress](../install/tls-ingress.md#your-workstation-os-trust-store).

How to check: from the machine, try to reach a managed system
directly. It must fail. Then run the same action through MEHO. It must
work.

## Step 4: Check the data settings

On Team and Enterprise plans, Anthropic does not use your chats to
train its models by default. Enterprise owners can set their own
retention period. Read the pages for the current rules and numbers:

- [Is my data used for model
  training?](https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training)
  (read 2026-11-10). It says commercial products, such as Claude for
  Work and the API, are not used for training by default. Feedback you
  send can be used.
- [Custom data retention controls for Claude
  Enterprise](https://privacy.claude.com/en/articles/10440198-custom-data-retention-controls-for-claude-enterprise)
  (read 2026-11-10). It lists what retention does not cover.

Remember that what Claude shows in chat can include data MEHO
returned. MEHO's own audit and retention are set in MEHO, not here.

## Step 5: Log in through your own login service

People log in to MEHO through your company login service. MEHO's audit
then names the person, not a shared account. See
[Company login](../install/login/index.md). Do not give a team one
shared MEHO login or one shared token for Claude.

## What does not work

Claude's remote connectors run in Anthropic's cloud. They need a server
that is open on the public internet. A MEHO behind a VPN is not, and
must not be. So the remote connector cannot reach it. Use the local
extension or the Claude Code plugin instead. See
[Remote Custom Connector: not applicable](index.md#remote-custom-connector-not-applicable).

## Check list for the admin

- [ ] Extension allowlist is on and holds the MEHO extension.
- [ ] No device policy blocks the MEHO extension.
- [ ] Cowork and Claude Code in Desktop are off, or the machine has no
      route and no credentials.
- [ ] Machines have no vendor tools, no stored credentials, and no
      route except to MEHO.
- [ ] Data settings are read and chosen.
- [ ] Login goes through your company login service.
