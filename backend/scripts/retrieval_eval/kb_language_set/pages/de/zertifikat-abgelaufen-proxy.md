---
type: lesson
applies_to: product-and-version
status: confirmed
product: Relay Proxy
version: "2.4"
summary: Das Zertifikat des Proxys ist abgelaufen.
---

# Zertifikat am Reverse-Proxy abgelaufen

## Symptom

Browser zeigen beim Aufruf der Webseite eine rote Warnseite. Im Protokoll des Relay Proxys steht RP-CERT-EXP.

## Ursache

Das Zertifikat hat sein Ablaufdatum überschritten. Die Erinnerung per E-Mail ging an eine Adresse, die es nicht mehr gibt.

## Lösung

1. Ein neues Zertifikat besorgen.
2. Das Zertifikat und die ganze Kette in den Ordner des Proxys legen.
3. `relayctl cert reload` ausführen. Ein Neustart ist nicht nötig.
4. Die Webseite in einem privaten Browserfenster öffnen und das Ablaufdatum ansehen.

## Vorbeugen

Die Erinnerung an eine Gruppenadresse schicken, nicht an eine einzelne Person.
