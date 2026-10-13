# Aktiediagram

Lokal Python-app för att jämföra aktier, visa köp och sälj i diagrammet och följa portföljresultat. Funktionaliteten och beteendekraven finns dokumenterade i [OpenSpec](openspec/specs/aktiediagram/spec.md).

## Krav

- Python 3.10 eller senare
- Internetanslutning för att hämta kursdata från Yahoo Finance

Installera beroenden:

```sh
python3 -m pip install -r requirements.txt
```

## Kör appen

```sh
python3 aktier_graf.py
```

Appen läser aktier och initial synlighet från `aktier.csv`, hämtar historik från 2010 och öppnar diagrammet i webbläsaren. Dagskurser används först efter respektive börsstängning och en kort publiceringsmarginal, så ofullständiga kurser under handelsdagen inte används. Knappen **Hämta senaste slutkurser** hämtar senare tillgängliga dagskurser från Yahoo Finance utan att starta om appen och uppdaterar kurvor och resultat. Den lokala servern stoppas med Ctrl+C i terminalen. `aktier_graf.html` skapas som en statisk kopia; formuläret som sparar transaktioner och knappen för datauppdatering använder den lokala servern.

## Köp och sälj

Registrerade transaktioner sparas i `aktiekop.csv` i projektmappen. Filen ignoreras av Git eftersom den innehåller privata ekonomiska uppgifter. Den skapas när den första transaktionen registreras.

## Importera mäklartransaktioner

`konvertera_aktiekop.py` visar en förhandsgranskning utan att ändra köpfilen:

```sh
python3 konvertera_aktiekop.py
```

För att lägga till nya transaktioner efter granskning:

```sh
python3 konvertera_aktiekop.py --apply
```

Verktyget förväntar sig exportfilen `transactions-and-notes-export (1).csv` i användarens Downloads-mapp.

## Konfiguration

Redigera `aktier.csv` för att ändra aktier, visningsnamn och vilka kurvor som syns från början. Appen stöder upp till 50 aktier.
