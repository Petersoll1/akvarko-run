from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import time

app = FastAPI()

# Povolení CORS (pro jistotu, aby web neblokoval data)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# Health check limity
TEMP_MIN = 22.0
TEMP_MAX = 26.0
PH_MIN = 6.5
PH_MAX = 7.5
TURBIDITY_MAX = 1000
TDS_MAX = 500
WATER_LEVEL_MIN = 20

# Nastavení termostatu
target_temp = 24.0
HYSTERESIS = 0.2
heater_cmd = False

# Sem se ukládají data
current_data = {
    "temp": 0.0,
    "ph": 0.0,
    "ph_raw": 0,
    "turbidity": 0,
    "tds": 0,
    "water_level": 0,
    "pump_state": False,
    "heater_state": False,
    "status": "Offline",
    "device_name": "Čekám na ESP...",
    "last_update": "Nikdy",
    "temp_alert": False,
    "ph_alert": False,
    "turbidity_alert": False,
    "tds_alert": False,
    "water_level_alert": False,
    "global_alert": False,
    "target_temp": 24.0
}

def scale_ph(raw_value: int) -> float:
    raw_value = max(0, min(4095, raw_value))
    return round((raw_value / 4095) * 14.0, 2)

def check_health(temp: float, ph: float, turbidity: int, tds: int, water_level: int) -> dict:
    temp_alert = not (TEMP_MIN <= temp <= TEMP_MAX) if temp != -127 else True
    ph_alert = not (PH_MIN <= ph <= PH_MAX)
    turbidity_alert = turbidity >= TURBIDITY_MAX
    tds_alert = tds > TDS_MAX
    water_level_alert = water_level < WATER_LEVEL_MIN
    
    return {
        "temp_alert": temp_alert,
        "ph_alert": ph_alert,
        "turbidity_alert": turbidity_alert,
        "tds_alert": tds_alert,
        "water_level_alert": water_level_alert,
        "global_alert": temp_alert or ph_alert or turbidity_alert or tds_alert or water_level_alert
    }

# --- HLAVNÍ STRÁNKA (VIEW) ---
@app.get("/")
async def dashboard(request: Request):
    # Tady se vezmou uložená data a pošlou se do index.html
    return templates.TemplateResponse("index.html", {"request": request, "data": current_data})

# --- PŘÍJEM DAT Z ESP32 (LOGIC) ---
# Tady byla chyba! ESP32 posílá na /api/data, tak to musíme chytat TADY.
@app.post("/api/data")
async def receive_data(data: dict):
    global current_data, heater_cmd
    
    # 1. Rozbalíme data z ESP32
    temp = data.get("temp", 0.0)
    ph_raw = data.get("ph", 0)
    turbidity = data.get("turbidity", 0)
    tds = data.get("tds", 0)
    water_level = data.get("water_level", 0)
    pump_state = data.get("pump_state", False)
    heater_state = data.get("heater_state", False)
    
    # 2. Přepočítáme pH
    ph_scaled = scale_ph(ph_raw)
    
    # 3. Zkontrolujeme zdraví akvária
    health = check_health(temp, ph_scaled, turbidity, tds, water_level)
    
    # 4. Logika termostatu
    if temp != -127:
        if temp < (target_temp - HYSTERESIS):
            heater_cmd = True
        elif temp > (target_temp + HYSTERESIS):
            heater_cmd = False
    
    # 5. ULOŽÍME DATA (aby je viděl web)
    current_data.update({
        "temp": temp,
        "ph": ph_scaled,
        "ph_raw": ph_raw,
        "turbidity": turbidity,
        "tds": tds,
        "water_level": water_level,
        "pump_state": pump_state,
        "heater_state": heater_state,
        "status": "Online",  # Teď už víme, že je online!
        "device_name": data.get("device_name", "ESP32 Akvárko"),
        "last_update": time.strftime("%d.%m.%Y %H:%M:%S"),
        "target_temp": target_temp,
        **health
    })
    
    print(f"✅ Data uložena! Teplota: {temp}°C | pH: {ph_scaled}")
    
    # Odpovíme ESPčku, jestli má topit
    return {"message": "Data saved", "heater_cmd": heater_cmd, "target_temp": target_temp}

# --- NASTAVENÍ CÍLOVÉ TEPLOTY Z WEBU ---
@app.post("/set_target")
async def set_target(data: dict):
    global target_temp, current_data
    new_target = data.get("target_temp", 24.0)
    target_temp = max(18.0, min(30.0, float(new_target)))
    current_data["target_temp"] = target_temp
    print(f"🎯 Cílová teplota změněna na: {target_temp}°C")
    return {"message": "Target updated", "target_temp": target_temp}