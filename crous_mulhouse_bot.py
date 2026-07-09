#!/usr/bin/env python3
"""
Bot de surveillance CROUS — Mulhouse
====================================
Interroge l'API de trouverunlogement.lescrous.fr toutes les heures.
Si un logement se libère (désistement) dans la zone de Mulhouse,
envoie une notification sur Discord via un webhook.

Utilisation :
    1. pip install requests
    2. Renseigner DISCORD_WEBHOOK_URL ci-dessous
    3. python crous_mulhouse_bot.py          -> boucle infinie (1 requête/heure)
       python crous_mulhouse_bot.py --once   -> une seule vérification (pour cron)
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ============================== CONFIGURATION ==============================

# Webhook Discord : Paramètres du salon -> Intégrations -> Webhooks -> Nouveau webhook
# En local : colle l'URL ci-dessous. Sur GitHub Actions : elle est lue depuis
# le secret DISCORD_WEBHOOK_URL, ne mets rien en dur dans un repo.
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/TON_ID/TON_TOKEN",
)

# ID de la campagne CROUS (47 = année 2026-2027, 42 = année 2025-2026)
TOOL_ID = 47

# Bounding box autour de Mulhouse (coin Nord-Ouest, coin Sud-Est)
# Zone large pour couvrir toute l'agglomération (Illberg, centre, etc.)
# TEST NANCY — bounds copiés depuis l'URL du site :
# ?bounds=6.083332452540421_48.74305929625056_6.275249871974014_48.547296478282604
# (format : lonNO_latNO_lonSE_latSE)
BOUNDS_NW = {"lon": 6.083332452540421, "lat": 48.74305929625056}
BOUNDS_SE = {"lon": 6.275249871974014, "lat": 48.547296478282604}
# MULHOUSE (à remettre après le test) :
# BOUNDS_NW = {"lon": 7.20, "lat": 47.82}
# BOUNDS_SE = {"lon": 7.42, "lat": 47.69}

# Intervalle entre deux vérifications (secondes) — 3600 = 1 heure
CHECK_INTERVAL = 3600

# Fichier d'état pour mémoriser les logements déjà vus (évite les doublons)
STATE_FILE = Path(__file__).parent / "crous_state.json"

API_URL = f"https://trouverunlogement.lescrous.fr/api/fr/search/{TOOL_ID}"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://trouverunlogement.lescrous.fr",
    "Referer": f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/search",
}

# ===========================================================================


def build_payload(page: int = 1) -> dict:
    """Payload identique à celui envoyé par le site quand on cherche sur la carte."""
    return {
        "idTool": TOOL_ID,
        "need_aggregation": False,
        "page": page,
        "pageSize": 50,
        "sector": None,
        "occupationModes": [],
        "location": [BOUNDS_NW, BOUNDS_SE],
        "residence": None,
        "precision": 6,
        "equipment": [],
        "price": {"min": 0, "max": 10000000},  # prix en centimes
        "toolMechanism": "Residual",
    }


def fetch_accommodations() -> list[dict]:
    """Interroge l'API et renvoie la liste des logements disponibles dans la zone."""
    items = []
    page = 1
    while True:
        resp = requests.post(API_URL, headers=HEADERS,
                             json=build_payload(page), timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", {})
        page_items = results.get("items", [])
        items.extend(page_items)

        total = results.get("total", {})
        total_count = total.get("value", len(items)) if isinstance(total, dict) else total
        if len(items) >= total_count or not page_items:
            break
        page += 1

    return items


def summarize(item: dict) -> dict:
    """Extrait les infos utiles d'un logement renvoyé par l'API."""
    residence = item.get("residence") or {}
    address = residence.get("address", "")
    occupation = item.get("occupationMode", "")

    # Prix : l'API renvoie des centimes (ex: 38500 -> 385,00 €)
    price = None
    rent = item.get("rent") or {}
    if isinstance(rent, dict):
        price = rent.get("min") or rent.get("max")
    if price:
        price = f"{price / 100:.2f} €"
    else:
        price = "?"

    area = item.get("area") or {}
    if isinstance(area, dict):
        surface = area.get("min") or area.get("max")
    else:
        surface = area
    surface = f"{surface} m²" if surface else "?"

    return {
        "id": item.get("id"),
        "label": item.get("label") or residence.get("label", "Logement CROUS"),
        "residence": residence.get("label", ""),
        "address": address,
        "price": price,
        "surface": surface,
        "occupation": occupation,
        "url": f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/accommodations/{item.get('id')}",
    }


def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_state(seen_ids: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen_ids)))


def notify_discord(new_items: list[dict]) -> None:
    """Envoie un embed Discord par logement libéré."""
    embeds = []
    for it in new_items[:10]:  # Discord limite à 10 embeds par message
        embeds.append({
            "title": f"🏠 {it['label']}",
            "url": it["url"],
            "color": 0x2ECC71,
            "description": (
                f"**Résidence :** {it['residence']}\n"
                f"**Adresse :** {it['address']}\n"
                f"**Prix :** {it['price']}  •  **Surface :** {it['surface']}\n"
                f"**Type :** {it['occupation']}"
            ),
            "footer": {"text": "CROUS Mulhouse — fonce avant qu'il parte !"},
            "timestamp": datetime.utcnow().isoformat(),
        })

    payload = {
        "content": f"@here 🚨 **{len(new_items)} logement(s) CROUS disponible(s) à Mulhouse !**",
        "embeds": embeds,
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()


def notify_error(message: str) -> None:
    """Prévient sur Discord si le bot plante (silencieux si le webhook échoue)."""
    try:
        requests.post(DISCORD_WEBHOOK_URL,
                      json={"content": f"⚠️ Bot CROUS : {message}"}, timeout=15)
    except requests.RequestException:
        pass


def check_once() -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    try:
        items = fetch_accommodations()
    except requests.RequestException as e:
        print(f"[{now}] Erreur API : {e}")
        notify_error(f"Erreur lors de la requête API ({e})")
        return

    summaries = [summarize(it) for it in items]
    current_ids = {s["id"] for s in summaries if s["id"] is not None}
    seen_ids = load_state()

    new_ids = current_ids - seen_ids
    new_items = [s for s in summaries if s["id"] in new_ids]

    if new_items:
        print(f"[{now}] 🎉 {len(new_items)} nouveau(x) logement(s) à Mulhouse !")
        for it in new_items:
            print(f"    - {it['label']} | {it['price']} | {it['url']}")
        try:
            notify_discord(new_items)
        except requests.RequestException as e:
            print(f"    Échec de l'envoi Discord : {e}")
    else:
        print(f"[{now}] Rien de nouveau ({len(current_ids)} logement(s) déjà connus dans la zone).")

    # On mémorise l'état courant : si un logement disparaît puis revient
    # (nouveau désistement), il sera re-signalé.
    save_state(current_ids)


def main() -> None:
    if "--once" in sys.argv:
        check_once()
        return

    print("Bot CROUS Mulhouse démarré — vérification toutes les "
          f"{CHECK_INTERVAL // 60} minutes. Ctrl+C pour arrêter.")
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
