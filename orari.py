import requests
import gspread
import json
import os
import time
import socket
import threading

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.service_account import Credentials
from requests.exceptions import (
    ConnectTimeout,
    ReadTimeout,
    ConnectionError,
    HTTPError,
    RequestException
)


# ============================================================
# CONFIGURAZIONE
# ============================================================

SPREADSHEET_ID = "132krIB3I5EVTHThK4fv7gr1M7Nzeex45APNAvGVG8jA"
CREDS_FILE = "credentials.json"
NOME_FOGLIO = "Foglio1"

URL_DETAIL = "https://carburanti.mise.gov.it/ospzApi/registry/servicearea/{}"

# ============================================================
# RIPARTENZA
# ============================================================

# ⚠️ IL PROGRAMMA PARTE DALLA RIGA 502
RIGA_INIZIO = 502

# Non usare il vecchio checkpoint
IGNORA_CHECKPOINT = True

# ============================================================
# PRESTAZIONI
# ============================================================

# Numero di richieste API contemporanee
MAX_WORKERS = 5

# Numero di impianti dopo i quali scrivere su Google Sheets
BATCH_SCRITTURA = 50

# Timeout
TIMEOUT_CONNECT = 5
TIMEOUT_READ = 10

# Tentativi API
MAX_TENTATIVI = 3

# Pausa tra i tentativi
PAUSA_RETRY = 5

# ============================================================
# FILE CHECKPOINT
# ============================================================

CHECKPOINT_FILE = "checkpoint.json"


# ============================================================
# HEADER API
# ============================================================

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://carburanti.mise.gov.it",
    "Referer": "https://carburanti.mise.gov.it/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


# ============================================================
# GIORNI
# ============================================================

GIORNI = {
    1: "Lunedì",
    2: "Martedì",
    3: "Mercoledì",
    4: "Giovedì",
    5: "Venerdì",
    6: "Sabato",
    7: "Domenica"
}


# ============================================================
# CONNESSIONE GOOGLE SHEETS
# ============================================================

def connetti_sheet():

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials = Credentials.from_service_account_file(
        CREDS_FILE,
        scopes=scopes
    )

    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    return spreadsheet.worksheet(NOME_FOGLIO)


# ============================================================
# PREPARA COLONNE
# ============================================================

def prepara_colonne(sheet):

    intestazioni = sheet.row_values(1)

    colonne_richieste = [
        "id_impianto",
        "Lunedì",
        "Martedì",
        "Mercoledì",
        "Giovedì",
        "Venerdì",
        "Sabato",
        "Domenica",
        "Ultimo aggiornamento",
        "Stato"
    ]

    modificato = False

    for colonna in colonne_richieste:

        if colonna not in intestazioni:
            intestazioni.append(colonna)
            modificato = True

    if modificato:
        sheet.update(
            "A1",
            [intestazioni]
        )

    return intestazioni


# ============================================================
# CHECKPOINT
# ============================================================

def carica_checkpoint():

    if IGNORA_CHECKPOINT:
        return set()

    if not os.path.exists(CHECKPOINT_FILE):
        return set()

    try:

        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            dati = json.load(f)

        return set(dati)

    except Exception as e:

        print(f"⚠️ Errore lettura checkpoint: {e}")
        return set()


