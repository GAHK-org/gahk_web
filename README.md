# gahk_web
[![CI](https://github.com/GAHK-org/gahk_web/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/GAHK-org/gahk_web/actions/workflows/ci.yml)

Den offentlige hjemmeside **gahk.dk** for G. A. Hagemanns Kollegium.

Alt indhold – sider, menu, nyheder, begivenheder og billeder – redigeres på
[intern.gahk.dk](https://intern.gahk.dk) under *Hjemmeside* (repoet
[gahk_intern](https://github.com/GAHK-org/gahk_intern)). Ved hver ændring sender intern et signeret
øjebliksbillede af indholdet hertil, og gahk_web gør det til færdige, statiske HTML-filer, som Apache
serverer direkte. Derfor kører gahk.dk videre, selv om intern.gahk.dk eller S3 er nede.

Python kører kun for det, statiske filer ikke kan klare: synkroniserings-API'et (`/api/*`, kun for
intern), optagelsesformularerne, besøgstælleren, kalender-feeds og omdirigeringer af gamle adresser.
Ansøgninger og besøgstal ligger kun på webhotellet, indtil intern henter dem (hvert andet minut).
Webhotellet gemmer altså intet permanent, og det har hverken adgang til interns database eller til S3.

Siden kører på Hetzner Webhosting (Apache og systemets Python 3.13). Python-delen er en Flask-app, som
kører i en vedvarende FastCGI-proces (`cgi-bin/gahk.fcgi`); kun opdateringer og store uploads går
gennem CGI (`cgi-bin/gahk.cgi`), som også er reserveløsningen, hvis FastCGI svigter. Webhotellet opdaterer
sig selv: når intern beder om det (hvert kvarter, eller med knappen *Opdatér gahk.dk nu*), henter det
den nyeste bundle, som CI har lagt på GitHub-releasen `production`, og installerer dens
Python-pakker fra PyPI med pip. Der bruges ikke noget cron-job. Opsætning og drift er beskrevet i [DEPLOY.md](DEPLOY.md), kontrakten
med intern i [docs/protocol.md](docs/protocol.md) og opførslen i [spec/features/](spec/features/).

## Kom i gang (udvikling)

```sh
task install     # opret virtualenv + installer afhængigheder
task dev         # udviklingsserver → http://127.0.0.1:8810
```

`task dev` skriver første gang `local/config.ini` med nye hemmeligheder, opretter databaserne og
kører migreringerne. Den bruger som standard gahk_interns Postgres-container (`localhost:5432`,
bruger og kodeord `gahk`); brug en anden med `task dev PG=postgres://bruger:kode@vært:port`.
Udviklingsserveren er Werkzeugs med genstart, når en `.py`-fil ændres. Den efterligner `.htaccess`
(også hvilke forespørgsler der går til FastCGI-processen, og hvilke til CGI) og skriver siderne til
`var/public_html`.

Indtil der er publiceret indhold, svarer siden "Siden opdateres" (503). Indhold kommer fra en lokal
intern: sæt `GAHK_WEB_URL` og `GAHK_WEB_KEY` i gahk_interns `app/.env` til de værdier, `task config`
skrev ud (nøglen står også i `local/config.ini`), og gem en side under *Hjemmeside*.

## Test

```sh
task db:create   # opret test-databasen (én gang)
task test        # kør testsuiten mod Postgres, som CI
task bundle      # byg release-bundlen i dist/ og røgtest den på Python 3.13 og 3.14 (henter pakker fra PyPI)
task webhost     # byg bundlen, og installér og afprøv den på et falsk webhotel i docker
```

Testene genskaber skemaet i test-databasen (`gahk_web_test`) ved hver kørsel.
`task bundle -- --database postgres://…/en_smid_vaek_database` prøver også den første installation
gennem CGI-scriptet og forespørgsler til FastCGI-scriptet, ligesom CI gør. Den sletter alle tabeller i databasen, så brug kun en til formålet.

`task webhost` kræver docker og er CI's `webhost`-job. Den bygger et image (`scripts/fake_webhost/`),
der ligner Hetzners webhotel så meget, vi kan: Debian trixie med Apache 2.4, mod_rewrite, mod_fcgid,
CGI og suEXEC og systemets python3 (3.13.5 med Debians pip 25.1.1 og glibc 2.41, som på webhotellet).
Den starter det ved siden af en PostgreSQL 17 og spiller selv operatør, GitHub, intern og besøgende:
upload af de fire bootstrap-filer, første installation med pip fra PyPI, publicering med billeder,
statiske sider og deres cache-headere, formularer, besøgstæller, omdirigeringer, adresser der skal
afvises, en ny release, CGI-only-kontakten og ødelagte releases, som ikke må komme i drift. Det tager
omkring et minut plus byggetiden af imaget. Containerne hedder `gahkweb-e2e-*` og fjernes bagefter.

```sh
task webhost -- --keep                 # lad containerne køre bagefter, så man kan kigge ind
task webhost -- --wheelhouse ~/wheels  # tag pakkerne fra en mappe med wheels i stedet for PyPI
task webhost -- --no-suexec            # det andet gæt: Apache selv kører som kontoen, uden suEXEC
task webhost -- --platform linux/amd64 # webhotellets arkitektur (langsommere på en Mac med ARM)
```

Hvad kørslen viser, og hvad den ikke kan vise om det rigtige webhotel, står i
[DEPLOY.md](DEPLOY.md) afsnit 13.

## Code Quality: lint + typer + pre-commit

```sh
task lint         # ruff check + format-tjek
task fix          # ret det, ruff kan rette, og formatér koden
task typecheck    # mypy (strict) – samme som CI's typecheck-job
task hooks        # installér prek git pre-commit hooks (kør én gang)
```

`task hooks` installerer [prek](https://github.com/j178/prek) (en hurtig, pre-commit-kompatibel
runner) via `.pre-commit-config.yaml`. Derefter kører **ruff** (check + format) og **mypy** automatisk
ved hvert commit. Kør manuelt på alt med `uv run prek run --all-files`. De samme tjek håndhæves i CI
(`.github/workflows/ci.yml`): `lint`, `typecheck`, `test` (Python 3.13 med PostgreSQL 15 og 3.14 med
17), `bundle`, `webhost` og `security`. Et merge til `main` udgiver derefter en ny bundle (`release`).

Bundlen indeholder ikke selve afhængighederne, kun `requirements.lock` (eksporteret fra `uv.lock`,
hver fil låst med sin sha256). Webhotellets updater installerer dem med pip for sin egen Python, når
den installerer en release. Derfor skal hver afhængighed have færdigbyggede wheels til CPython 3.13
og 3.14 på Linux (manylinux, x86_64); `bundle`-jobbet installerer dem præcis som webhotellet og
fejler, hvis en mangler eller ikke kan indlæses.

## Branches og pull requests

Ingen kan pushe direkte til `main` eller `dev`; alt går gennem pull requests. `dev` samler det
næste release, og `main` er det, der kører i produktion: hvert merge til `main` bliver til en ny
bundle, som gahk.dk installerer inden for et kvarter.

| Branch | Laves fra | PR ind i | Merge med |
|--------|-----------|----------|-----------|
| `feature/…`, `fix/…` (og Copilots `copilot/…`) | `dev` | `dev` | **Squash and merge** |
| `dev` (release) | – | `main` | **Create a merge commit** |
| `hotfix/…` | `main` | `main` | **Squash and merge** |
| `automation/sync-main-…` (laves automatisk) | – | `dev` | **Create a merge commit** – aldrig squash |
| Dependabot | – | `dev`; sikkerhedsopdateringer: `main` | **Squash and merge** (patch-opdateringer merges automatisk) |

**Release:** Åbn en PR fra `dev` til `main`, og merge med "Create a merge commit". Squash eller rebase
aldrig en release: så deler `main` og `dev` ikke længere historik, og hver release giver de samme
konflikter igen.

**Hotfix** – en rettelse, der ikke kan vente på næste release:

1. Lav branchen fra `main`, ikke `dev`: `git fetch && git switch -c hotfix/kort-navn origin/main`.
2. Åbn en PR ind i `main`, og squash-merge den.
3. Workflowet *Sync main to dev* laver en branch med `main` merget ind i `dev`. Åbn PR'en via linket i
   kørslens opsummering (Actions-fanen), og merge med "Create a merge commit".

Indtil sync-PR'en er merget, er PR'er ind i `dev` blokeret – så rettelsen ikke går tabt i næste
release. Det samme gælder, når Dependabot har merget en sikkerhedsopdatering ind i `main`.

**Tjekkene `branch-flow` og `in-sync`** (`.github/workflows/branch-policy.yml`) håndhæver reglerne:

- `branch-flow` fejler: branchen må ikke merges derind (tjek navnet og målet), eller en hotfix
  indeholder upubliceret arbejde fra `dev` – lav den forfra fra `main`.
- `in-sync`: *"main has changes dev lacks"* – merge sync-PR'en ind i `dev` først.
- `in-sync`: *"main has moved on"* – klik "Update branch" (på Dependabots PR'er: kommentér
  `@dependabot rebase`).

**Ændringer i kontrakten med intern** (API'et og øjebliksbilledet, [docs/protocol.md](docs/protocol.md))
skal laves i begge repoer. Gør dem bagudkompatible, så den ene side kan udgives før den anden.

## Licens

Koden er udgivet under GNU Affero General Public License v3.0 eller senere (AGPL-3.0-or-later) – se [LICENSE](LICENSE).
