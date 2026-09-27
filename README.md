# Checker voli Volotea da Firenze

Cerca i voli Volotea diretti da Firenze (FLR) verso le destinazioni in `config.toml`
per i prossimi 365 giorni, combina andata e ritorno con soggiorni di 5-7 giorni
e invia per email le combinazioni più economiche (con tutte le combinazioni in allegato CSV e HTML),
più il miglior prezzo per ogni destinazione.

## Da dove arrivano i prezzi

Il sito Volotea è protetto da sistemi anti-bot (Imperva/Akamai): uno script Python
semplice viene bloccato. Per questo il programma ha tre "provider", scelti con
`provider` in `config.toml`:

| provider | come funziona | costo |
|----------|---------------|-------|
| `google` (predefinito) | Google Flights filtrato su Volotea (codice V7), solo voli diretti, tramite la libreria `fast-flights` | gratis |
| `apify`  | scraper Volotea su [Apify](https://apify.com/studio-amba/volotea-scraper) che usa un browser vero sul sito Volotea; serve `APIFY_TOKEN` | circa 5-7 € a scansione annuale completa |
| `mock`   | prezzi finti, per provare il programma | gratis |

Con `google` una scansione annuale fa circa 1.250 richieste (solo nei giorni in cui
le rotte volano, vedi `weekdays`), con 2 secondi di pausa: circa 40-60 minuti.
I risultati restano in cache per 20 ore, quindi se interrompi e rilanci riparte da dove era.

## Installazione (Python 3.11 o superiore)

```bash
pip install -r requirements.txt
python volotea_checker.py --provider mock --dry-run   # prova senza rete
python volotea_checker.py --dry-run                   # prezzi veri, stampa il report e salva report.html + combinazioni.csv/.html
```

## Email

Configurazione tramite variabili d'ambiente:

| variabile | esempio |
|-----------|---------|
| `SMTP_HOST` | `smtp.gmail.com` (predefinito) |
| `SMTP_PORT` | `465` (SSL, predefinito) oppure `587` (STARTTLS) |
| `SMTP_USER` | il tuo indirizzo Gmail |
| `SMTP_PASSWORD` | una **password per le app** di Google (non la password normale): myaccount.google.com → Sicurezza → Verifica in due passaggi → Password per le app |
| `MAIL_TO` | destinatario (se vuoto, uguale a `SMTP_USER`) |

## Schedulazione

**Opzione A, cron sul tuo PC / Raspberry / server** (consigliata con `google`: gli IP di casa
vengono bloccati meno di quelli dei data center):

```cron
# ogni lunedì alle 7:00
0 7 * * 1 cd /percorso/volotea-checker && SMTP_USER=tu@gmail.com SMTP_PASSWORD=xxxx MAIL_TO=tu@gmail.com /usr/bin/python3 volotea_checker.py >> checker.log 2>&1
```

Su Windows si usa l'Utilità di pianificazione con lo stesso comando.

**Opzione B, GitHub Actions** (gratis, niente PC acceso): metti questa cartella in un
repository GitHub (anche privato), vai in Settings → Secrets and variables → Actions e crea
i secret `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_TO` (e `APIFY_TOKEN` se usi Apify).
Il workflow `.github/workflows/report.yml` gira ogni lunedì e si può lanciare a mano dalla
scheda Actions. Google può a volte rifiutare le richieste dai server GitHub: se il report
arriva vuoto, passa a cron in locale o ad Apify.

## Personalizzare

In `config.toml`: destinazioni (ci sono già commentate Catania, Praga, Lione, Amburgo),
durata del soggiorno, numero di risultati, passeggeri, giorni della settimana di ogni rotta.
Le frequenze delle nuove rotte sono quelle annunciate a settembre 2026; se Volotea le cambia,
metti `weekdays = []` per controllare tutti i giorni (più lento).

## Test

```bash
python -m unittest discover tests
```
