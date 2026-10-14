# Aktiediagram och portföljtransaktioner

## Purpose

Beskriver den befintliga lokala aktieappen: hämtning och jämförelse av aktiekurser, interaktivt diagram, köp och säljaffärer, portföljresultat samt import från mäklarens transaktionsexport.

## Requirements

### Requirement: Konfigurerbar aktielista

Appen SHALL läsa aktietickrar, visningsnamn och initial synlighet från `aktier.csv` i samma katalog som Python-programmet. Rader utan ticker SHALL ignoreras. Listan SHALL stödja högst 50 aktier. Ticker och visningsnamn SHALL vara unika. Initial synlighet SHALL kunna anges som `true`/`false`, `ja`/`nej` eller `1`/`0`. Ogiltig konfiguration SHALL ge ett begripligt fel.

#### Scenario: Läs aktier från konfiguration

- **WHEN** appen startar med en giltig `aktier.csv`
- **THEN** varje aktie med ticker blir tillgänglig i diagrammet och i transaktionsformuläret
- **AND** initial synlighet följer aktiens `show_initially`-värde

#### Scenario: Ogiltig eller för stor konfiguration

- **WHEN** konfigurationen innehåller dubbla tickrar, dubbla namn, ogiltigt synlighetsvärde eller fler än 50 aktier
- **THEN** appen avbryter med ett fel som pekar ut konfigurationsproblemet

### Requirement: Historiska kurser och indexerat jämförelsediagram

Appen SHALL hämta justerade dagskurser från Yahoo Finance med historik från 2010-01-01. Dagens kurs SHALL endast inkluderas efter respektive börsstängning och en kort publiceringsmarginal. Kurserna SHALL visas som separata kurvor i ett gemensamt diagram. Varje kurva SHALL indexeras till 100 vid den första tillgängliga kursen inom det synliga datumintervallet. Berkshire Hathaway B (`BRK-B`) SHALL räknas om från USD till SEK med hämtad USD/SEK-kurs. Aktier utan användbar kursdata SHALL hoppas över utan att stoppa övriga diagrammet.

#### Scenario: Starta diagrammet

- **WHEN** appen har hämtat historiska kurser för aktierna
- **THEN** diagrammet visar deras indexutveckling i samma koordinatsystem
- **AND** synligt startdatum är som standard 2020-01-01
- **AND** slutet sträcker sig minst till slutet av innevarande månad

#### Scenario: Ändra synligt datumintervall

- **WHEN** användaren flyttar datumreglaget eller zoomar diagrammet
- **THEN** varje aktie indexeras om till 100 vid sin första tillgängliga kurs inom det nya synliga intervallet
- **AND** köp- och säljmarkeringarnas höjd räknas om mot samma indexbas
- **AND** y-axeln anpassas efter de synliga kurvorna

#### Scenario: Ange synliga datum direkt

- **WHEN** användaren anger start- och slutdatum i ISO-formatet `ÅÅÅÅ-MM-DD`
- **THEN** diagrammets synliga datumintervall och nedre datumreglage uppdateras till dessa datum
- **AND** varje aktie indexeras om vid det nya synliga startdatumet
- **WHEN** användaren i stället flyttar handtagen i datumreglaget
- **THEN** start- och slutdatumfälten uppdateras så att de motsvarar det synliga intervallet
- **AND** ogiltiga datum, datum utanför tillgänglig historik eller startdatum efter slutdatum accepteras inte

#### Scenario: Historik saknas för en aktie

- **WHEN** en ticker inte ger användbara kursdata
- **THEN** appen meddelar i terminalen att den aktien hoppas över
- **AND** övriga tillgängliga aktier visas normalt

#### Scenario: Hämta nya slutkurser utan omstart

- **WHEN** användaren klickar på knappen för senaste slutkurser
- **THEN** appen hämtar Yahoo Finance-data senare än den senast inlästa dagen
- **AND** kurser inkluderas först efter respektive börsstängning och en kort publiceringsmarginal
- **AND** nya kurser läggs till i diagrammet, rangeslidern och resultatberäkningen utan att sidan eller Python-appen startas om
- **WHEN** ingen senare slutkurs finns
- **THEN** appen meddelar att ingen ny slutkurs är tillgänglig

