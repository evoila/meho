---
type: lesson
applies_to: product-and-version
status: confirmed
product: Beacon Monitor
version: "5.0"
summary: Create a new alert rule for a server check.
---

# Create a new alert rule

## Goal

A team wants a message when a server check goes over a limit.

## Steps

1. Choose the server and the check in Beacon Monitor.
2. Enter the limit for "warning" and the limit for "critical".
3. Choose the evaluation window, for example five minutes.
4. Choose who gets the message: a group, not a single person.

## Test

Press "send test message". The group must receive it within one minute.

## Note

A new rule starts in the state "unknown" until the first full window has passed.