def salva_checkpoint(completati):

    try:

        with open(
            CHECKPOINT_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                list(completati),
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(f"⚠️ Errore salvataggio checkpoint: {e}")


def pulisci_checkpoint():

    if os.path.exists(CHECKPOINT_FILE):

        try:
            os.remove(CHECKPOINT_FILE)
        except Exception:
            pass


# ============================================================
# FORMATTA ORARIO
# ============================================================

def formatta_orario(orario):

    if not isinstance(orario, dict):
        return ""

    if orario.get("flagNonComunicato") is True:
        return "Non comunicato"

    if orario.get("flagH24") is True:
        return "H24"

    if orario.get("flagChiusura") is True:
        return "Chiuso"

    if orario.get("flagOrarioContinuato") is True:

        apertura = orario.get(
            "oraAperturaOrarioContinuato",
            ""
        )

        chiusura = orario.get(
            "oraChiusuraOrarioContinuato",
            ""
        )

        if apertura and chiusura:
            return f"{apertura}-{chiusura}"

    risultato = []

    apertura_mattina = orario.get(
        "oraAperturaMattina",
        ""
    )

    chiusura_mattina = orario.get(
        "oraChiusuraMattina",
        ""
    )

    apertura_pomeriggio = orario.get(
        "oraAperturaPomeriggio",
        ""
    )

    chiusura_pomeriggio = orario.get(
        "oraChiusuraPomeriggio",
        ""
    )

    if apertura_mattina and chiusura_mattina:

        risultato.append(
            f"{apertura_mattina}-{chiusura_mattina}"
        )

    if apertura_pomeriggio and chiusura_pomeriggio:

        risultato.append(
            f"{apertura_pomeriggio}-{chiusura_pomeriggio}"
        )

    return " / ".join(risultato)


# ============================================================
# ESTRAZIONE ORARI
# ============================================================

def estrai_orari(dettaglio):

    risultato = {
        "Lunedì": "",
        "Martedì": "",
        "Mercoledì": "",
        "Giovedì": "",
        "Venerdì": "",
        "Sabato": "",
        "Domenica": ""
    }

    if not isinstance(dettaglio, dict):
        return risultato

    orariapertura = dettaglio.get("orariapertura")

    if not orariapertura:
        return risultato

    if isinstance(orariapertura, dict):

        # Alcune risposte possono avere direttamente
        # la lista dentro una proprietà
        for chiave in [
            "orari",
            "giorni",
            "data",
            "items",
            "content"
        ]:

            if chiave in orariapertura:

                possibile_lista = orariapertura[chiave]

                if isinstance(possibile_lista, list):
                    orariapertura = possibile_lista
                    break

    if not isinstance(orariapertura, list):
        return risultato

    for giorno in orariapertura:

        if not isinstance(giorno, dict):
            continue

        giorno_id = giorno.get("giornoSettimanaId")

        try:
            giorno_id = int(giorno_id)
        except Exception:
            continue

        nome_giorno = GIORNI.get(giorno_id)

        if not nome_giorno:
            continue

        risultato[nome_giorno] = formatta_orario(giorno)

    return risultato


# ============================================================
# DOWNLOAD DETTAGLIO IMPIANTO
# ============================================================

def scarica_dettaglio(id_impianto):

    url = URL_DETAIL.format(id_impianto)

    for tentativo in range(1, MAX_TENTATIVI + 1):

        try:

            print(
                f"🌐 API {id_impianto} "
                f"(tentativo {tentativo}/{MAX_TENTATIVI})"
            )

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=(
                    TIMEOUT_CONNECT,
                    TIMEOUT_READ
                )
            )

            print(
                f"📡 Risposta {id_impianto}: "
                f"HTTP {response.status_code}"
            )

            response.raise_for_status()

            try:

                dettaglio = response.json()

            except ValueError:

                print(
                    f"❌ JSON non valido per {id_impianto}"
                )

                return None, "ERRORE JSON"

            return dettaglio, "OK"

        except ConnectTimeout:

            print(
                f"⏱️ Connect timeout {id_impianto} "
                f"- tentativo {tentativo}/{MAX_TENTATIVI}"
            )

            if tentativo < MAX_TENTATIVI:
                time.sleep(PAUSA_RETRY)

        except ReadTimeout:

            print(
                f"⏱️ Read timeout {id_impianto} "
                f"- tentativo {tentativo}/{MAX_TENTATIVI}"
            )

            if tentativo < MAX_TENTATIVI:
                time.sleep(PAUSA_RETRY)

        except ConnectionError as e:

            errore = str(e).lower()

            if (
                "name resolution" in errore
                or "failed to resolve" in errore
                or "temporary failure in name resolution" in errore
            ):

                print(
                    f"🌐 Errore DNS {id_impianto}: {e}"
                )

                if tentativo < MAX_TENTATIVI:
                    time.sleep(PAUSA_RETRY)

                if tentativo == MAX_TENTATIVI:
                    return None, "ERRORE DNS"

            else:

                print(
                    f"🔌 Errore connessione {id_impianto}: {e}"
                )

                if tentativo < MAX_TENTATIVI:
                    time.sleep(PAUSA_RETRY)

                if tentativo == MAX_TENTATIVI:
                    return None, "ERRORE CONNESSIONE"

        except HTTPError as e:

            print(
                f"❌ Errore HTTP {id_impianto}: {e}"
            )

            if tentativo < MAX_TENTATIVI:
                time.sleep(PAUSA_RETRY)

            if tentativo == MAX_TENTATIVI:
                return None, "ERRORE API"

        except RequestException as e:

            print(
                f"❌ Errore richiesta {id_impianto}: {e}"
            )

            if tentativo < MAX_TENTATIVI:
                time.sleep(PAUSA_RETRY)

            if tentativo == MAX_TENTATIVI:
                return None, "ERRORE API"

        except Exception as e:

            print(
                f"❌ Errore generico {id_impianto}: {e}"
            )

            return None, "ERRORE API"

    return None, "ERRORE API"


