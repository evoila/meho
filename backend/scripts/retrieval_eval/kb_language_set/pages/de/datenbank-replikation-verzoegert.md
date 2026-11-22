---
type: lesson
applies_to: product-and-version
status: confirmed
product: Tarn DB
version: "9.1"
summary: Das Replikat hinkt dem Hauptsystem hinterher.
---

# Datenbank-Replikation hinkt hinterher

## Symptom

Auswertungen auf dem Replikat zeigen veraltete Zahlen. Die Verzögerung beträgt mehr als 30 Minuten. Die Überwachung meldet TDB-LAG-30.

## Ursache

Eine lange Abfrage auf dem Replikat hält Änderungen zurück, bis sie fertig ist. Seltener ist die Netzverbindung zwischen den Servern zu langsam.

## Lösung

1. Mit `tarnctl lag` die aktuelle Verzögerung ansehen.
2. Die laufenden Abfragen auf dem Replikat auflisten.
3. Die längste Abfrage beenden, wenn der Besitzer zustimmt.
4. Danach den Wert nach fünf Minuten erneut lesen.

## Vorbeugen

Lange Auswertungen auf ein eigenes Replikat legen und eine Höchstdauer von 10 Minuten für Abfragen festlegen.
