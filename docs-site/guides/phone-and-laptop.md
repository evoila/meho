# MEHO on your phone and laptop

This page explains how to use MEHO on a phone or a laptop:

- install MEHO as an app, so it opens in its own window on the approvals
  page;
- turn on a notification for each request that waits for your approval;
- sign in and approve with a passkey.

The app shows only pages of your MEHO. Your phone or laptop needs the
same network access as the web console, for example the VPN.

## Install MEHO as an app

1. Open MEHO in the browser, over the VPN.
2. Install it, as in the table below. The account page of MEHO shows the
   same steps, under **Install MEHO as an app**.

| Device | Browser | How you install |
|---|---|---|
| iPhone or iPad | Safari | Tap **Share**, then **Add to Home Screen**. |
| Android phone | Chrome | Open the menu (three dots), then tap **Install app** or **Add to Home screen**. |
| Mac or Windows laptop | Chrome or Edge | Click the install icon at the right end of the address bar, then **Install**. |
| Mac | Safari | Choose **File**, then **Add to Dock**. |

The app opens on the approvals page. Installing needs the `https://`
address of your MEHO.

**With the VPN off**, the app shows the page "MEHO is not reachable.
Connect to the VPN and try again." It never shows an old copy of the
approvals list. The app keeps only its icons, styles and this offline
page on the device. It keeps no approval, broadcast or API answer.

## Get a notification when an approval waits

MEHO can send a notification to your phone or laptop when a request waits
for your approval. You turn it on once for each browser or installed app.

### Before you start

