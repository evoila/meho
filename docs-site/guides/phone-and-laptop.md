# MEHO on your phone and laptop

This page explains how to use MEHO on a phone or a laptop. Today it has
one part: how you sign in and approve with a passkey. The steps to
install MEHO as an app and to turn on notifications come with that
feature.

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

The MEHO app on an iPhone home screen needs iOS 16.4 or newer.

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
