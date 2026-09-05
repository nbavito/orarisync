# ============================================================
# MAIN
# ============================================================

def main():
    print("========================================")
    print("🚀 AVVIO AGGIORNAMENTO ORARI (ottimizzato)")
    print("========================================")

    sheet = connetti_sheet()
    print("🔗 Collegato a Google Sheets")

    colonne = prepara_colonne(sheet)
    if "id_impianto" not in colonne:
        print("❌ Manca la colonna id_impianto")
        return

    dati = sheet.get_all_values()
    if len(dati) <= 1:
        print("❌ Nessun impianto trovato")
        return

    col_id = colonne.index("id_impianto")
    totale = len(dati) - 1
    print(f"\n📋 Totale impianti: {totale}")

    # Riprendi da un checkpoint se il giro precedente è stato interrotto
    completati = carica_checkpoint()
    if completati:
        print(f"↩️  Riprendo un giro interrotto: {len(completati)} impianti già fatti")

    # Prepara la lista dei task da eseguire (salta quelli già fatti)
    da_fare = []
    for indice in range(1, len(dati)):
        riga = dati[indice]
        numero_riga = indice + 1
        if len(riga) <= col_id:
            continue
        id_impianto = riga[col_id].strip()
        if not id_impianto or id_impianto in completati:
            continue
        da_fare.append((id_impianto, numero_riga))

    if not da_fare:
        print("✅ Tutti gli impianti sono già stati elaborati!")
        return

    buffer_updates = []
    buffer_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(elabora_impianto, id_impianto, numero_riga, colonne): id_impianto
            for id_impianto, numero_riga in da_fare
        }

        for future in as_completed(futures):
            id_impianto = futures[future]
            try:
                updates, id_fatto = future.result()
            except Exception as e:
                print(f"❌ Errore imprevisto per {id_impianto}: {e}")
                continue

            with buffer_lock:
                buffer_updates.extend(updates)
                completati.add(id_fatto)

                if len(buffer_updates) >= BATCH_SCRITTURA * 9:
                    try:
                        flush_su_sheet(sheet, buffer_updates)
                        buffer_updates = []
                        salva_checkpoint(completati)
                    except Exception as e:
                        print(f"❌ Errore scrittura Google Sheet: {e}")

        # Flush finale di eventuali update rimasti in buffer
        with buffer_lock:
            try:
                flush_su_sheet(sheet, buffer_updates)
            except Exception as e:
                print(f"❌ Errore scrittura finale Google Sheet: {e}")

    # Operazione completata: pulisce il checkpoint ed esce
    pulisci_checkpoint()

    print("\n========================================")
    print("✅ ELABORAZIONE COMPLETATA")
    print("========================================")
