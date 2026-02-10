from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from collections import deque
import statistics
import time

app = FastAPI()

# --- GLOBÁLNÍ NASTAVENÍ (přežije po dobu běhu serveru) ---
# Tyto hodnoty se používají pro termostat a zobrazování
SETTINGS = {
    "target_temp": 24.0,
    "tank_volume": 50
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# Limity kvality vody (vědecky přesné hodnoty dle požadavků práce)
PH_MIN = 6.0
PH_MAX = 8.2
TURBIDITY_LIMIT = 30      # Jednotka: NTU. Alarm pokud hodnota > LIMIT. Pitná voda <5, akvárium <30 OK, >30 znečištěná
TDS_LIMIT = 500           # Jednotka: PPM. Alarm pokud hodnota > LIMIT
WATER_LEVEL_MIN = 40      # Procenta

# Hystereze pro topení (0.5 stupně)
HYSTERESIS = 0.5
# Hystereze pro ALARM (1.5 stupně)
ALARM_TOLERANCE = 1.5

heater_cmd = False

# --- HISTORIE DAT PRO VĚDECKOU ANALÝZU ---
# Ukládáme data jednou za minutu, maxlen=2000 pokryje cca 33 hodin
history = deque(maxlen=2000)
last_history_save = 0  # Timestamp posledního uložení do historie

# --- DATOVÉ ÚLOŽIŠTĚ ---
current_data = {
    "temp": 0.0,
    "ph": 0.0,
    "turbidity": 0,
    "tds": 0,
    "water_level": 0,
    "pump_state": True,        # Předpokládáme, že čerpadlo jede
    "heater_state": False,
    "status": "Čekám...",
    "device_name": "Neznámé",
    "last_update": "Nikdy",
    "last_timestamp": 0,
    "target_temp": SETTINGS["target_temp"],
    "tank_volume": SETTINGS["tank_volume"],
    # Alerty
    "temp_alert": False,
    "ph_alert": False,
    "turbidity_alert": False,
    "tds_alert": False,
    "water_level_alert": False,
    "global_alert": False,
    # Doporučení rádce
    "advice": [],
    # Vědecká analýza
    "wqi": 0,                    # Water Quality Index (0-100%)
    "temp_stability": 0.0,       # Tepelná stabilita (směrodatná odchylka)
    "temp_stability_text": "Nedostatek dat",
    "tds_prediction_days": None, # Predikce dnů do výměny vody
    "history_count": 0           # Počet záznamů v historii
}

# --- FUNKCE CHYTRÝ RÁDCE (SMART ADVISOR) ---
def generate_advice(data, volume):
    """
    Generuje seznam doporučení na základě naměřených dat a objemu akvária.
    Vrací seznam slovníků s textem a typem (ok/warning/danger).
    """
    advice_list = []
    target = data["target_temp"]
    temp = data["temp"]
    
    # Kontrola TDS (rozpuštěné látky)
    if data["tds"] > TDS_LIMIT:
        water_change = volume * 0.3
        advice_list.append({
            "text": f"Voda je znečištěná. Vyměň okamžitě 30 % vody (tj. cca {water_change:.0f} litrů).",
            "type": "danger"
        })
    
    # Kontrola zákalu (turbidity)
    if data["turbidity"] > TURBIDITY_LIMIT:
        water_change = volume * 0.2
        advice_list.append({
            "text": f"Voda je zakalená. Vyčisti filtr, odkal dno a vyměň {water_change:.0f} litrů vody.",
            "type": "warning"
        })
    
    # Kontrola pH - příliš kyselá
    if data["ph"] < PH_MIN and data["ph"] > 0:
        soda_amount = volume / 50
        advice_list.append({
            "text": f"Voda je příliš kyselá. Přidej jedlou sodu (cca {soda_amount:.1f} kávové lžičky) nebo přípravek pH Plus.",
            "type": "warning"
        })
    
    # Kontrola pH - příliš zásaditá
    if data["ph"] > PH_MAX:
        advice_list.append({
            "text": "Voda je příliš zásaditá. Přidej přípravek pH Minus nebo kousek rašeliny do filtru.",
            "type": "warning"
        })
    
    # Kontrola teploty - příliš studená
    if temp != -127 and temp < (target - 1.0):
        heater_power = volume  # Doporučený výkon topítka cca 1W na litr
        advice_list.append({
            "text": f"Voda je studená. Zkontroluj topítko. Doporučený výkon topítka pro {volume} l je cca {heater_power} W.",
            "type": "warning"
        })
    
    # Kontrola teploty - příliš teplá
    if temp != -127 and temp > (target + 2.0):
        advice_list.append({
            "text": "Voda je příliš teplá. Vypni topítko, přidej provzdušňování nebo polož na hladinu zmrazené PET lahve.",
            "type": "warning"
        })
    
    # Kontrola hladiny vody
    if data["water_level"] < WATER_LEVEL_MIN:
        advice_list.append({
            "text": "Nízká hladina vody. Doplň odpařenou vodu (nejlépe odstátou nebo přefiltrovanou).",
            "type": "warning"
        })
    
    # Pokud je vše OK
    if len(advice_list) == 0:
        advice_list.append({
            "text": "Voda je v perfektní kondici. Jen tak dál! 🐠",
            "type": "ok"
        })
    
    return advice_list

# --- FUNKCE PRO VĚDECKOU ANALÝZU (SOČ FEATURES) ---
def calculate_wqi(data):
    """
    Výpočet Indexu kvality vody (Water Quality Index) 0-100%.
    Vážený průměr penalizující odchylky od ideálních hodnot.
    """
    score = 100.0
    
    # pH skóre (ideál 7.0, rozsah 6.0-8.2)
    ph = data["ph"]
    if ph > 0:
        ph_deviation = abs(ph - 7.0)
        ph_penalty = min(ph_deviation * 15, 30)  # Max penalizace 30 bodů
        score -= ph_penalty
    
    # TDS skóre (ideál < 300, limit 500)
    tds = data["tds"]
    if tds > 500:
        score -= 30  # Kritické - velká penalizace
    elif tds > 300:
        tds_penalty = ((tds - 300) / 200) * 20  # 0-20 bodů penalizace
        score -= tds_penalty
    
    # NTU skóre (ideál < 10, limit 30)
    ntu = data["turbidity"]
    if ntu > 30:
        score -= 25  # Kritické
    elif ntu > 10:
        ntu_penalty = ((ntu - 10) / 20) * 15  # 0-15 bodů penalizace
        score -= ntu_penalty
    
    # Teplota skóre (penalizace za odchylku od cíle)
    temp = data["temp"]
    target = data["target_temp"]
    if temp != -127:
        temp_deviation = abs(temp - target)
        if temp_deviation > 2:
            score -= 15
        elif temp_deviation > 1:
            score -= 5
    
    return max(0, min(100, int(score)))

def calculate_temp_stability(history_data):
    """
    Výpočet tepelné stability jako směrodatná odchylka teploty.
    Vrací tuple (hodnota, textový popis).
    """
    temps = [h["temp"] for h in history_data if h["temp"] != -127]
    
    if len(temps) < 5:
        return (0.0, "Nedostatek dat")
    
    try:
        stdev = statistics.stdev(temps)
        
        if stdev < 0.3:
            text = "Vynikající stabilita"
        elif stdev < 0.5:
            text = "Dobrá stabilita"
        elif stdev < 1.0:
            text = "Mírné kolísání"
        elif stdev < 2.0:
            text = "Zvýšené kolísání"
        else:
            text = "Nestabilní teplota"
        
        return (round(stdev, 2), text)
    except:
        return (0.0, "Chyba výpočtu")

def predict_tds_maintenance(history_data, current_tds, limit=500):
    """
    Lineární predikce - za kolik dní dosáhne TDS limitu.
    Vrací počet dní nebo None pokud nelze predikovat.
    """
    if len(history_data) < 10:
        return None
    
    # Získáme TDS hodnoty s časovými značkami
    tds_data = [(h["timestamp"], h["tds"]) for h in history_data if h["tds"] > 0]
    
    if len(tds_data) < 10:
        return None
    
    # Jednoduchá lineární regrese
    n = len(tds_data)
    sum_x = sum(t[0] for t in tds_data)
    sum_y = sum(t[1] for t in tds_data)
    sum_xy = sum(t[0] * t[1] for t in tds_data)
    sum_xx = sum(t[0] * t[0] for t in tds_data)
    
    denominator = n * sum_xx - sum_x * sum_x
    if denominator == 0:
        return None
    
    # Sklon přímky (změna TDS za sekundu)
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    
    if slope <= 0:
        return None  # TDS klesá nebo je stabilní - není potřeba predikce
    
    # Kolik sekund do dosažení limitu
    if current_tds >= limit:
        return 0  # Už je nad limitem
    
    seconds_to_limit = (limit - current_tds) / slope
    days_to_limit = seconds_to_limit / 86400  # Převod na dny
    
    if days_to_limit > 365:
        return None  # Příliš daleko - nepredikujeme
    
    return max(1, int(days_to_limit))

# --- FUNKCE PRO KONTROLU ZDRAVÍ (DOKTOR) ---
def check_health(data):
    target = data["target_temp"]
    temp = data["temp"]
    
    # 1. Dynamický Alarm pro Teplotu
    # Pokud je teplota mimo rozsah (Cíl +/- 1 stupeň), spustí se alarm
    if temp != -127:
        temp_is_bad = (temp < (target - ALARM_TOLERANCE)) or (temp > (target + ALARM_TOLERANCE))
    else:
        temp_is_bad = True # Senzor odpojen

    alerts = {
        "temp_alert": temp_is_bad,
        "ph_alert": not (PH_MIN <= data["ph"] <= PH_MAX),
        "turbidity_alert": data["turbidity"] > TURBIDITY_LIMIT,  # Alarm pokud NTU > LIMIT
        "tds_alert": data["tds"] > TDS_LIMIT,
        "water_level_alert": data["water_level"] < WATER_LEVEL_MIN
    }
    alerts["global_alert"] = any(alerts.values())
    return alerts

@app.get("/")
async def dashboard(request: Request):
    global current_data, SETTINGS
    
    # Použij nastavení z SETTINGS (globální slovník)
    current_data["target_temp"] = SETTINGS["target_temp"]
    current_data["tank_volume"] = SETTINGS["tank_volume"]
    
    # Offline detekce (20 sekund)
    time_diff = time.time() - current_data["last_timestamp"]
    if current_data["last_timestamp"] != 0 and time_diff > 20:
        current_data["status"] = "Offline 🔴"
    else:
        if current_data["last_timestamp"] != 0:
            current_data["status"] = "Online 🟢"

    return templates.TemplateResponse("index.html", {"request": request, "data": current_data})


# --- API PRO NASTAVENÍ (GET/POST) ---
@app.get("/api/settings")
async def get_settings():
    """Vrací aktuální nastavení pro frontend nebo jiné klienty."""
    global SETTINGS, heater_cmd
    return {
        "target_temp": SETTINGS["target_temp"],
        "tank_volume": SETTINGS["tank_volume"],
        "heater_cmd": heater_cmd
    }


@app.post("/api/settings")
async def update_settings(data: dict):
    """Aktualizuje nastavení z frontendu. Změny jsou okamžitě platné."""
    global SETTINGS, current_data, heater_cmd
    
    try:
        # Aktualizace cílové teploty
        if "target_temp" in data:
            new_target = float(data["target_temp"])
            SETTINGS["target_temp"] = new_target
            current_data["target_temp"] = new_target
            print(f"🎯 Nová cílová teplota: {new_target}°C")
        
        # Aktualizace objemu akvária
        if "tank_volume" in data:
            new_volume = max(1, int(data["tank_volume"]))
            SETTINGS["tank_volume"] = new_volume
            current_data["tank_volume"] = new_volume
            print(f"🐠 Nový objem akvária: {new_volume} l")
        
        # Přepočítáme alerty a doporučení
        alerts = check_health(current_data)
        current_data.update(alerts)
        
        advice = generate_advice(current_data, SETTINGS["tank_volume"])
        current_data["advice"] = advice
        
        return {
            "status": "ok",
            "target_temp": SETTINGS["target_temp"],
            "tank_volume": SETTINGS["tank_volume"],
            "heater_cmd": heater_cmd
        }
    except Exception as e:
        print(f"❌ Chyba při aktualizaci nastavení: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/data")
async def receive_data(data: dict):
    global current_data, heater_cmd, history, last_history_save, SETTINGS
    
    # Použij nastavení z SETTINGS (globální slovník)
    current_data["target_temp"] = SETTINGS["target_temp"]
    current_data["tank_volume"] = SETTINGS["tank_volume"]
    
    current_timestamp = time.time()
    formatted_time = time.strftime("%H:%M:%S", time.localtime(current_timestamp))

    # Načtení a zaokrouhlení teploty
    raw_temp = data.get("temp", -127)
    if raw_temp != -127:
        temp = round(float(raw_temp), 1)  # Zaokrouhlení na 1 desetinné místo
    else:
        temp = -127

    # --- VÝPOČET pH Z RAW ADC HODNOTY ---
    raw_ph = data.get("ph", 0)
    # ESP32 posílá průměr z 10 čtení (RAW ADC 0-4095)
    # Převod na napětí (3.3V reference)
    v_ph = (raw_ph / 4095.0) * 3.3
    
    # Pro senzory které posílají vyšší napětí = kyselejší (nižší pH)
    # RAW 0 = 0V = pH 14 (zásadité), RAW 4095 = 3.3V = pH 0 (kyselé)
    # Lineární mapování: pH = 14 - (napětí / 3.3) * 14
    # NEBO pro standardní pH sondy kde 2.5V = pH 7:
    # pH = 7.0 + (2.5 - napětí) * 3.5
    
    # Jednodušší přístup - přímé mapování RAW na pH
    # RAW 0 = pH 0, RAW 4095 = pH 14 (nebo naopak podle senzoru)
    # Vyzkoušíme: RAW 3000 by mělo být cca pH 7
    ph_value = 14.0 - (raw_ph / 4095.0) * 14.0
    ph_value = round(max(0, min(14, ph_value)), 1)  # Omezení na 0-14

    # --- VÝPOČET TDS S TEPLOTNÍ KOMPENZACÍ ---
    raw_tds = data.get("tds", 0)
    # Použít aktuální teplotu, nebo 25°C pokud není validní
    temp_for_comp = temp if temp != -127 else 25.0
    
    # Převod RAW hodnoty na napětí (ESP32 ADC: 12-bit = 4095, napájení 3.3V)
    v_tds = (raw_tds / 4095.0) * 3.3
    
    # Teplotní kompenzační koeficient
    k = 1.0 + 0.02 * (temp_for_comp - 25.0)
    
    # Kompenzované napětí
    v_comp = v_tds / k
    
    # Výpočet TDS v PPM (standardní vzorec pro TDS sondy)
    tds_value = (133.42 * (v_comp ** 3) - 255.86 * (v_comp ** 2) + 857.39 * v_comp) * 0.5
    tds_value = int(max(0, tds_value))  # Zaokrouhlení a omezení na kladné hodnoty

    # --- VÝPOČET ZÁKALU (TURBIDITY) - PŘEVOD RAW NA NTU ---
    raw_turbidity = data.get("turbidity", 0)
    
    # Převod RAW hodnoty na napětí
    v_turb = (raw_turbidity / 4095.0) * 3.3
    
    # Turbidity senzor: vyšší napětí = čistší voda
    # Typicky: 4.5V = 0 NTU (čistá), 2.5V = 3000 NTU (velmi zakalená)
    # Ale máme 3.3V max, takže přepočítáme rozsah
    if v_turb >= 3.2:
        ntu_value = 0  # Velmi čistá voda
    elif v_turb <= 1.0:
        ntu_value = 3000  # Velmi zakalená voda
    else:
        # Lineární interpolace mezi 1.0V (3000 NTU) a 3.2V (0 NTU)
        ntu_value = int(3000 * (3.2 - v_turb) / 2.2)
    
    # Omezení výsledku do platného rozsahu 0-3000 NTU
    ntu_value = max(0, min(3000, ntu_value))

    # Logika Termostatu (Ovládání topení)
    # Topíme, jen když teplota klesne pod (Cíl - 0.5)
    target = current_data["target_temp"]
    
    if temp != -127:
        if temp < (target - HYSTERESIS):
            heater_cmd = True  # Zapnout topení
        elif temp > target:
            heater_cmd = False  # Vypnout, až dosáhneme cíle
            # (Tím se zajistí, že to nebude cvakat sem a tam)
    
    current_data.update({
        "temp": temp,
        "ph": ph_value,          # Uložení vypočtené hodnoty pH (0-14)
        "turbidity": ntu_value,  # Uložení vypočtené hodnoty v NTU
        "tds": tds_value,        # Uložení vypočtené hodnoty v PPM
        "water_level": data.get("water_level", 0),
        "pump_state": data.get("pump_state", True),
        "heater_state": data.get("heater_state", False),
        "device_name": data.get("device_name", "ESP32"),
        "status": "Online 🟢",
        "last_update": formatted_time,
        "last_timestamp": current_timestamp,
        # target_temp neměníme, zůstává nastavená uživatelem
    })
    
    # --- SMART SAMPLING: Ukládání do historie jednou za minutu ---
    if current_timestamp - last_history_save >= 60:
        history.append({
            "timestamp": current_timestamp,
            "temp": temp,
            "tds": tds_value,
            "ntu": ntu_value,
            "ph": ph_value
        })
        last_history_save = current_timestamp
        current_data["history_count"] = len(history)
    
    alerts = check_health(current_data)
    current_data.update(alerts)
    
    # Generování doporučení od Chytrého rádce
    advice = generate_advice(current_data, current_data["tank_volume"])
    current_data["advice"] = advice
    
    # --- VĚDECKÁ ANALÝZA ---
    # Výpočet WQI (Water Quality Index)
    current_data["wqi"] = calculate_wqi(current_data)
    
    # Výpočet tepelné stability
    stability, stability_text = calculate_temp_stability(list(history))
    current_data["temp_stability"] = stability
    current_data["temp_stability_text"] = stability_text
    
    # Predikce údržby (TDS)
    current_data["tds_prediction_days"] = predict_tds_maintenance(list(history), tds_value, TDS_LIMIT)
    
    # Debug výpis RAW hodnot a vypočtených hodnot
    print(f"📊 RAW: pH={raw_ph}, TDS={raw_tds}, Turb={raw_turbidity}")
    print(f"✅ Data: {temp}°C (Cíl: {target}°C) | pH: {ph_value} | TDS: {tds_value} PPM | Zákal: {ntu_value} NTU | Topení: {heater_cmd}")
    
    return {"message": "Data saved", "heater_cmd": heater_cmd}

@app.post("/set_target")
async def set_target(data: dict):
    global SETTINGS, current_data, heater_cmd
    try:
        # Uživatel změnil cílovou teplotu na webu
        if "target_temp" in data:
            new_target = float(data.get("target_temp", 24.0))
            SETTINGS["target_temp"] = new_target
            current_data["target_temp"] = new_target
            print(f"🎯 [set_target] Nová cílová teplota: {new_target}°C (SETTINGS aktualizováno)")
        
        # Uživatel změnil objem akvária
        if "tank_volume" in data:
            new_volume = max(1, int(data.get("tank_volume", 50)))
            SETTINGS["tank_volume"] = new_volume
            current_data["tank_volume"] = new_volume
            print(f"🐠 [set_target] Nový objem akvária: {new_volume} l (SETTINGS aktualizováno)")
        
        # Hned přepočítáme alerty s novou cílovou teplotou
        alerts = check_health(current_data)
        current_data.update(alerts)
        
        # Přegenerujeme doporučení
        advice = generate_advice(current_data, SETTINGS["tank_volume"])
        current_data["advice"] = advice
        
        print(f"📊 SETTINGS stav: target_temp={SETTINGS['target_temp']}, tank_volume={SETTINGS['tank_volume']}")
        
        return {
            "status": "ok", 
            "target": SETTINGS["target_temp"],
            "volume": SETTINGS["tank_volume"],
            "heater_cmd": heater_cmd
        }
    except Exception as e:
        print(f"❌ Chyba v set_target: {e}")
        return {"status": "error", "message": str(e)}