#### Scenario: Hämta aktuella intradagskurser

- **WHEN** användaren klickar på `Hämta aktuell kurs`
- **THEN** appen hämtar senaste tillgängliga intradagskurs från Yahoo Finance för aktierna
- **AND** diagram, legend och vinst/förlust-tabellen uppdateras med kurserna utan omstart
- **AND** Berkshire-kursen räknas om till SEK

### Requirement: Kurvornas synlighet och legend

Diagrammet SHALL visa en extern aktielegend med en solo-checkbox och klickbart aktienamn för varje tillgänglig aktie. Klick på aktienamnet SHALL växla kurvan och dess transaktionsmarkeringar av eller på. Solo SHALL visa endast den valda aktien och dess transaktioner, och avmarkering SHALL återställa synligheten från före sololäget. Kurvnamn vid kurvornas slut SHALL vara förbundna med sina slutpunkter av tunna svarta ledarlinjer och placeras så att etiketterna inte överlappa. Hovring över ett externt aktienamn SHALL tillfälligt öka motsvarande kurvas tjocklek från 2 px till 5 px. Legendens vinst/förlustinformation SHALL följa respektive aktienamn.

#### Scenario: Växla aktie

- **WHEN** användaren klickar på en akties namn i den externa legenden
- **THEN** aktiens kurva och köp-/säljmarkeringar växlas tillsammans
- **AND** y-axeln räknas om efter synliga aktier

#### Scenario: Visa en aktie solo

- **WHEN** användaren markerar solo för en aktie
- **THEN** endast den aktiens kurva och transaktionsmarkeringar visas
- **WHEN** användaren avmarkerar solo
- **THEN** synligheten återställs till läget före solo-valet

#### Scenario: Markera kurva med hovring

- **WHEN** pekaren förs över ett aktienamn i den externa legenden
- **THEN** motsvarande kurva blir 5 px tjock
- **WHEN** pekaren lämnar aktienamnet
- **THEN** kurvan återgår till 2 px

### Requirement: Kursinformation och jämförelsemarkeringar

Hovring över diagrammet SHALL visa datum samt både aktiens kurs och dess indexvärde för aktier vars kurvor är synliga. Datum SHALL visas med år, månad och dag. Informationen SHALL begränsas till högst två rader per aktie. Ett diagramklick SHALL välja den närmaste synliga aktiekurvan inom rimligt visuellt avstånd och uppdatera aktie, datum och föreslagen kurs i transaktionsformuläret. Två klick på samma kurva SHALL skapa en bestående jämförelseetikett med aktienamn, procentuell kursförändring, procent per månad och förfluten tid i månader. Jämförelsen SHALL markeras med två horisontella linjer och en vertikal linje. Dubbelklick utanför kurvorna SHALL rensa jämförelser utan att ändra datumintervallet.

#### Scenario: Läs kurser vid hovring

- **WHEN** användaren hovrar över diagrammet
- **THEN** hovringsinformationen visar datum, aktiekurs och index för synliga aktier
- **AND** informationen per aktie ryms på högst två rader

#### Scenario: Jämför två punkter på en kurva

- **WHEN** användaren klickar på två punkter på samma kurva
- **THEN** diagrammet visar förändringen i procent, procent per månad och tidsintervallet i månader
- **AND** etiketten anger aktiens namn och jämförelsen ligger kvar tills den rensas

#### Scenario: Rensa jämförelser

- **WHEN** användaren dubbelklickar utanför kurvorna
- **THEN** samtliga jämförelseetiketter tas bort
- **AND** datumintervallet lämnas oförändrat

### Requirement: Registrera och spara köp och sälj

