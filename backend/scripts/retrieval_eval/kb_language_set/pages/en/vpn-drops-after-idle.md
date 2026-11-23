---
type: lesson
applies_to: product-and-version
status: confirmed
product: Orbit Gate
version: "4.1"
summary: The VPN drops after a few idle minutes.
---

# VPN drops after idle time

## Symptom

The VPN connection of the Orbit Gate client ends after about 15 minutes without traffic. The client shows the code ORB-4012 and asks for a new sign-in.

## Cause

The gateway closes tunnels that carry no data. The default idle timeout is 900 seconds. A laptop that only sits in a meeting sends nothing in that time.

## Fix

1. Open the client profile of the user.
2. Set `KeepAliveSeconds` to 30.
3. Save the profile and reconnect.

## Check

Leave the laptop alone for 20 minutes. The tunnel must stay up. If it still ends, ask the network team to raise the gateway idle timeout for that user group.
