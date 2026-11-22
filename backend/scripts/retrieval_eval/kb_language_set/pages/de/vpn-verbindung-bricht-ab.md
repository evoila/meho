---
type: lesson
applies_to: product-and-version
status: confirmed
product: Orbit Gate
version: "4.1"
summary: Die VPN-Verbindung bricht im Leerlauf ab.
---

# VPN-Verbindung bricht im Leerlauf ab

## Symptom

Die VPN-Verbindung des Orbit-Gate-Clients endet nach etwa 15 Minuten ohne Datenverkehr. Der Client zeigt den Code ORB-4012 und verlangt eine neue Anmeldung.

## Ursache

Das Gateway schließt Tunnel, in denen keine Daten fließen. Die Standardzeit für den Leerlauf beträgt 900 Sekunden. Ein Laptop, der nur in einer Besprechung liegt, sendet in dieser Zeit nichts.

## Lösung

1. Das Client-Profil des Benutzers öffnen.
2. Den Wert `KeepAliveSeconds` auf 30 setzen.
3. Das Profil speichern und die Verbindung neu aufbauen.

## Kontrolle

Den Laptop 20 Minuten unberührt lassen. Der Tunnel muss bestehen bleiben. Bricht er trotzdem ab, bittet man das Netzwerkteam, die Leerlaufzeit am Gateway für diese Benutzergruppe zu erhöhen.