Appen SHALL erbjuda ett formulär för transaktionstyp (Köp/Sälj), aktie, datum, antal, kurs och courtage. Aktie och datum SHALL föreslå senast tillgänglig kurs på eller före valt datum; priset SHALL kunna redigeras och visas med två decimaler. Om användaren klickar på en kurva SHALL formuläret förifyllas med vald aktie, datum och kurs. Formuläret SHALL visa beräknat totalbelopp inklusive courtage för köp, respektive nettobelopp efter courtage för sälj. Giltiga transaktioner SHALL sparas i `aktiekop.csv` med semikolonseparerade kolumner `date`, `transaction_type`, `ticker`, `quantity`, `price`, `commission`; importerade transaktioner får även ha `source_id` och `amount_currency`. Kurs och courtage SHALL sparas till två decimaler. Ogiltiga värden eller okända aktier SHALL avvisas med ett begripligt fel.

#### Scenario: Föreslå kurs

- **WHEN** användaren väljer aktie och datum
- **THEN** kursfältet föreslår aktiens senast tillgängliga kurs på eller före datumet med två decimaler
- **AND** användaren kan ändra den föreslagna kursen

#### Scenario: Registrera köp

- **WHEN** användaren skickar in ett giltigt köp
- **THEN** köpet sparas i `aktiekop.csv`
- **AND** formuläret visar bekräftelse och köpmarkeringen läggs omedelbart till i diagrammet

#### Scenario: Registrera sälj

- **WHEN** användaren skickar in en giltig säljtransaktion
- **THEN** transaktionen sparas som `SÄLJ`
- **AND** den visas och påverkar portföljresultatet som en säljtransaktion

#### Scenario: Ogiltig transaktion

- **WHEN** antal eller kurs inte är större än noll, courtage är negativt, datumet saknar tidigare kursdata eller ticker saknar hämtad kursserie
- **THEN** transaktionen sparas inte och formuläret visar ett fel

### Requirement: Visa transaktioner i diagram och lista

Köp SHALL visas i huvuddiagrammet som små cirklar med mörk kontur. Sälj SHALL visas som nedåtriktade trianglar. Transaktionsmarkeringarna SHALL inte visas i det nedre översiktsdiagrammet/rangeslidern. Under diagrammet SHALL finnas en lista över registrerade transaktioner med datum, typ, aktie, antal, kurs, courtage och total- eller nettobelopp. Listan SHALL uppdateras direkt när en transaktion har sparats.

#### Scenario: Visa en registrerad transaktion

- **WHEN** en transaktion finns i `aktiekop.csv`
- **THEN** den visas i transaktionslistan och på motsvarande datum i huvuddiagrammet
- **AND** listans belopp visar antal multiplicerat med kurs, justerat med courtage
- **AND** markeringen förekommer inte i rangesliderns översiktskurva

### Requirement: Beräkna resultat per aktie

För aktier med registrerade köp SHALL den externa legenden visa uppskattad vinst eller förlust, månadsresultat i valuta och månadsavkastning i procent. Beräkningen SHALL utgå från aktuell senast tillgänglig kurs gånger kvarvarande antal, minus anskaffningskostnad inklusive courtage, med hänsyn till registrerade försäljningar. Resultatet SHALL uppdateras när en ny transaktion registreras. Historiskt antal Sirius XM (`SIRI`) SHALL räknas om till nuvarande aktiebas för transaktioner före 2024-09-10. Om månadsberäkning inte kan göras SHALL detta anges med `—/mån`.

#### Scenario: Beräkna resultat från flera affärer

- **WHEN** en aktie har flera köp och eventuella försäljningar
- **THEN** legendens resultat omfattar transaktionerna, courtage och aktuell position
- **AND** vinst/förlust, resultat per månad och procent per månad visas

#### Scenario: Uppdatera efter ny transaktion

- **WHEN** ett köp eller en försäljning sparas
- **THEN** vinst/förlustetiketten för den berörda aktien räknas om

### Requirement: Visa aktuell portföljställning bredvid transaktionslistan

Under diagrammet SHALL en sammanställning visas till höger om listan `Transaktioner`, med en rad per aktie som har registrerade transaktioner. Tabellen SHALL visa aktiens namn, datum för senast tillgängliga kurs, kvarvarande antal, senaste kurs, GAV (genomsnittligt anskaffningsvärde inklusive courtage), resultat i SEK och resultat i procent. Resultatet SHALL inkludera realiserad vinst/förlust från registrerade försäljningar och orealiserad vinst/förlust från kvarvarande aktier. GAV SHALL räknas med viktat genomsnittligt anskaffningsvärde, inklusive courtage. Utländska köp SHALL räknas om till SEK med valutakurs för transaktionsdagen eller närmast föregående tillgängliga valutadag; marknadsvärdet SHALL räknas om med senast tillgängliga valutakurs. Tabellen SHALL uppdateras efter en ny transaktion och efter uppdatering av marknadsdata, inklusive aktuella intradagskurser.

