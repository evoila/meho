---
type: lesson
applies_to: product-and-version
status: confirmed
product: Nimbus Print
version: "2.8"
summary: A print job is stuck and blocks the queue.
---

# Print job is stuck in the queue

## Symptom

One job shows the state "waiting" for a long time. All jobs behind it stay in the queue. The Nimbus Print server writes the error NP-210 in its log.

## Cause

The spooler cannot hand the first job to the printer, often because the file is very large or damaged. The spooler then stops the whole queue.

## Fix

1. Stop the service `nimbus-spool`.
2. Delete the files in the spool folder.
3. Start `nimbus-spool` again.
4. Ask the user to print the document once more.

## Prevent it

Set the maximum job size to 200 MB. Larger documents are refused with a clear message and no longer stop the queue.