- Your admin has turned the feature on (see
  [For admins](#for-admins-turn-notifications-on)). Without it, the account
  page has no notification switch.
- **iPhone and iPad:** install MEHO on the home screen first (iOS 16.4 or
  newer), and turn the switch on inside the installed app. Safari on an
  iPhone shows no web notification outside an installed app.
- **Android, Mac and Windows:** the switch works in the installed app and
  in the normal browser.

### Turn it on

1. In MEHO (the installed app, or the browser), open **Account** from the
   user menu.
2. Under **Approval notifications**, switch on **Notify me about
   approvals in this browser**.
3. The browser asks you to allow notifications. Allow them.
4. The page says "Notifications are on in this browser." Below the switch,
   the list shows each browser of yours that gets notifications.

To turn it off, switch it off again on the same device. To stop a lost or
old device, click **Remove** next to it in the list. A tenant admin can
remove any browser of their tenant in the same list.

If the page says "Notifications are blocked for this site in the browser
settings", allow notifications for your MEHO address in the browser or
phone settings, then switch it on again.

### What the notification says

The notification holds only these things:

| When | Title | Text |
|---|---|---|
| A request waits for your approval | An approval waits | Tenant `<tenant name>`. Request `<request id>`. |
| The request is decided or ends | Approval approved, Approval rejected, Approval expired or Approval withdrawn | The same text. It replaces the first notification on the device. |
| More requests came within 30 seconds | Approvals changed | `<n>` more approval messages. Open the approvals page. |

A notification never holds the operation, its parameters or the target.
It is encrypted on its way to your device, so the push service of the
browser maker cannot read the text. The push service does see the
request id (see [What leaves your network](#what-leaves-your-network)).

A tap opens the approvals page of your MEHO. You approve there as always:
with a fresh sign-in or a passkey, and a click. A notification does not
let you approve, and it does not change who may approve.

### The VPN

- The notification arrives **without** the VPN. It comes through the push
  service of your browser maker (Apple, Google, Microsoft or Mozilla), not
  from MEHO directly.
- Opening it **needs** the VPN, because the approvals page comes from your
  MEHO. Without the VPN you see the offline page. Connect to the VPN and
  tap the notification again, or open the app.

### Who gets a notification

You get one when you may approve the request, by the same rules as the
approve button: your role in that tenant, your login groups and the risk
level of the operation. You get none for requests in other tenants.

MEHO reads your login groups when you turn the switch on, at each sign-in
to the web console and each time you open the account page. So:

- **Sign in to the web console at least once a week.** When your last
  sign-in is more than 7 days old, MEHO sends you no notification until
  you sign in again. Your groups may have changed since.
- A change of your login groups counts from your next sign-in.

### When no notification comes

| What you see | What to do |
|---|---|
| The account page has no **Approval notifications** part | The feature is off. Ask your admin to turn it on. |
| The switch says the browser cannot show notifications | On an iPhone or iPad, install MEHO on the home screen and turn the switch on in the app. Otherwise use a newer browser. |
| The switch is on, but nothing arrives | Sign in to the web console once (see above). Check that the phone allows notifications for the app and is not in a focus or do-not-disturb mode. |
| Only one notification for many requests | That is the limit: at most one "An approval waits" per tenant every 30 seconds, then one summary. Open the approvals page to see all requests. |
| The notifications stopped after an upgrade | Sign in to the web console once. |

## For admins: turn notifications on

The feature is off until an admin turns it on for the whole MEHO.

1. Allow MEHO to reach the push services of the browser makers. See
   [Push services for approval notifications](../install/index.md#push-services-for-approval-notifications)
   for the host names and the network rule.
2. Set `webPush.enabled: true` in your Helm values, and upgrade.

With a built-in core part (the default), the install makes the push key
(VAPID: a key that proves the messages come from your MEHO) once and
keeps it on every upgrade. Without a built-in core part, store your own
key in a Secret and set `webPush.secretName`. The details are in
[`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md#operational-chart-knobs),
under `webPush.*`. MEHO never logs or shows the key.

With more than one copy of MEHO running (`replicaCount` above 1), each
copy keeps its own limit of one message every 30 seconds, and its own
order of the messages of one request. So a person may get more
messages, and in rare cases a result may arrive before its "An approval
waits".

### What leaves your network

Each notification is one HTTPS request from MEHO to the push service of
the browser maker. It carries:

- the title and text from the table above: the words, the tenant name and
  the request id;
- the address of the approvals page, as a path (`/ui/approvals`);
- the address of your MEHO, in the signed key token (when it is an
  `https://` address);
- the push address of the person's browser, which the browser maker made;
- the request id again, readable, in the `Topic` header. With it the push
  service replaces a waiting message by its result, when the first one
  was not delivered yet. A summary message has the fixed word
  `approvalsummary` there.

The title, text and page address are encrypted for the browser. The push
service sees the push address, the token, the request id in the `Topic`
header, and the size and time of the message. With the request id it can
tell which messages belong to the same request. The request id is a
random number: it says nothing about the operation. Nothing else of the
request leaves your network: no operation, no parameters, no target and
no name of a person.

## Sign in with a passkey

A passkey is a login key that your device keeps for you. You unlock it
with Face ID, Touch ID, a fingerprint or Windows Hello. With a passkey:

- you sign in to MEHO with one tap, with no password and no one-time code;
- the sign-in for an approval is one tap too. A passkey counts as the
  fresh sign-in that an approval needs. The time limits stay the same:
  the sign-in may be at most 15 minutes old, and 5 minutes for a
  `destructive` operation;
- a fake login page cannot use it. The passkey works only on your MEHO's
  login page.

Your password and your one-time code keep working. Use them on a device
that has no passkey.

### Before you start

- Your MEHO login works with your password, and your one-time code if you
  have one.
- Your MEHO uses its built-in Keycloak, or a Keycloak with passkeys on.
  The admin page for this is
  [Remember the browser, and passkeys](../install/keycloak-realm.md#remember-the-browser-and-passkeys).
  With a company login, you add the passkey at your company's login page,
  not at MEHO's (see
  [Your own Keycloak](../install/login/customer-keycloak.md)).
- The device has a screen lock (a PIN, a fingerprint or a face).

### Add a passkey, once per device

Each device gets its own passkey, unless your devices share passkeys
through the same Apple or Google account.

1. On the device, open the Keycloak account page of your MEHO:
   `https://<meho-host>/auth/realms/meho/account`.
2. Sign in with your password (and your one-time code).
3. Open **Account security → Signing in**.
4. In the **Passwordless** part, click **Set up Passkey**. (The
   **Two-factor authentication** part has a passkey too: that one does
   not replace the password.) Keycloak may ask for your password again
   first.
5. Confirm on the device, as in the table below. Then give the passkey a
   name, for example `iPhone` or `work laptop`.

| Device | Browser | How you confirm | Where the passkey is kept |
|---|---|---|---|
| iPhone or iPad (iOS 16 or newer) | Safari | Face ID or Touch ID | iCloud Keychain. Your Mac with the same Apple account gets it too. |
| Android phone (Android 9 or newer) | Chrome | Fingerprint, face or screen lock | Google Password Manager. Other devices with the same Google account get it too. |
| Mac | Safari or Chrome | Touch ID, or your Mac password | iCloud Keychain (Safari) or your Chrome profile |
| Windows laptop | Edge or Chrome | Windows Hello: face, fingerprint or PIN | Windows Hello on this laptop |

### Sign in

On the MEHO login page, click **Sign in with Passkey**. Or tap the user
name field: the browser offers your passkey. Confirm with your face,
your fingerprint or your PIN. You are signed in.

### Approve

1. Open the request on the approvals page.
2. When your sign-in is too old, the request shows **Sign in to approve**
   instead of **Approve**. Click it, then click **Sign in with Passkey**
   and confirm on the device.
3. Open the request again and click **Approve**.

On the command line, `meho approvals approve` opens the browser, and its
login page offers the passkey too. If you have a one-time code, that
login still asks for the code after the passkey.

### A lost or new device

- **Lost device:** sign in on another device. On the account page, under
  **Account security → Signing in**, delete the passkey of the lost
  device. Or ask an admin to delete it (in the Keycloak admin console:
  **Users → you → Credentials**).
- **New device:** add a passkey on it, as above.
- **New MEHO address:** a passkey works only for the address where you
  made it. Add a new passkey on the new address.
