streamlit
requests
import argparse
import os
import sys
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
MARKETCHECK_SEARCH_URL = "https://api.marketcheck.com/v2/search/car/active"
DEFAULT_RADIUS_MILES = 75
DEFAULT_RECON_RESERVE = 1200
DEFAULT_TARGET_PROFIT = 2500
DEFAULT_MIN_MARGIN_PCT = 0.15
REQUEST_TIMEOUT = 20


@dataclass
class VehicleInfo:
    vin: str
    year: Optional[int]
    make: Optional[str]
    model: Optional[str]
    trim: Optional[str]
    body_class: Optional[str]
    drivetrain: Optional[str]
    fuel_type: Optional[str]


@dataclass
class CompSummary:
    count: int
    min_price: Optional[float]
    median_price: Optional[float]
    avg_price: Optional[float]
    max_price: Optional[float]
    estimated_value: Optional[float]
    adjusted_for_mileage: Optional[float]
    sample_prices: List[float]


@dataclass
class DealAnalysis:
    vehicle: VehicleInfo
    mileage: int
    zip_code: str
    estimated_value: Optional[float]
    buy_price_target: Optional[float]
    target_profit: Optional[float]
    projected_profit_margin_pct: Optional[float]
    signal: str
    notes: List[str]
    comp_summary: Optional[CompSummary]


def safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return None if value is None else float(value)
    except Exception:
        return None


def decode_vin_nhtsa(vin: str) -> VehicleInfo:
    url = NHTSA_URL.format(vin=vin)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    results = data.get("Results", [])
    if not results:
        raise ValueError("VIN decode returned no results.")

    row = results[0]
    return VehicleInfo(
        vin=vin.upper(),
        year=safe_int(row.get("ModelYear")),
        make=(row.get("Make") or "").strip() or None,
        model=(row.get("Model") or "").strip() or None,
        trim=(row.get("Trim") or "").strip() or None,
        body_class=(row.get("BodyClass") or "").strip() or None,
        drivetrain=(row.get("DriveType") or "").strip() or None,
        fuel_type=(row.get("FuelTypePrimary") or "").strip() or None,
    )


