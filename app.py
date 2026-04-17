import streamlit as st
import requests
import os
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

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

    api_key: Optional[str] = None
    try:
        api_key = st.secrets.get("MARKETCHECK_API_KEY")
    except Exception:
        api_key = None
    if not api_key:
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
            signal="UNKNOWN",
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
            signal="UNKNOWN",
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


st.set_page_config(page_title="Deal Analyzer", layout="centered")
st.title("Deal Analyzer")

with st.form("deal_form"):
    vin = st.text_input("VIN (17 characters)", max_chars=17)
    mileage = st.number_input("Mileage", min_value=0, step=1000, value=50000)
    zip_code = st.text_input("ZIP Code", max_chars=5, value="")

    with st.expander("Advanced Settings"):
        radius = st.number_input("Search Radius (miles)", min_value=10, max_value=100, value=DEFAULT_RADIUS_MILES, help="MarketCheck caps radius at 100 miles on most plans.")
        recon_reserve = st.number_input("Recon Reserve ($)", min_value=0, step=100, value=DEFAULT_RECON_RESERVE)
        target_profit = st.number_input("Target Profit ($)", min_value=0, step=100, value=DEFAULT_TARGET_PROFIT)
        min_margin_pct = st.number_input("Min Margin (%)", min_value=0.0, max_value=100.0, step=1.0, value=DEFAULT_MIN_MARGIN_PCT * 100) / 100.0

    submitted = st.form_submit_button("Analyze Deal")

if submitted:
    if not vin or len(vin) != 17:
        st.error("Please enter a valid 17-character VIN.")
    elif not zip_code or len(zip_code) != 5 or not zip_code.isdigit():
        st.error("Please enter a valid 5-digit ZIP code.")
    else:
        with st.spinner("Decoding VIN and pulling comps..."):
            try:
                result = analyze_deal(
                    vin=vin,
                    mileage=int(mileage),
                    zip_code=zip_code,
                    radius=int(radius),
                    recon_reserve=int(recon_reserve),
                    target_profit=int(target_profit),
                    min_margin_pct=min_margin_pct,
                )

                vehicle_name = " ".join(
                    [str(x) for x in [result.vehicle.year, result.vehicle.make, result.vehicle.model, result.vehicle.trim] if x]
                ).strip()

                st.subheader(vehicle_name or result.vehicle.vin)

                if result.signal == "BUY":
                    st.success(f"Signal: {result.signal}")
                elif result.signal == "UNKNOWN":
                    st.info(f"Signal: {result.signal}")
                else:
                    st.warning(f"Signal: {result.signal}")

                col1, col2 = st.columns(2)
                with col1:
                    st.metric("Estimated Value", format_money(result.estimated_value))
                    st.metric("Buy Price Target", format_money(result.buy_price_target))
                with col2:
                    st.metric("Target Profit", format_money(result.target_profit))
                    margin_str = "N/A" if result.projected_profit_margin_pct is None else f"{result.projected_profit_margin_pct}%"
                    st.metric("Projected Margin", margin_str)

                st.markdown("---")
                st.caption("Vehicle Details")
                details_col1, details_col2, details_col3 = st.columns(3)
                with details_col1:
                    st.write(f"**VIN:** {result.vehicle.vin}")
                    st.write(f"**Mileage:** {result.mileage:,}")
                with details_col2:
                    st.write(f"**Body:** {result.vehicle.body_class or 'Unknown'}")
                    st.write(f"**Drive:** {result.vehicle.drivetrain or 'Unknown'}")
                with details_col3:
                    st.write(f"**Fuel:** {result.vehicle.fuel_type or 'Unknown'}")
                    st.write(f"**ZIP:** {result.zip_code}")

                if result.comp_summary and result.comp_summary.count > 0:
                    st.markdown("---")
                    st.caption("Comp Summary")
                    comp_col1, comp_col2 = st.columns(2)
                    with comp_col1:
                        st.write(f"**Comp Count:** {result.comp_summary.count}")
                        st.write(f"**Median Price:** {format_money(result.comp_summary.median_price)}")
                    with comp_col2:
                        st.write(f"**Average Price:** {format_money(result.comp_summary.avg_price)}")
                        st.write(f"**Adjusted Value:** {format_money(result.comp_summary.adjusted_for_mileage)}")

                if result.notes:
                    st.markdown("---")
                    st.caption("Notes")
                    for note in result.notes:
                        st.write(f"- {note}")

            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                detail = ""
                if exc.response is not None:
                    try:
                        detail = exc.response.json().get("message") or exc.response.text[:300]
                    except Exception:
                        detail = exc.response.text[:300]
                st.error(f"MarketCheck API error (HTTP {status}): {detail or 'no details returned'}")
            except Exception as exc:
                st.error(f"Error: {type(exc).__name__}: {exc}")
