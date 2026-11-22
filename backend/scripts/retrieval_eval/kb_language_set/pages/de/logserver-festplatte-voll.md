---
type: lesson
applies_to: product-and-version
status: confirmed
product: Quill Logs
version: "3.0"
summary: Die Platte des Logservers läuft voll.
---

# Logserver: Festplatte ist voll

## Symptom

Auf dem Server von Quill Logs sind weniger als 5 Prozent der Platte frei. Neue Einträge werden abgelehnt. Die Überwachung meldet QL-DISK-90.

## Ursache

Die Aufbewahrung ist auf 30 Tage eingestellt, aber die Menge der Einträge ist seit dem letzten Release stark gestiegen. Die alten Dateien werden nicht schnell genug entfernt.

## Lösung

1. Die Aufbewahrung von 30 auf 14 Tage senken.
2. Die Rotation jeden Tag statt jede Woche laufen lassen.
3. Die Kompression der rotierten Dateien einschalten.
4. Archive, die man behalten muss, auf den Archivspeicher verschieben.

## Danach

Nach einem Tag den freien Platz prüfen. Er sollte über 30 Prozent liegen.
