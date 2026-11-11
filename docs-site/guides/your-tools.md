# Your tools: sign out one tool

You can use MEHO through several tools: the web console, the `meho`
command line, Claude Code, Claude Desktop and other MCP clients. When you
no longer trust one of them, you can stop it in one click. Your other
tools keep working.

## Your tools

Open your account page in the console: click your name in the sidebar,
or go to `/ui/account`. The card **Your tools** lists each tool kind:

- the name of the tool kind;
- its login client in Keycloak (for example `meho-mcp-desktop`);
- its last use: the newest entry of the audit log for you and that tool,
  in the last 90 days. It shows "none in the last 90 days" when there is
  none.

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

- **The tool stops within about five minutes.** Its last access token
  works until it ends: at most about five and a half minutes on MEHO's
  built-in Keycloak. The tool cannot get a new token.
- **Your other tools keep working.** For example, if you sign out Claude
  Desktop, Claude Code and the command line keep their logins.
- **It is not a block.** The tool can sign in again. Then it asks you to
  log in in the browser and gets a new login.
- **The audit log records it** with the entry `auth.tool_sign_out` and
  the login client.

Sometimes two tools share one login. This happens when both signed in
through the same browser login, for example Claude Desktop and the
command line. Keycloak can end such a login only for both tools at once.
So MEHO keeps it, and your other tool keeps working. The card names the
other tool. While that login lasts, the signed-out tool can still get new
access tokens through it. To stop that too, end the login in your
Keycloak account page.

If Keycloak has no login of that tool for you, the card says "Nothing
changed". If the call to Keycloak fails, the card shows an error and the
audit log gets no sign-out entry. Try again later.

The web console has no button here. To sign out a browser, use the card
**Active sessions** on the same page.

You can sign out only your own tools.

## Before you start

The card shows only when your MEHO admin has given each AI tool its own
login client: the setting `MCP_CLIENT_PER_TOOL_KIND` is on. See
[Claude Code](../clients/claude-code.md#a-login-of-its-own-for-claude-code).

For the admin:

- The last use needs the setting `AUDIT_CLIENT_IDENTITY`, which names the
  login client in each audit entry. Without it, every tool shows "none in
  the last 90 days", and the sign-out still works.
- MEHO signs a tool out with its Keycloak admin client. That client needs
  the realm role `manage-users`. MEHO's built-in Keycloak has it. On your
  own Keycloak, give it to the admin client of MEHO, or the card shows an
  error.
