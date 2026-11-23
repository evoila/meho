---
type: lesson
applies_to: product-and-version
status: confirmed
product: Nimbus Mail
version: "6.0"
summary: A full mailbox rejects new mail.
---

# Mailbox is full

## Symptom

The user gets no new mail. Senders receive a bounce message with the code MBX-552 and the text "quota exceeded".

## Cause

The mailbox reached its size limit. The standard limit is 10 GB. Attachments in the folders "Sent" and "Deleted items" are the usual cause.

## Fix

1. Empty "Deleted items" and the folder "Recoverable items".
2. Move mail older than two years into the personal archive.
3. If the user still needs more space, the team lead can ask for a limit of 20 GB.

## Check

Open the mailbox size in the account view. It should be below 90 percent. Ask a colleague to send a test mail.
