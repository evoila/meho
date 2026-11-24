---
type: lesson
applies_to: product-and-version
status: confirmed
product: Nimbus Print
version: "2.8"
summary: Ein Druckauftrag blockiert die Warteschlange.
---

# Druckauftrag hängt in der Warteschlange

## Symptom

Ein Auftrag steht lange im Zustand „Wartet“. Alle Aufträge dahinter bleiben in der Warteschlange. Der Nimbus-Print-Server schreibt den Fehler NP-210 ins Protokoll.

## Ursache

Der Spooler kann den ersten Auftrag nicht an den Drucker übergeben, oft weil die Datei sehr groß oder beschädigt ist. Dann hält der Spooler die ganze Warteschlange an.

## Lösung

1. Den Dienst `nimbus-spool` beenden.
2. Die Dateien im Spool-Ordner löschen.
3. Den Dienst `nimbus-spool` wieder starten.
4. Den Benutzer bitten, das Dokument noch einmal zu drucken.

## Vorbeugen

Die maximale Auftragsgröße auf 200 MB setzen. Größere Dokumente werden mit einer klaren Meldung abgelehnt und halten die Warteschlange nicht mehr an.
