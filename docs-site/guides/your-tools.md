# Your tools: sign out one tool

You can use MEHO through several tools: the web console, the `meho`
command line, Claude Code, Claude Desktop and other MCP clients. The
account page lets you end the saved login of one tool in one click. Your
other tools keep working.

This is a label and an off-switch, not a lock: programs that run as the
same user on one computer can read each other's saved logins.

## Your tools

Open your account page in the console: click your name in the sidebar,
or go to `/ui/account`. The card **Your tools** lists each tool kind:

- the name of the tool kind;
- its login client in Keycloak (for example `meho-mcp-desktop`);
- its last use: the newest entry of the audit log for you and that tool,
  in the last 90 days. MEHO looks only at your newest 500 entries. It
  shows "none in your newest 500 audit rows of the last 90 days" when
  none of them is from that tool.

| Tool kind | Login client |
|---|---|
| Web console | the console's client |
| Command line | `meho-cli` |
| Claude Code | `meho-mcp-code` |
| Claude Desktop | `meho-mcp-desktop` |
| Other MCP client | `meho-mcp` |

### Sign out one tool

1. Click **Sign out this tool** next to the tool.
2. Read the box and click **Sign out**.

MEHO asks Keycloak to end the logins of that one tool: the offline
login (it keeps a tool signed in for a long time) and each ordinary
login. Then:

- **The tool stops when its last access token ends.** That is within the
  access token lifetime: at most about five and a half minutes on MEHO's
  built-in Keycloak. The tool's saved login ends.
- **Your other tools keep working.** For example, if you sign out Claude
  Desktop, Claude Code and the command line keep their logins.
- **It is not a block.** The tool can sign in again. If your browser is
  still signed in to MEHO's Keycloak, that can happen without a question.
  If it is not, the tool asks you to log in in the browser. Either way,
  the tool gets a new login.
- **The audit log records it** with the entry `auth.tool_sign_out` and
  the login client.

Sometimes two tools share one login. This happens when both signed in
through the same browser login, for example Claude Desktop and the
command line. Keycloak can end such a login only for both tools at once.
So MEHO keeps it, and your other tool keeps working. Then the card says
"Partly signed out" and names the other tool. While that login lasts,
the signed-out tool can still get new access tokens through it. To stop
that too, end the login in your Keycloak account page. Then the other
tool must sign in again.

If Keycloak has no login of that tool for you, the card says "Nothing
changed". If a call to Keycloak fails, the card shows an error. Try again
later. The audit log records the failed try too: what ended before the
failure, if anything, and the error. If Keycloak refuses the call, the
card says that MEHO's Keycloak admin client needs the right
`manage-users`. Ask your MEHO admin.

The web console has no button here. To sign out a browser, use the card
**Active sessions** on the same page.

You can sign out only your own tools.

## An admin signs out a tool for someone else

A tenant admin can sign out one tool of another person, for example
when a laptop with Claude Desktop is lost. There is no console page for
this. Use the command line:

```bash
meho admin tools sign-out --user <username or user id> --tool claude-desktop
```

- `--user` is the person's username or Keycloak user id in the realm
  MEHO uses for sign-in. MEHO finds only people of your tenant.
- `--tool` is `cli`, `claude-code`, `claude-desktop` or `other-mcp`. The
  web console cannot be signed out this way.
- You need the `tenant_admin` role. The call waits until a person
  approves it, in the console or with `meho approvals approve`. Every
  tenant admin may approve their own request, so no second person is
  needed.
- While `MCP_CLIENT_PER_TOOL_KIND` is off, MEHO refuses the call at once
  and makes no approval request.
- The approval shows the person by user id. MEHO signs out that user id,
  even if the username points at someone else by then.
- The person's other tools keep working.
- The sign-out ends the tool's login in the whole sign-in realm, not only
  in your tenant. A person who is in two tenants also loses that tool's
  login for the other tenant.
- The same limits hold as for your own sign-out: the tool's last access
  token works for a few more minutes, and the tool can sign in again.
  To stop the person, disable their account in Keycloak.
- The audit entry `auth.tool_sign_out` names the admin who ran the call
  (`operator_sub`: the admin who approved it), the admin who asked
  (`requested_by`), the person (`user_id`, `username`) and the login
  client. When you approve your own request, `operator_sub` and
  `requested_by` are both you. If the call fails, the command shows the
  error, and no "signed out" entry is written.

## Before you start

The card shows only when your MEHO admin has given each AI tool its own
login client: the setting `MCP_CLIENT_PER_TOOL_KIND` is on. See
[Claude Code](../clients/claude-code.md#a-login-of-its-own-for-claude-code).

For the admin:

- The last use needs the setting `AUDIT_CLIENT_IDENTITY`, which names the
  login client in each audit entry. Without it, no tool shows a last
  use, and the sign-out still works.
- MEHO signs a tool out with its Keycloak admin client. That client needs
  the realm role `manage-users`. MEHO's built-in Keycloak has it. On your
  own Keycloak, give it to the admin client of MEHO, or the card shows an
  error.