# ============================================================
# ELABORA UN IMPIANTO
# ============================================================

def elabora_impianto(
    id_impianto,
    numero_riga,
    colonne
):

    print("")
    print(
        f"🏭 Impianto {id_impianto}"
    )
    print(
        f"📄 Riga Google Sheet: {numero_riga}"
    )

    dettaglio, stato_api = scarica_dettaglio(
        id_impianto
    )

    # ========================================================
    # ERRORE API
    # ========================================================

    if dettaglio is None:

        print(
            f"❌ Impossibile aggiornare "
            f"{id_impianto}: {stato_api}"
        )

        # Aggiorniamo solo timestamp e stato.
        # Gli eventuali vecchi orari NON vengono cancellati.

        aggiornamenti = []

        col_timestamp = colonne.index(
            "Ultimo aggiornamento"
        )

        col_stato = colonne.index(
            "Stato"
        )

        timestamp = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        aggiornamenti.append({
            "range": f"{gspread.utils.rowcol_to_a1(numero_riga, col_timestamp + 1)}",
            "values": [[timestamp]]
        })

        aggiornamenti.append({
            "range": f"{gspread.utils.rowcol_to_a1(numero_riga, col_stato + 1)}",
            "values": [[stato_api]]
        })

        return aggiornamenti, id_impianto

    # ========================================================
    # ESTRAI ORARI
    # ========================================================

    orari = estrai_orari(dettaglio)

    valori = [
        orari["Lunedì"],
        orari["Martedì"],
        orari["Mercoledì"],
        orari["Giovedì"],
        orari["Venerdì"],
        orari["Sabato"],
        orari["Domenica"]
    ]

    # ========================================================
    # CALCOLA STATO
    # ========================================================

    if not dettaglio.get("orariapertura"):

        stato = "NESSUN ORARIO"

    elif all(
        valore == "Non comunicato"
        for valore in valori
    ):

        stato = "NON COMUNICATO"

    elif any(valore != "" for valore in valori):

        stato = "OK"

    else:

        stato = "NESSUN ORARIO"

    # ========================================================
    # AGGIORNAMENTI
    # ========================================================

    aggiornamenti = []

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    for nome_colonna, valore in zip(
        [
            "Lunedì",
            "Martedì",
            "Mercoledì",
            "Giovedì",
            "Venerdì",
            "Sabato",
            "Domenica"
        ],
        valori
    ):

        indice_colonna = colonne.index(
            nome_colonna
        )

        cella = gspread.utils.rowcol_to_a1(
            numero_riga,
            indice_colonna + 1
        )

        aggiornamenti.append({
            "range": cella,
            "values": [[valore]]
        })

    col_timestamp = colonne.index(
        "Ultimo aggiornamento"
    )

    col_stato = colonne.index(
        "Stato"
    )

    cella_timestamp = gspread.utils.rowcol_to_a1(
        numero_riga,
        col_timestamp + 1
    )

    cella_stato = gspread.utils.rowcol_to_a1(
        numero_riga,
        col_stato + 1
    )

    aggiornamenti.append({
        "range": cella_timestamp,
        "values": [[timestamp]]
    })

    aggiornamenti.append({
        "range": cella_stato,
        "values": [[stato]]
    })

    print(
        f"✅ {id_impianto} → {stato}"
    )

    return aggiornamenti, id_impianto


# ============================================================
# SCRITTURA SU GOOGLE SHEETS
# ============================================================

