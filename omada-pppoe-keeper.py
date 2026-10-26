#!/usr/bin/env python3
"""omada-pppoe-keeper - hält die PPPoE-Verbindung des ER8411 über den Omada-Controller am Leben.

Aufruf (aus Cronicle alle 2 Minuten):
  omada-pppoe-keeper.py           normaler Lauf:
                                  - tägliche Zwangstrennung um FORCE_AT
                                  - Neuverbindung, wenn das Internet länger als DOWN_GRACE weg ist
  omada-pppoe-keeper.py --force   sofort Disconnect/Connect (zum Testen)

Konfiguration per Umgebungsvariablen (z.B. aus /etc/omada-pppoe-keeper.env):
  OMADA_URL, OMADA_USER, OMADA_PASSWORD, OMADA_GW_MAC   Pflicht
  OMADA_SITE (Default), OMADA_WAN_PORT (1), OMADA_VERIFY_SSL (1)
  DOWN_GRACE (180 s), COOLDOWN (600 s), FORCE_AT (04:00, leer = aus), FORCE_WINDOW (30 min)
  STATE_FILE (/var/lib/omada-pppoe-keeper/state.json)

Exit-Codes: 0 = alles in Ordnung, 1 = offline bzw. Eingriff nötig/fehlgeschlagen, 2 = Konfigurationsfehler
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tplink_omada_client import OmadaClient

TARGETS = ["1.1.1.1", "9.9.9.9", "8.8.8.8"]  # IPs, damit DNS keine Rolle spielt


def log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


def die(msg: str) -> None:
    log(f"FEHLER: {msg}")
    sys.exit(2)


# --- Konfiguration -------------------------------------------------------------
def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"{name} fehlt")
    return value


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    if not value.isdigit():
        die(f"{name} muss eine positive Zahl sein (ist: {value!r})")
    return int(value)


URL = env_required("OMADA_URL")
USER = env_required("OMADA_USER")
PASSWORD = env_required("OMADA_PASSWORD")
GW_MAC = env_required("OMADA_GW_MAC")
SITE = os.environ.get("OMADA_SITE", "Default")
WAN_PORT = env_int("OMADA_WAN_PORT", 1)
VERIFY_SSL = os.environ.get("OMADA_VERIFY_SSL", "1") == "1"

DOWN_GRACE = env_int("DOWN_GRACE", 180)
COOLDOWN = env_int("COOLDOWN", 600)
FORCE_WINDOW = env_int("FORCE_WINDOW", 30)
FORCE_AT = os.environ.get("FORCE_AT", "04:00").strip()
STATE = Path(os.environ.get("STATE_FILE", "/var/lib/omada-pppoe-keeper/state.json"))

if FORCE_AT:
    try:
        FORCE_HOUR, FORCE_MINUTE = map(int, FORCE_AT.split(":"))
        if not (0 <= FORCE_HOUR <= 23 and 0 <= FORCE_MINUTE <= 59):
            raise ValueError
    except ValueError:
        die(f"FORCE_AT muss im Format HH:MM sein (ist: {FORCE_AT!r})")


# --- Zustand -------------------------------------------------------------------
def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log(f"WARNUNG: {STATE} ist beschädigt, beginne neu")
        return {}


def save(state: dict) -> None:
    # atomar schreiben, damit ein Abbruch keine halbe Datei hinterlässt
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


# --- Logik ---------------------------------------------------------------------
def online() -> bool:
    return any(
        subprocess.run(["ping", "-c", "2", "-W", "2", t], capture_output=True).returncode == 0
        for t in TARGETS
    )


def forced_due(state: dict, now: float) -> bool:
    """True beim ersten Lauf ab FORCE_AT (innerhalb FORCE_WINDOW), einmal pro Tag."""
    if not FORCE_AT:
        return False
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, FORCE_HOUR, FORCE_MINUTE, 0, 0, 0, -1))
    return start <= now < start + FORCE_WINDOW * 60 and state.get("last_forced") != today(now)


def today(now: float) -> str:
    return time.strftime("%F", time.localtime(now))


async def _reconnect() -> None:
    async with OmadaClient(URL, USER, PASSWORD, verify_ssl=VERIFY_SSL) as client:
        site = await client.get_site_client(SITE)
        log(f"WAN-Port {WAN_PORT}: Disconnect")
        await site.set_gateway_wan_port_connect_state(WAN_PORT, False, GW_MAC)
        await asyncio.sleep(10)  # PADT rausgehen lassen
        log(f"WAN-Port {WAN_PORT}: Connect")
        await site.set_gateway_wan_port_connect_state(WAN_PORT, True, GW_MAC)


def reconnect() -> bool:
    try:
        asyncio.run(_reconnect())
        return True
    except Exception as exc:  # Controller nicht erreichbar, Login falsch, API-Fehler ...
        log(f"FEHLER beim Neuverbinden über den Controller: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    now = time.time()
    state = load()

    # --- manueller Test --------------------------------------------------------
    if "--force" in sys.argv:
        log("Manuelle Trennung (--force)")
        ok = reconnect()
        state.update(last_kick=now, down_since=now)
        save(state)
        return 0 if ok else 1

    # --- tägliche Zwangstrennung -----------------------------------------------
    if forced_due(state, now):
        log(f"Tägliche Trennung ({FORCE_AT})")
        if not reconnect():
            # last_forced nicht setzen -> nächster Lauf im Zeitfenster versucht es erneut
            return 1
        # down_since setzen, damit der nächste Lauf die Dauer bis zur Wiederverbindung loggt
        state.update(last_kick=now, last_forced=today(now), down_since=now)
        save(state)
        return 0

    # --- Überwachung -----------------------------------------------------------
    if online():
        if "down_since" in state:
            log(f"Wieder online nach {int(now - state['down_since'])} s")
            state.pop("down_since")
            save(state)
        else:
            log("Online")
        return 0

    state.setdefault("down_since", now)
    down_for = int(now - state["down_since"])

    if down_for < DOWN_GRACE:
        log(f"Offline seit {down_for} s, warte noch")
        save(state)
        return 0

    if now - state.get("last_kick", 0) < COOLDOWN:
        log(f"Offline seit {down_for} s, Cooldown aktiv")
        save(state)
        return 1

    log(f"Offline seit {down_for} s, stoße PPPoE neu an")
    reconnect()
    # auch bei Fehlschlag den Cooldown starten, damit der Controller nicht im 2-Minuten-Takt belastet wird
    state["last_kick"] = now
    save(state)
    return 1


if __name__ == "__main__":
    sys.exit(main())