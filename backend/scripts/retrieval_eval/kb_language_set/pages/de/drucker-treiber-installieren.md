---
type: lesson
applies_to: product-and-version
status: confirmed
product: Nimbus Print
version: "2.8"
summary: Einen Drucker mit Treiber und Warteschlange einrichten.
---

# Neuen Drucker mit Treiber einrichten

## Vorbereitung

Man braucht den Treiber für das Druckermodell und die Adresse des Nimbus-Print-Servers. Beides gibt der Support heraus.

## Treiber installieren

1. Das Treiberpaket auf dem Server entpacken.
2. Im Verwaltungsprogramm „Treiber hinzufügen“ wählen.
3. Das Paket auswählen und bestätigen.

## Warteschlange anlegen

Für jeden Drucker legt man eine eigene Warteschlange an. Der Name soll den Standort enthalten, zum Beispiel „Etage 2 Farbe“. Danach wird der Spooler-Dienst nicht neu gestartet, die Warteschlange ist sofort bereit.

## Testseite

Eine Testseite drucken und prüfen, ob Ränder und Farben stimmen.
