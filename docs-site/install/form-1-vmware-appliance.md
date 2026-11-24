# Form 1: the VMware appliance

!!! note "In progress, not released"

    This chapter has no steps yet. There is no download. Ask evoila
    when the appliance is released.

The appliance is one OVA file (a ready-made virtual machine) for VMware
vSphere and VMware Cloud Foundation. You import it into vCenter, fill in
a name, an address, DNS and a time server, and start it. Everything is
inside, and it needs no internet.

Until then, use [form 2, the Linux server](form-2-linux-server.md), or
go back to [Which install form is right for you?](which-form.md).

The appliance has the same host command as form 2, `meho-host`. For
example, when a person of a local account lost the one-time code app and
the recovery codes, see
[A person lost the one-time code app](form-2-linux-server.md#a-person-lost-the-one-time-code-app).

When the appliance is ready, the steps after the install are the same
as for every form. Start with
[the setup page and the settings file](setup-page.md).

**Plan for a second platform admin.** An admin makes most changes of rights alone in the web console. In MEHO's own login realm, a change asked with a token, the command line, MCP or an agent, a change of the approval sign-in or of most realm settings, and a move of a secret into your own password need a platform admin other than you. See [who approves a change of rights](../guides/approvals-and-break-glass.md#changes-of-rights-need-another-admin).
