---
type: lesson
applies_to: product-and-version
status: confirmed
product: Harbor Backup
version: "5.3"
summary: Einen Zeitplan für einen Sicherungsjob anlegen.
---

# Zeitplan für einen Sicherungsjob anlegen

## Ziel

Ein Sicherungsjob soll jede Nacht laufen, ohne dass jemand ihn startet.

## Schritte

1. Im Harbor-Backup-Verwaltungsprogramm einen neuen Job anlegen.
2. Die Ordner wählen, die gesichert werden sollen.
3. Unter „Zeitplan“ die Startzeit eintragen, zum Beispiel 01:30 Uhr.
4. Den Job speichern und aktivieren.

## Aufbewahrung

Jede Sicherung wird 30 Tage aufbewahrt. Ältere Sicherungen löscht Harbor Backup selbst.

## Hinweis

Zwei Jobs sollten nicht zur gleichen Zeit starten, weil sie sich sonst die Leitung teilen und länger dauern.
