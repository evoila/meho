---
type: lesson
applies_to: product-and-version
status: confirmed
product: Gatekeeper
version: "7.2"
summary: Die Anmeldung scheitert, weil die Uhr falsch geht.
---

# Anmeldung scheitert wegen Zeitabweichung

## Symptom

Die Anmeldung an Gatekeeper wird abgelehnt, obwohl Benutzername und Kennwort stimmen. Die Meldung enthält den Code GK-401-SKEW.

## Ursache

Gatekeeper akzeptiert nur Anmeldungen von Geräten, deren Uhr höchstens fünf Minuten vom Server abweicht. Ein Gerät mit falscher Uhrzeit oder Zeitzone wird abgewiesen.

## Lösung

1. Die Uhrzeit und die Zeitzone am Gerät prüfen.
2. Auf dem Gerät `gkctl time-sync` ausführen.
3. Sich danach neu anmelden.

## Wenn es wiederkommt

Geht die Uhr nach jedem Neustart wieder falsch, ist oft die Pufferbatterie des Geräts leer. Dann das Gerät zur Reparatur geben.