Registrerade utdelningar SHALL räknas som kontant avkastning i SEK-resultatet, utan att ändra aktieantal eller GAV. Eventuell källskatt SHALL minska utdelningsresultatet.

#### Scenario: Visa aktuell ställning från flera köp

- **WHEN** en aktie har en eller flera registrerade köp
- **THEN** sammanställningen visar senaste stängningsdatum och kurs, kvarvarande antal, GAV samt resultat i kronor och procent
- **AND** sammanställningen ligger till höger om transaktionslistan på breda skärmar
- **AND** resultatet räknar med courtage och växelkurs för utländska aktier

#### Scenario: Ta med försäljningar i ställningen

- **WHEN** aktien har registrerade köp och försäljningar
- **THEN** kvarvarande antal och anskaffningsvärde minskas med sålda aktier enligt viktat genomsnitt
- **AND** realiserat resultat från försäljningen läggs till orealiserat resultat

### Requirement: Konvertera mäklarens transaktionsexport

`konvertera_aktiekop.py` SHALL läsa den förväntade UTF-16-kodade tabbseparerade transaktionsexporten från Downloads, mappa kolumnen `Värdepapper` till appens ticker och omvandla köp, försäljningar och utdelningar till appens CSV-format. ISIN SHALL inte användas som ticker. Berkshire-kursen i källan SHALL omvandlas till SEK med exportens växlingskurs. Utdelningens kontantbelopp SHALL importeras som utdelning utan att påverka antal eller anskaffningsvärde; källskatt SHALL registreras som negativ utdelning. Exportens transaktions-ID SHALL sparas för idempotent återimport. Äldre importer utan transaktions-ID SHALL dedupliceras med datum, typ, ticker, antal, kurs och courtage. Programmet SHALL som standard endast visa en förhandsgranskning och SHALL bara skriva till `aktiekop.csv` med explicit `--apply`.

Diagrammets transaktionslista SHALL ha knappen `Importera aktiehändelser`. Den SHALL köra samma idempotenta import från exportfilen, spara nya rader och lägga till deras diagrammarkeringar och beräkningar i den öppna vyn utan omstart. Listans rubrik SHALL vara `Transaktioner`.

#### Scenario: Förhandsgranska import

- **WHEN** konverteringsverktyget körs utan `--apply`
- **THEN** det visar antal konverterbara transaktioner, dubbletter, nya rader och varningar
- **AND** målfilen ändras inte

#### Scenario: Tillämpa import

- **WHEN** konverteringsverktyget körs med `--apply`
- **THEN** nya transaktioner läggs till i `aktiekop.csv`
- **AND** redan importerade dubbletter hoppas över
- **AND** okända värdepapper eller ogiltiga rader rapporteras som varningar och hoppas över

### Requirement: Lokal körning

Diagrammet SHALL serveras lokalt via en loopback HTTP-server så transaktionsformuläret kan spara data via `/api/purchases`. Appen SHALL öppna diagrammet i webbläsaren och skapa en HTML-kopia `aktier_graf.html`. På breda fönster SHALL uppdateringsknappen och datumfälten ligga till höger om köpformuläret, ungefär centrerade ovanför diagrammet. Servern SHALL kunna stoppas med Ctrl+C. Layouten SHALL anpassa diagram, legend och transaktionskontroller för smalare fönster.

#### Scenario: Starta och stoppa appen

- **WHEN** användaren startar Python-programmet och kursdata finns
- **THEN** ett interaktivt diagram öppnas från en lokal server
- **AND** en statisk HTML-kopia skrivs bredvid programmet
- **WHEN** användaren trycker Ctrl+C i terminalen
- **THEN** den lokala servern stoppas