def flush_su_sheet(sheet, updates):

    if not updates:
        return

    print(
        f"💾 Scrittura di "
        f"{len(updates)} celle su Google Sheets..."
    )

    sheet.batch_update(
        updates,
        value_input_option="USER_ENTERED"
    )

    print("✅ Google Sheets aggiornato")


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print("========================================")
    print("🚀 AGGIORNAMENTO ORARI DISTRIBUTORI")
    print("========================================")
    print(
        f"📍 Ripartenza dalla riga: {RIGA_INIZIO}"
    )
    print(
        f"⚡ Concorrenza: {MAX_WORKERS}"
    )
    print(
        f"💾 Batch scrittura: {BATCH_SCRITTURA}"
    )
    print("========================================")
    print("")

    # ========================================================
    # GOOGLE SHEETS
    # ========================================================

    sheet = connetti_sheet()

    print("🔗 Collegato a Google Sheets")

    colonne = prepara_colonne(sheet)

    if "id_impianto" not in colonne:

        print(
            "❌ Manca la colonna id_impianto"
        )

        return

    dati = sheet.get_all_values()

    if len(dati) <= 1:

        print(
            "❌ Nessun impianto trovato"
        )

        return

    col_id = colonne.index(
        "id_impianto"
    )

    totale = len(dati) - 1

    print(
        f"📋 Totale righe dati: {totale}"
    )

    # ========================================================
    # CHECKPOINT
    # ========================================================

    completati = set()

    if not IGNORA_CHECKPOINT:

        completati = carica_checkpoint()

    else:

        print(
            "♻️ Checkpoint precedente IGNORATO"
        )

    # ========================================================
    # PREPARA LISTA
    # ========================================================

    da_fare = []

    for indice in range(
        1,
        len(dati)
    ):

        numero_riga = indice + 1

        # ----------------------------------------------------
        # PARTENZA DALLA RIGA 502
        # ----------------------------------------------------

        if numero_riga < RIGA_INIZIO:
            continue

        riga = dati[indice]

        if len(riga) <= col_id:
            continue

        id_impianto = riga[col_id].strip()

        if not id_impianto:
            continue

        if id_impianto in completati:
            continue

        da_fare.append(
            (
                id_impianto,
                numero_riga
            )
        )

    print("")
    print(
        f"📋 Impianti da elaborare: "
        f"{len(da_fare)}"
    )

    if not da_fare:

        print(
            "✅ Nessun impianto da elaborare."
        )

        return

    print("")
    print(
        f"▶️ Partenza effettiva dalla "
        f"riga {RIGA_INIZIO}"
    )
    print("")

    # ========================================================
    # ELABORAZIONE PARALLELA
    # ========================================================

    buffer_updates = []

    lock = threading.Lock()

    completati_sessione = set()

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {}

        for id_impianto, numero_riga in da_fare:

            future = executor.submit(
                elabora_impianto,
                id_impianto,
                numero_riga,
                colonne
            )

            futures[future] = (
                id_impianto,
                numero_riga
            )

        # ----------------------------------------------------
        # RISULTATI
        # ----------------------------------------------------

        for future in as_completed(
            futures
        ):

            id_impianto, numero_riga = (
                futures[future]
            )

            try:

                updates, id_fatto = (
                    future.result()
                )

                with lock:

                    buffer_updates.extend(
                        updates
                    )

                    completati_sessione.add(
                        id_fatto
                    )

            except Exception as e:

                print("")
                print(
                    f"❌ Errore imprevisto "
                    f"per {id_impianto}: {e}"
                )

            # ------------------------------------------------
            # SCRITTURA A BLOCCHI
            # ------------------------------------------------

            if len(completati_sessione) % BATCH_SCRITTURA == 0:

                if buffer_updates:

                    try:

                        flush_su_sheet(
                            sheet,
                            buffer_updates
                        )

                        buffer_updates = []

                        if not IGNORA_CHECKPOINT:

                            salva_checkpoint(
                                completati_sessione
                            )

                    except Exception as e:

                        print(
                            f"❌ Errore scrittura "
                            f"Google Sheets: {e}"
                        )

    # ========================================================
    # SCRITTURA FINALE
    # ========================================================

    if buffer_updates:

        try:

            flush_su_sheet(
                sheet,
                buffer_updates
            )

        except Exception as e:

            print(
                f"❌ Errore scrittura finale: {e}"
            )

    # ========================================================
    # FINE
    # ========================================================

    if not IGNORA_CHECKPOINT:

        pulisci_checkpoint()

    print("")
    print("========================================")
    print("✅ ELABORAZIONE COMPLETATA")
    print("========================================")
    print(
        f"📊 Elaborati in questa sessione: "
        f"{len(completati_sessione)}"
    )
    print("")


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print("")
        print(
            "🛑 Processo interrotto manualmente."
        )

    except Exception as e:

        print("")
        print(
            f"💥 ERRORE FATALE: {e}"
        )
