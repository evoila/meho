---
type: lesson
applies_to: product-and-version
status: confirmed
product: Atlas DNS
version: "3.3"
summary: After a change, some clients still get the old address.
---

# Old address after a DNS change

## Symptom

A website moved to a new server. Some users reach the new one, others still reach the old one for hours.

## Cause

Resolvers keep an answer until its TTL ends. The record had a TTL of 24 hours, so the old answer lives that long. The event shows the tag AD-TTL.

## Fix

1. Run `atlasdns flush` on the company resolvers.
2. Ask users to restart their browser. Their own computer keeps a cache too.
3. Keep the old server running until all users have moved.

## Next time

Lower the TTL to 300 seconds two days before a move. Raise it again afterwards.
