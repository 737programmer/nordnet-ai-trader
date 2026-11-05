# Nordnet AI Swing Trader – iPhone webapp / cloud v1

En **read-only PWA** med daglig global aktiescanning. Dette er en **regelbaseret** prototype, ikke en AI-nyhedsanalyse og ikke en Nordnet-integration. Ingen handler placeres. Bygger videre på scannerlogikken fra desktopversionen.

## Kom i gang: GitHub Pages (kræver GitHub-konto)

1. Opret et nyt GitHub-repository. **Vigtigt:** Et offentligt repo og en offentlig GitHub Pages-side betyder, at både koden og signalerne kan ses af andre. Læg **aldrig** beholdninger, adgangskoder, API-nøgler eller andre personlige data i repoet. Brug et neutralt navn; dette projekt indeholder ingen personlige beholdninger.
2. Upload **indholdet af denne mappe** til roden af repositoryet, inklusive `.github/workflows/daily.yml` (skjulte mapper skal med).
3. Åbn **Settings → Pages → Build and deployment → Source: GitHub Actions**. Gem.
4. Gå til **Actions → Daily scan and deploy → Run workflow**. Første kørsel kan tage nogle minutter. Hvis GitHub Actions er slået fra, aktivér dem i repositoryindstillingerne.
5. Find adressen under **Settings → Pages** eller i deployment-resultatet. Den vil typisk være `https://BRUGERNAVN.github.io/REPO/` (erstat pladsholdere).
6. Åbn adressen i **Safari på iPhone → Del → Føj til hjemmeskærm**. Nu kan appen åbnes som en app.

**Automatik:** GitHub Actions forsøger scanning **mandag–fredag kl. 23:20 UTC** (typisk 00:20/01:20 dansk tid afhængigt af sommertid). GitHubs planlagte jobs kan blive forsinket eller springes over; tjek tidsstemplet i appen. Asiens børser er typisk ikke lukket for den kommende handelsdag på dette tidspunkt. Markedsdata kan være forsinkede, og weekends/helligdage giver ældre kursdatoer.

**Bemærk:** GitHub Pages til private repositories afhænger af plan og organisation. Den enkleste opsætning ovenfor er et offentligt repo **uden personlige oplysninger**. Hvis du ønsker private beholdninger eller private dashboards, bør vi i stedet opsætte en adgangsbeskyttet hostingløsning.

## Lokal test

Kræver Python 3.11+ og internet:

```bash
python -m venv .venv
# Windows: .venv\Scripts\python -m pip install -r requirements.txt
# macOS/Linux: .venv/bin/pip install -r requirements.txt
python build.py
python -m http.server 8000 --directory site
```

Åbn `http://localhost:8000`. Appen viser ingen data, før `python build.py` har gennemført en scanning. For at teste brugerfladen uden data kan du lave en **lokal** `site/signals.json` med testdata; offentliggør ikke fiktive data som ægte signaler.

## Konfiguration

- `config.yaml`: overvågede tickere (30 illustrative instrumenter fra flere markeder), ATR-multiplikator og parametre. Ikke en fuldstændig global markedsscanner.
- `bot.py`: download, indikatorer og backtest for enkeltaktier.
- `build.py`: skriver `site/signals.json` og afbryder deployment, hvis alle datakilder fejler.
- `site/`: statisk iPhone-app. `signals.json` genereres i workflowet.
- `.github/workflows/daily.yml`: daglig cloud-kørsel og udgivelse.

## Risiko og begrænsninger

- **Ingen automatisk handel, ingen Nordnet-API, ingen login, ingen beholdninger i skyen.**
- Koden er en regelbaseret scanner. AI-baseret nyhedsanalyse er **ikke** inkluderet i denne version.
- Enkeltaktie-backtest i `bot.py` er eksperimentel og ikke en samlet porteføljebacktest. Ingen dokumenteret merperformance.
- Tickerlisten er en statisk, lille watchlist; ikke global universdækning. Yahoo Finance/yfinance er en uofficiel datakilde uden garanti for rettidighed, tilgængelighed eller licens til kommerciel brug.
- Kurs og stop-reference er i lokal instrumentvaluta; appen laver ingen DKK-konvertering, position sizing eller håndtering af Nordnet-kurtage, skat eller FX.
- Signalberegning bruger seneste tilgængelige lukkekurs, ikke livekurser. ATR-stop-reference er **ikke** en stopordre.
- Tjek altid markedets åbningstid, ticker, nyheder, spread, pris og valuta manuelt før en handel.
- GitHub Actions' tidsplan er ikke en garanti for rettidig udførelse; kontrollér datostempler.

## Udvidelser

Næste trin: adgangsbeskyttet dashboard, portefølje-CSV privat på en sikker server, valuta- og positionsrisiko, robust porteføljebacktest og valgfri AI-analyse med kildehenvisninger. Brug aldrig Nordnet-adgangskoder i kildekode eller GitHub-repo.
