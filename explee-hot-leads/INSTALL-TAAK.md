# Taak voor Claude op mijn eigen computer: explee-hot-leads installeren op mijn TransIP-server

Deze taak moet op mijn eigen computer draaien. Een cloud-sessie heeft geen SSH-sleutels
voor mijn server en kan poort 22 niet bereiken (getest). Je hebt geen lokale kopie van deze
repo nodig: alles wordt van GitHub gedownload.

## Context
Dit programma haalt elke 10 minuten nieuwe "hot leads" uit Explee (project 42547) en stuurt
elke lead als e-mail naar mij. De code en uitleg staan naast dit bestand:
https://github.com/imyuliy/CL4R1T4S/tree/claude/explee-hot-leads-crm-fisit0/explee-hot-leads
(`README.md`, `install.sh`, `explee_hot_leads.py`). Mijn server staat waarschijnlijk op
136.144.231.87 (drop.vayu-solutions.nl, TransIP). Controleer dat in mijn SSH-instellingen.

Ik ben niet technisch. Doe het zelf. Vraag me alleen wat echt nodig is, één vraag tegelijk,
in gewone taal (Nederlands).

## Regels voor geheimen
- Herhaal de Explee-sleutel en het app-wachtwoord nooit in je antwoorden.
- Zet ze nooit in een bestand op deze computer en nooit als argument op een commandoregel.
  Geef ze alleen via stdin door, zoals in stap 4.
- Toon nooit de inhoud van private SSH-keys.

## Stappen
1. **Server vinden.** Kijk in `~/.ssh/config` en `~/.ssh/known_hosts` hoe deze computer op
   mijn TransIP VPS inlogt (zoek naar transip, vayu, vps of 136.144.231.87). Kun je het niet
   vinden, vraag me dan hoe ik normaal inlog. Test daarna:
   `ssh -o BatchMode=yes HOST 'echo ok; whoami; python3 --version; test -d /run/systemd/system && echo systemd'`
   Is de gebruiker niet `root`, dan moet `ssh HOST 'sudo -n true'` slagen. Lukt dat niet,
   leg me in gewone taal uit wat er nodig is en stop.

2. **Gegevens vragen**, één voor één:
   a. mijn Explee API key (begint met `sk_explee_`),
   b. het Gmail-adres dat de mails verstuurt,
   c. het Gmail app-wachtwoord dat ik "explee" heb genoemd (16 letters; haal de spaties eruit),
   d. naar welk adres de hot leads moeten (standaard: hetzelfde Gmail-adres),
   e. of de hot leads van de afgelopen 7 dagen ook meteen gemaild moeten worden
      (standaard: ja → `yes`, anders `no`).

3. **Script op de server zetten:**
   `ssh HOST 'curl -fsSLO https://raw.githubusercontent.com/imyuliy/CL4R1T4S/refs/heads/claude/explee-hot-leads-crm-fisit0/explee-hot-leads/install.sh'`

4. **Installeren.** Het script leest de omgevingsvariabelen `EXPLEE_API_KEY`, `SMTP_USER`,
   `SMTP_PASSWORD`, `MAIL_TO` en `BACKFILL` in plaats van vragen te stellen. Geef ze via stdin
   door (printf is een shell-builtin, dus niets komt in `ps`):
   ```bash
   printf 'EXPLEE_API_KEY=%q\nSMTP_USER=%q\nSMTP_PASSWORD=%q\nMAIL_TO=%q\nBACKFILL=%q\n' \
     '<sleutel>' '<gmail>' '<app-wachtwoord>' '<ontvanger>' 'yes' \
   | ssh HOST 'set -a; . /dev/stdin; set +a; if [ "$(id -u)" -eq 0 ]; then bash install.sh </dev/null; else sudo -E bash install.sh </dev/null; fi'
   ```
   Op Windows (PowerShell, zonder bash): voer de stappen uit via `wsl` of Git Bash als die er
   zijn. Zo niet, vertel me dat en stop.
   Let op: bestaat `/etc/explee-hot-leads/config.env` al, dan houdt het script de oude
   instellingen (zonder toetsenbord kiest het "keep"). Wil ik nieuwe waarden, draai dan eerst
   `bash install.sh --uninstall` op de server (met sudo als je geen root bent).

5. **Uitkomst controleren.** In de uitvoer moeten `OK   Explee: connected` en
   `OK   Email: test email sent` staan, en het moet eindigen met `Done.`
   - Staat er `FAIL`: forceer niets. Het script zet het schema dan zelf niet aan. Laat me de
     FAIL-regel letterlijk zien (zonder geheimen) en leg in gewone taal uit wat er mis is.
   - Gaat het om Explee (bijvoorbeeld HTTP 422 of "unexpected answer"): lees
     https://api.explee.com/public/api/llms.txt en
     https://api.explee.com/public/api/openapi.json en vertel me wat er anders is dan het
     programma verwacht. Pas de code niet aan zonder het mij te vragen.

6. **Nakijken:**
   `ssh HOST 'explee-hot-leads status 2>/dev/null || sudo explee-hot-leads status; systemctl list-timers explee-hot-leads.timer --no-pager'`

7. **Afronden.** Vertel me in drie korte regels: werkt het (ja/nee), naar welk adres de
   testmail is gestuurd, en dat ik in mijn spam moet kijken als hij niet in mijn inbox staat
   (en hem dan als "geen spam" moet markeren).

Klaar is: het schema draait elke 10 minuten op de server, `status` geeft geen fout, en de
testmail is verstuurd.
