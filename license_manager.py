import hashlib
import socket
import datetime
import os
import subprocess

# ==============================
# CONFIG
# ==============================
WARNING_DAYS = 3  # reminder before expiry
LICENSE_FILE = os.path.join(os.path.dirname(__file__), "license.txt")


# ==============================
# MACHINE ID (STABLE)
# ==============================
def get_machine_id():
    """
    Uses Windows UUID (stable across reboots)
    """
    try:
        output = subprocess.check_output("wmic csproduct get uuid", shell=True)
        lines = output.decode().splitlines()
        for line in lines:
            line = line.strip()
            if line and line != "UUID":
                return line
    except:
        pass

    # fallback (very rare)
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()


# ==============================
# LOAD LICENSE FILE
# ==============================
def load_license():
    if not os.path.exists(LICENSE_FILE):
        return None

    data = {}
    with open(LICENSE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                data[k.strip()] = v.strip()
    return data


# ==============================
# DATE PARSER (ALL FORMATS)
# ==============================
def parse_expiry_date(date_str):
    formats = [
        "%Y-%m-%d",   # 2026-01-16
        "%d-%m-%Y",   # 16-01-2026
        "%d/%m/%Y",   # 16/01/2026
        "%m/%d/%Y",   # 01/16/2026
        "%Y/%m/%d",   # 2026/01/16
    ]

    for fmt in formats:
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except:
            continue

    return None


# ==============================
# MAIN LICENSE CHECK
# ==============================
def is_license_valid():
    lic = load_license()

    if not lic:
        return False, "License file not found"

    machine_id = get_machine_id()

    if lic.get("MACHINE_ID") != machine_id:
        return False, "License invalid – machine mismatch"

    expiry_str = lic.get("EXPIRY")
    expiry_date = parse_expiry_date(expiry_str)

    if not expiry_date:
        return False, "License expiry format invalid"

    today = datetime.date.today()
    days_left = (expiry_date - today).days

    if days_left < 0:
        return False, "License expired. Please renew."

    if days_left <= WARNING_DAYS:
        return True, f"License will expire in {days_left} day(s). Please renew soon."

    return True, "License valid"







