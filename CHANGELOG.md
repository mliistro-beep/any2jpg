# Changelog

## 0.2.0b1 — 2026-10-20

Prima versione pubblica (beta).

- Conversione in JPG di HEIC/HEIF, AVIF, PNG, WebP, TIFF, BMP, GIF e degli altri formati letti da Pillow.
- Qualità JPG regolabile (1–100).
- Conversione di cartelle intere, con o senza sottocartelle; trascinamento di file e cartelle.
- Metadati: mantenere gli originali o rimuoverli tutti; autore e copyright impostabili a mano (EXIF + XMP, UTF-8).
- Destinazione: cartella di origine (default) o cartella a scelta con struttura delle sottocartelle.
- Nessuna sovrascrittura degli originali; suffissi `_1`, `_2`… per i JPG già esistenti.
- Trasparenza su sfondo bianco, rotazione EXIF, CMYK → sRGB, immagini a 16 bit.
- Script `install.sh` per l'installazione utente, con voce nel menu applicazioni.
