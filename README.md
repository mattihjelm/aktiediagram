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

Appen läser aktier och initial synlighet från `aktier.csv`, hämtar historik från 2010 och öppnar diagrammet i webbläsaren. Titelraden visar tidsstämpeln för den senast ändrade filen i appkatalogen, exklusive Git-metadata, Python-cache och genererad HTML. Dagskurser används först efter respektive börsstängning och en kort publiceringsmarginal. Knappen **Hämta slutkurser** hämtar senare tillgängliga dagskurser från Yahoo Finance. **Hämta aktuell kurs** hämtar senaste tillgängliga intradagskurs och uppdaterar diagrammet, legenden och resultattabellen. Den lokala servern stoppas med Ctrl+C i terminalen. `aktier_graf.html` skapas som en statisk kopia; formuläret och knapparna för datauppdatering använder den lokala servern.

Start- och slutdatum för det synliga diagramintervallet kan anges direkt i ISO-format (`ÅÅÅÅ-MM-DD`) eller ändras med handtagen i diagrammets nedre datumreglage. Fälten och reglaget hålls synkroniserade.

## Köp och sälj

Registrerade transaktioner sparas i `aktiekop.csv` i projektmappen. Filen ignoreras av Git eftersom den innehåller privata ekonomiska uppgifter. Den skapas när den första transaktionen registreras.

## Importera mäklartransaktioner

`konvertera_aktiekop.py` visar en förhandsgranskning utan att ändra transaktionsfilen. Verktyget importerar köp, försäljningar, utdelningar och källskatt:

```sh
python3 konvertera_aktiekop.py
```

För att lägga till nya transaktioner efter granskning:

```sh
python3 konvertera_aktiekop.py --apply
```

Standardfilen är `transactions-and-notes-export.csv` i användarens Downloads-mapp. I appen går det även att välja en annan exportfil bredvid knappen **Importera aktiehändelser**. Exportens transaktions-ID sparas så att samma export kan importeras igen utan dubbletter. Äldre importer utan ID jämförs med transaktionsdetaljerna.

När diagrammet är öppet kan knappen **Importera aktiehändelser** använda standardfilen eller den valda filen och lägga till nya köp, försäljningar och utdelningar i den aktuella vyn.

## Konfiguration

Redigera `aktier.csv` för att ändra aktier, visningsnamn och vilka kurvor som syns från början. Appen stöder upp till 50 aktier.