def marketcheck_active_comps(vehicle: VehicleInfo, zip_code: str, radius: int, api_key: str, rows: int = 25) -> List[Dict[str, Any]]:
    if not vehicle.year or not vehicle.make or not vehicle.model:
        raise ValueError("Need year, make, and model from VIN decode before searching comps.")

    params = {
        "api_key": api_key,
        "zip": zip_code,
        "radius": radius,
        "year": vehicle.year,
        "make": vehicle.make,
        "model": vehicle.model,
        "rows": rows,
    }

    response = requests.get(MARKETCHECK_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    return data.get("listings", [])


def estimate_value_from_comps(listings: List[Dict[str, Any]], subject_mileage: int) -> CompSummary:
    prices: List[float] = []
    mileage_adjusted_prices: List[float] = []

    for item in listings:
        price = safe_float(item.get("price"))
        miles = safe_int(item.get("miles"))

        if price is None or price <= 0:
            continue

        prices.append(price)

        if miles is None:
            mileage_adjusted_prices.append(price)
            continue

        delta = miles - subject_mileage
        adjustment = max(min(delta * 0.08, 2500), -2500)
        mileage_adjusted_prices.append(price + adjustment)

    if not prices:
        return CompSummary(
            count=0,
            min_price=None,
            median_price=None,
            avg_price=None,
            max_price=None,
            estimated_value=None,
            adjusted_for_mileage=None,
            sample_prices=[],
        )

    median_price = statistics.median(prices)
    avg_price = statistics.mean(prices)
    adjusted_value = statistics.median(mileage_adjusted_prices) if mileage_adjusted_prices else median_price

    return CompSummary(
        count=len(prices),
        min_price=round(min(prices), 2),
        median_price=round(median_price, 2),
        avg_price=round(avg_price, 2),
        max_price=round(max(prices), 2),
        estimated_value=round(median_price, 2),
        adjusted_for_mileage=round(adjusted_value, 2),
        sample_prices=[round(x, 2) for x in prices[:10]],
    )


def analyze_deal(vin: str, mileage: int, zip_code: str, radius: int, recon_reserve: int, target_profit: int, min_margin_pct: float) -> DealAnalysis:
    notes: List[str] = []
    vehicle = decode_vin_nhtsa(vin)
    api_key = os.getenv("MARKETCHECK_API_KEY")

    if not api_key:
        notes.append("MARKETCHECK_API_KEY not found. VIN decode worked, but live market value could not be calculated.")
        notes.append("Add a MarketCheck API key to enable estimated value and BUY/PASS logic.")
        return DealAnalysis(
            vehicle=vehicle,
            mileage=mileage,
            zip_code=zip_code,
            estimated_value=None,
            buy_price_target=None,
            target_profit=None,
            projected_profit_margin_pct=None,
            signal="PASS",
            notes=notes,
            comp_summary=None,
        )

    listings = marketcheck_active_comps(vehicle, zip_code, radius, api_key)
    comp_summary = estimate_value_from_comps(listings, mileage)

    if comp_summary.adjusted_for_mileage is None:
        notes.append("No usable comps returned from MarketCheck. Could not estimate value.")
        return DealAnalysis(
            vehicle=vehicle,
            mileage=mileage,
            zip_code=zip_code,
            estimated_value=None,
            buy_price_target=None,
            target_profit=float(target_profit),
            projected_profit_margin_pct=None,
            signal="PASS",
            notes=notes,
            comp_summary=comp_summary,
        )

    estimated_value = comp_summary.adjusted_for_mileage
    buy_price_target = max(estimated_value - recon_reserve - target_profit, 0)
    projected_profit = estimated_value - buy_price_target - recon_reserve
    projected_profit_margin_pct = (projected_profit / estimated_value) * 100 if estimated_value > 0 else None

    signal = "BUY" if projected_profit_margin_pct is not None and projected_profit_margin_pct >= min_margin_pct * 100 else "PASS"

    if comp_summary.count < 3:
        notes.append("Low comp count. Treat this estimate cautiously.")

    notes.append(f"Assumed recon reserve: ${recon_reserve}")
    notes.append(f"Target profit used: ${target_profit}")
    notes.append(f"Minimum margin threshold: {round(min_margin_pct * 100, 1)}%")

    return DealAnalysis(
        vehicle=vehicle,
        mileage=mileage,
        zip_code=zip_code,
        estimated_value=round(estimated_value, 2),
        buy_price_target=round(buy_price_target, 2),
        target_profit=float(target_profit),
        projected_profit_margin_pct=round(projected_profit_margin_pct, 2) if projected_profit_margin_pct is not None else None,
        signal=signal,
        notes=notes,
        comp_summary=comp_summary,
    )


def format_money(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return "${:,.2f}".format(value)


def print_report(result: DealAnalysis) -> None:
    vehicle_name = " ".join([str(x) for x in [result.vehicle.year, result.vehicle.make, result.vehicle.model, result.vehicle.trim] if x]).strip()

    print("\nDEAL ANALYZER REPORT")
    print("-" * 60)
    print(f"Vehicle: {vehicle_name or result.vehicle.vin}")
    print(f"VIN: {result.vehicle.vin}")
    print(f"Mileage: {result.mileage:,}")
    print(f"ZIP: {result.zip_code}")
    print(f"Body: {result.vehicle.body_class or 'Unknown'}")
    print(f"Drive: {result.vehicle.drivetrain or 'Unknown'}")
    print(f"Fuel: {result.vehicle.fuel_type or 'Unknown'}")
    print("-" * 60)
    print(f"Estimated Value: {format_money(result.estimated_value)}")
    print(f"Buy Price Target: {format_money(result.buy_price_target)}")
    print(f"Target Profit: {format_money(result.target_profit)}")
    print(f"Projected Profit Margin: {('N/A' if result.projected_profit_margin_pct is None else str(result.projected_profit_margin_pct) + '%')}")
    print(f"Signal: {result.signal}")

    if result.comp_summary:
        print("-" * 60)
        print("COMP SUMMARY")
        print(f"Comp Count: {result.comp_summary.count}")
        print(f"Median Price: {format_money(result.comp_summary.median_price)}")
        print(f"Average Price: {format_money(result.comp_summary.avg_price)}")
        print(f"Adjusted Value: {format_money(result.comp_summary.adjusted_for_mileage)}")
        print(f"Sample Prices: {result.comp_summary.sample_prices}")

    if result.notes:
        print("-" * 60)
        print("NOTES")
        for note in result.notes:
            print(f"- {note}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze a used car deal from VIN + mileage + ZIP.")
    parser.add_argument("--vin", required=True, help="17-character VIN")
    parser.add_argument("--mileage", required=True, type=int, help="Current vehicle mileage")
    parser.add_argument("--zip", dest="zip_code", required=True, help="ZIP code for local comps")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_MILES, help="Comp search radius in miles")
    parser.add_argument("--recon", type=int, default=DEFAULT_RECON_RESERVE, help="Recon reserve in dollars")
    parser.add_argument("--target-profit", type=int, default=DEFAULT_TARGET_PROFIT, help="Target profit in dollars")
    parser.add_argument("--min-margin", type=float, default=DEFAULT_MIN_MARGIN_PCT, help="Minimum acceptable profit margin as decimal")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = analyze_deal(
            vin=args.vin,
            mileage=args.mileage,
            zip_code=args.zip_code,
            radius=args.radius,
            recon_reserve=args.recon,
            target_profit=args.target_profit,
            min_margin_pct=args.min_margin,
        )
        print_report(result)
        return 0
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        if exc.response is not None:
            print(exc.response.text[:1000], file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
