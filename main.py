from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# --- VÝCHOZÍ NASTAVENÍ ---
# Tyto hodnoty se použijí po restartu serveru, než ESP pošle první data
DEFAULT_TARGET_TEMP = 24.0

# Ostatní limity (které se nemění podle teploty)
PH_MIN = 6.5
PH_MAX = 7.5
TURBIDITY_LIMIT = 2000 
TDS_LIMIT = 500
WATER_LEVEL_MIN = 30 

# Hystereze pro topení (0.5 stupně)
HYSTERESIS = 0.5
# Hystereze pro ALARM (1.0 stupeň - jak jsi chtěl)
ALARM_TOLERANCE = 1.0

heater_cmd = False

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
    "target_temp": DEFAULT_TARGET_TEMP,
    # Alerty
    "temp_alert": False,
    "ph_alert": False,
    "turbidity_alert": False,
    "tds_alert": False,
    "water_level_alert": False,
    "global_alert": False
}

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
        "turbidity_alert": data["turbidity"] < TURBIDITY_LIMIT,
        "tds_alert": data["tds"] > TDS_LIMIT,
        "water_level_alert": data["water_level"] < WATER_LEVEL_MIN
    }
    alerts["global_alert"] = any(alerts.values())
    return alerts

@app.get("/")
async def dashboard(request: Request):
    global current_data
    
    # Offline detekce (20 sekund)
    time_diff = time.time() - current_data["last_timestamp"]
    if current_data["last_timestamp"] != 0 and time_diff > 20:
        current_data["status"] = "Offline 🔴"
    else:
        if current_data["last_timestamp"] != 0:
            current_data["status"] = "Online 🟢"

    return templates.TemplateResponse("index.html", {"request": request, "data": current_data})

@app.post("/api/data")
async def receive_data(data: dict):
    global current_data, heater_cmd
    
    current_timestamp = time.time()
    formatted_time = time.strftime("%H:%M:%S", time.localtime(current_timestamp))

    # Načtení a zaokrouhlení teploty
    raw_temp = data.get("temp", -127)
    if raw_temp != -127:
        temp = round(float(raw_temp), 1) # Zaokrouhlení na 1 desetinné místo
    else:
        temp = -127

    # Logika Termostatu (Ovládání topení)
    # Topíme, jen když teplota klesne pod (Cíl - 0.5)
    target = current_data["target_temp"]
    
    if temp != -127:
        if temp < (target - HYSTERESIS):
            heater_cmd = True  # Zapnout topení
        elif temp > target:
            heater_cmd = False # Vypnout, až dosáhneme cíle
            # (Tím se zajistí, že to nebude cvakat sem a tam)
    
    current_data.update({
        "temp": temp,
        "ph": data.get("ph", 0),
        "turbidity": data.get("turbidity", 0),
        "tds": data.get("tds", 0),
        "water_level": data.get("water_level", 0),
        "pump_state": data.get("pump_state", True),
        "heater_state": data.get("heater_state", False),
        "device_name": data.get("device_name", "ESP32"),
        "status": "Online 🟢",
        "last_update": formatted_time,
        "last_timestamp": current_timestamp,
        # target_temp neměníme, zůstává nastavená uživatelem
    })
    
    alerts = check_health(current_data)
    current_data.update(alerts)
    
    print(f"✅ Data: {temp}°C (Cíl: {target}°C) | Topení: {heater_cmd}")
    
    return {"message": "Data saved", "heater_cmd": heater_cmd}

@app.post("/set_target")
async def set_target(data: dict):
    global current_data
    try:
        # Uživatel změnil cílovou teplotu na webu
        new_target = float(data.get("target_temp", 24.0))
        current_data["target_temp"] = new_target
        
        # Hned přepočítáme alerty s novou cílovou teplotou
        alerts = check_health(current_data)
        current_data.update(alerts)
        
        return {"status": "ok", "target": new_target}
    except:
        return {"status": "error"}
