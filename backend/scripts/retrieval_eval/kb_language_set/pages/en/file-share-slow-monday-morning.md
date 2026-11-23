---
type: lesson
applies_to: product-and-version
status: confirmed
product: Stack Share
version: "4.6"
summary: The file share is slow at the start of the week.
---

# File share is slow on Monday mornings

## Symptom

Folders on the shared drive open very slowly from Monday 08:00 until about noon. The rest of the week it is fast. The log of the file server holds SS-SCAN-08.

## Cause

The antivirus scanner on the Stack Share file server runs a full scan every Monday at 08:00. It reads every file and uses most of the disk.

## Fix

1. Open the scan schedule of the scanner.
2. Move the full scan to Sunday at 22:00.
3. Keep a quick scan of new files during the week.

## Check

On the next Monday, open a large folder at 09:00. It should open in a few seconds.
