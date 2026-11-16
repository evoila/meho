# Approval notifications go through web push (decision)

**Status:** proposed in Initiative #5998; approved by the project owner
with the merge of its pull request
**Date:** 2026-11-15
**Task:** [#6003](https://github.com/evoila/meho/issues/6003)
**Initiative:** [#5998](https://github.com/evoila/meho/issues/5998) (MEHO on your phone and laptop)

## The decision in short

- **The channel.** MEHO tells a person that an approval waits with a web
  push message. It goes from MEHO through the push service of the
  browser maker (Apple, Google, Microsoft or Mozilla) to the person's
  phone or laptop. The installed MEHO app, or the browser, shows it.
- **What the message carries.** Only: the title ("An approval waits", or
  the result such as "Approval approved"), the tenant name, the request
  id, and the path of the approvals page. Never the operation, its
  parameters, the target or a person's name. The message is encrypted
  for the browser (RFC 8291), so the push service cannot read it. Only
  the request id is also sent readable, in the `Topic` header (see
  "What leaves the network").
- **Off by default.** An admin turns the feature on for the whole MEHO
  (`WEB_PUSH_ENABLED`, chart `webPush.enabled`). Then each person turns
  it on for each browser on the account page. While the setting is off,
  nothing changes for anyone and no push key is used.
- **A notification only points.** It opens the approvals page. Approving
  needs the same sign-in and the same click as before. Who may approve
  does not change.

## Why

People who approve are often away from their desk. Without a
notification, a request waits until someone happens to open the
approvals page. The approval then waits for minutes or hours, and the
agent or person who asked waits too.

Web push is the browser's own notification channel. It needs:

- no app store app and no native code: the web console stays
  server-built, as before;
- no new service that we run: the browser makers run the push
  services;
- no VPN for the message itself: it reaches a phone that is not on the
  VPN. Opening the approvals page still needs the VPN.

## What leaves the network

This is the first MEHO feature that sends data to a service the customer
did not set up. So we keep it small and say exactly what it is:

- **Where to:** only the push services of the browser makers. MEHO
  refuses any other push address (`web_push/endpoints.py`). A firewall
  team allows these hosts on HTTPS (TCP 443): `*.push.apple.com`,
  `fcm.googleapis.com`, `*.notify.windows.com` and
  `updates.push.services.mozilla.com`.
- **What:** one HTTPS request for each notification. Encrypted inside:
  the title, the tenant name, the request id and the approvals page
  path. Readable by the push service: the push address of the browser
  (made by the browser maker), the signed key token (which names the
  MEHO address when it is an `https://` address), the request id in the
  `Topic` header, and the size and time of the message. The `Topic`
  header lets the push service replace an undelivered "An approval
  waits" with its result. So the push service can tell which messages
  belong to the same request. The request id is a random number and
  says nothing about the request's content. A summary message has the
  fixed word `approvalsummary` there.
- **Not:** the request content, any secret, any target detail, any
  audit data.

The pages a person opens after a tap come from MEHO over the VPN, as
before.

## Other options we did not choose

- **E-mail or chat messages.** They work today through check
  notifications, but they reach a mail box or a channel, not one person's
  device. A chat channel also shows the message to everyone in it.
- **A native phone app.** It needs app store releases for iPhone and
  Android and a second code base. The installed web app gives the same
  result for approvals and broadcast.
- **Our own push relay.** It would mean running an internet service for
  every customer. The browser makers' push services already do this.

## Limits we accept

- iPhone and iPad show web push only in the installed app (iOS 16.4 and
  newer).
- An air-gapped install keeps the feature off: the messages need the
  internet.
- The rate limit (one "An approval waits" per person and tenant every 30
  seconds, then one summary) and the order of messages are kept in the
  memory of each MEHO copy.

## Where it is described

- User guide: `docs-site/guides/phone-and-laptop.md`.
- Firewall hosts: `docs-site/install/index.md`, "Push services for
  approval notifications".
- What leaves the network: `docs-site/deployment-shapes.md`, "What leaves
  your network".
- Code: `docs/codebase/ui.md` ("Approval notifications") and
  `docs/codebase/approvals.md` ("Notifications").
