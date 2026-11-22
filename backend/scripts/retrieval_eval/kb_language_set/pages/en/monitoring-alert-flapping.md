---
type: lesson
applies_to: product-and-version
status: confirmed
product: Beacon Monitor
version: "5.0"
summary: An alert switches between red and green all the time.
---

# Monitoring alert keeps flapping

## Symptom

The same check changes between "critical" and "ok" many times in one night. Each change sends a message. The event list shows the tag BM-FLAP.

## Cause

The value of the check lies close to the limit. Every small change crosses the limit and the state flips at once.

## Fix

1. Open the check in Beacon Monitor.
2. Set `recovery_count` to 3, so the check must be fine three times in a row.
3. Make the evaluation window five minutes long instead of one.
4. Save the check.

## Check

Watch the event list for one night. The check should change its state at most once.
