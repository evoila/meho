---
type: lesson
applies_to: product-and-version
status: confirmed
product: Gatekeeper
version: "7.2"
summary: Den Zeitserver für Gatekeeper-Server einrichten.
---

# Zeitserver für Server einrichten

## Ziel

Alle Server der Umgebung sollen dieselbe Uhrzeit haben. Das ist die Grundlage für Anmeldung und Protokolle.

## Schritte

1. Auf dem Gatekeeper-Server die Einstellungen für den Zeitdienst öffnen.
2. Zwei Zeitserver als Quelle eintragen.
3. Die Abfrage alle 10 Minuten festlegen.
4. Den Zeitdienst neu starten.

## Hinweis zur Zeitzone

Server laufen in UTC. Die Zeitzone des Benutzers stellt die Anzeige ein, nicht der Server.

## Prüfen

Mit dem Verwaltungsprogramm die Abweichung der Server untereinander anzeigen. Sie soll unter einer Sekunde liegen.
