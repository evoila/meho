---
type: lesson
applies_to: product-and-version
status: confirmed
product: Harbor Backup
version: "5.3"
summary: Die nächtliche Sicherung scheitert an offenen Dateien.
---

# Sicherung scheitert an gesperrten Dateien

## Symptom

Der nächtliche Sicherungsjob endet mit dem Zustand „fehlgeschlagen“. Im Protokoll stehen der Code HB-0073 und eine Liste von Dateien, die geöffnet waren.

## Ursache

Programme halten nachts manche Dateien offen, zum Beispiel Postarchive und Datenbankdateien. Der Job kann eine offene Datei im normalen Modus nicht lesen.

## Lösung

1. Die Einstellungen des Sicherungsjobs öffnen.
2. Den Lesemodus von „direkt“ auf „Schnappschuss-Modus“ umstellen.
3. Den Ordner für temporäre Dateien in die Ausschlussliste aufnehmen.
4. Den Job einmal von Hand starten.

## Kontrolle

Am nächsten Morgen muss der Job „abgeschlossen“ zeigen. Einige übersprungene temporäre Dateien sind in Ordnung.
