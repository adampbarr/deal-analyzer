import os
import sys
import statistics
from dotenv import load_dotenv
import requests

load_dotenv()

NHTSA = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
MC = "https://api.marketcheck.com/v2/search/car/active"

TESTS = [
    {"label": "2018 Honda Civic", "vin": "2HGFC2F59JH500000", "miles": 55000, "zip": "30301"},
    {"label": "2020 Toyota RAV4",  "vin": "2T3W1RFV8LW056003", "miles": 42000, "zip": "30301"},
]

def decode(vin):
    r = requests.get(NHTSA.format(vin=vin), timeout=20)
    r.raise_for_status()
    row = r.json()["Results"][0]
    return {
        "year": row.get("ModelYear"),
        "make": (row.get("Make") or "").strip(),
        "model": (row.get("Model") or "").strip(),
    }

def comps(veh, zip_code, key):
    r = requests.get(MC, params={
        "api_key": key, "zip": zip_code, "radius": 75,
        "year": veh["year"], "make": veh["make"], "model": veh["model"], "rows": 25,
    }, timeout=20)
    r.raise_for_status()
    return r.json().get("listings", [])

def main():
    key = os.getenv("MARKETCHECK_API_KEY")
    print(f"[env] MARKETCHECK_API_KEY loaded: {bool(key)}")
    if not key:
        print("[FAIL] No key in env. .env not loaded?")
        sys.exit(1)

    for t in TESTS:
        print(f"\n=== {t['label']} (VIN {t['vin']}, ZIP {t['zip']}) ===")
        try:
            v = decode(t["vin"])
            print(f"[nhtsa] decode: {v['year']} {v['make']} {v['model']}")
            if not all([v["year"], v["make"], v["model"]]):
                print("[warn] VIN decode incomplete — sample VIN may not be real. Skipping comps.")
                continue
            listings = comps(v, t["zip"], key)
            prices = [float(x["price"]) for x in listings if x.get("price")]
            print(f"[marketcheck] listings: {len(listings)}, usable prices: {len(prices)}")
            if prices:
                med = statistics.median(prices)
                print(f"[comps] median=${med:,.0f} min=${min(prices):,.0f} max=${max(prices):,.0f}")
                buy_target = max(med - 1200 - 2500, 0)
                margin_pct = ((med - buy_target - 1200) / med) * 100
                signal = "BUY" if margin_pct >= 15 else "PASS"
                print(f"[deal] est=${med:,.0f} buy_target=${buy_target:,.0f} margin={margin_pct:.1f}% signal={signal}")
        except requests.HTTPError as e:
            print(f"[FAIL] HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            print(f"[FAIL] {type(e).__name__}: {e}")

if __name__ == "__main__":
    main()
