---
type: lesson
applies_to: product-and-version
status: confirmed
product: Wavepoint
version: "8.0"
summary: Video calls freeze when walking between rooms.
---

# Wi-Fi drops between meeting rooms

## Symptom

Laptops freeze for 5 to 10 seconds when the user walks to another room. A video call stops during that time. The access point log holds WP-ROAM-11.

## Cause

The laptop holds on to the first access point although a closer one is available. The new connection starts only when the old signal is gone.

## Fix

1. Switch on fast roaming in the Wavepoint controller.
2. Raise the minimum signal level to -70 dBm, so weak links are dropped sooner.
3. Use the same network name on all access points of the floor.

## Check

Walk through three rooms with a video call open. The picture may stop for at most one second.
