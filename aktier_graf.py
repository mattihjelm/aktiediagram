"""Jämför aktiekurser i ett interaktivt diagram med köp, sälj och portföljresultat."""

from datetime import date, datetime, time, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
from threading import Lock
from zoneinfo import ZoneInfo
import webbrowser

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from konvertera_aktiekop import prepare_import, write_rows


STOCK_LIST_FILE = Path(__file__).with_name("aktier.csv")
STOCK_LIST_EXAMPLE_FILE = Path(__file__).with_name("aktier.example.csv")
VIEW_RANGE_FILE = Path(__file__).with_name("aktievy.json")
PURCHASES_FILE = Path(__file__).with_name("aktiekop.csv")
MAX_STOCKS = 50
PLOT_COLORS = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]
START_DATE = "2010-01-01"
INITIAL_VISIBLE_START = pd.Timestamp("2020-01-01")
US_MARKET_TICKERS = {"BRK-B", "NDAQ", "SIRI", "RIVN"}
FX_TICKERS = {"USD": "USDSEK=X", "EUR": "EURSEK=X"}


def load_stocks() -> list[dict[str, str | bool]]:
    """Läs aktielista och initial synlighet från aktier.csv."""
    # Den lokala listan skapas från exempelfilen vid första start.
    if not STOCK_LIST_FILE.exists():
        shutil.copyfile(STOCK_LIST_EXAMPLE_FILE, STOCK_LIST_FILE)
    stocks = []
    with STOCK_LIST_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        for row_number, row in enumerate(csv.DictReader(file), start=2):
            ticker = (row.get("ticker") or "").strip()
            name = (row.get("name") or "").strip()
            show_value = (row.get("show_initially") or "").strip().lower()

            # Tomma rader är platshållare i 31-radersmallen och ignoreras.
            if not ticker:
                continue
            if not name:
                name = ticker
            if show_value not in {"true", "false", "ja", "nej", "1", "0"}:
                raise ValueError(
                    f"Ogiltigt show_initially-värde på rad {row_number} i {STOCK_LIST_FILE}. "
                    "Använd true eller false."
                )
            stocks.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "show_initially": show_value in {"true", "ja", "1"},
                }
            )

    if len(stocks) > MAX_STOCKS:
        raise ValueError(f"Högst {MAX_STOCKS} aktier kan anges i {STOCK_LIST_FILE}.")
    if not stocks:
        raise ValueError(f"Ingen ticker angiven i {STOCK_LIST_FILE}.")
    tickers = [stock["ticker"] for stock in stocks]
    names = [stock["name"] for stock in stocks]
    if len(set(tickers)) != len(tickers):
        raise ValueError(f"En ticker förekommer flera gånger i {STOCK_LIST_FILE}.")
    if len(set(names)) != len(names):
        raise ValueError(f"Ett visningsnamn förekommer flera gånger i {STOCK_LIST_FILE}.")
    return stocks


def load_visible_date_range(
    available_start: pd.Timestamp,
    available_end: pd.Timestamp,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Läs senast visat datumintervall och begränsa det till tillgänglig historik."""
    available_start = pd.Timestamp(available_start).normalize()
    available_end = pd.Timestamp(available_end).normalize()
    default_start = max(INITIAL_VISIBLE_START, available_start)
    default_end = available_end
    try:
        saved = json.loads(VIEW_RANGE_FILE.read_text(encoding="utf-8"))
        start = pd.Timestamp(saved["start"]).normalize()
        end = pd.Timestamp(saved["end"]).normalize()
        if pd.isna(start) or pd.isna(end):
            return default_start, default_end
        start = max(start, available_start)
        end = min(end, available_end)
        if start >= end:
            return default_start, default_end
        return start, end
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return default_start, default_end


def load_purchases() -> list[dict[str, str]]:
    """Läs tidigare registrerade köp från CSV-filen."""
    if not PURCHASES_FILE.exists():
        return []
    with PURCHASES_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        rows = [row for row in csv.DictReader(file, delimiter=";") if row.get("ticker")]
    for row in rows:
        row.setdefault("transaction_type", "KÖP")
        row.setdefault("commission", "0")
        row.setdefault("source_id", "")
        row.setdefault("amount_currency", "")
    return rows


def currency_for_ticker(ticker: str) -> str:
    if ticker.endswith(".ST") or ticker == "BRK-B":
        return "kr"
    if ticker in {"NDAQ", "SIRI", "RIVN"}:
        return "USD"
    if ticker == "RHM.DE":
        return "EUR"
    return ""


def fx_currency_for_ticker(ticker: str) -> str:
    if ticker == "BRK-B":
        return "SEK"
    if ticker in US_MARKET_TICKERS - {"BRK-B"}:
        return "USD"
    if ticker == "RHM.DE":
        return "EUR"
    return "SEK"


def fx_rate_to_sek(
    ticker: str,
    on_date: str | date | pd.Timestamp,
    rates_by_currency: dict[str, pd.Series],
) -> float | None:
    return fx_rate_for_currency(fx_currency_for_ticker(ticker), on_date, rates_by_currency)


def fx_rate_for_currency(
    currency: str,
    on_date: str | date | pd.Timestamp,
    rates_by_currency: dict[str, pd.Series],
) -> float | None:
    currency = currency.upper()
    if currency == "SEK":
        return 1.0
    rates = rates_by_currency.get(currency)
    if rates is None or rates.empty:
        return None
    rates_index = rates.index
    if getattr(rates_index, "tz", None) is not None:
        rates_index = rates_index.tz_localize(None)
    cutoff = pd.Timestamp(on_date)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    eligible = rates.loc[rates_index <= cutoff]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1])


def latest_closed_market_date(ticker: str) -> date:
    """Return the latest weekday after that market's close has had time to publish."""
    if ticker in US_MARKET_TICKERS:
        now = datetime.now(ZoneInfo("America/New_York"))
        close_buffer = time(16, 30)
    else:
        now = datetime.now(ZoneInfo("Europe/Stockholm"))
        close_buffer = time(17, 50)
    latest = now.date()
    if latest.weekday() >= 5 or now.time().replace(tzinfo=None) < close_buffer:
        latest -= timedelta(days=1)
    while latest.weekday() >= 5:
        latest -= timedelta(days=1)
    return latest


def quantity_in_current_shares(ticker: str, transaction_date: str, quantity: float) -> float:
    """Convert historic share counts to the share basis used by current prices."""
    # Old Sirius XM shares became 0.1 New Sirius shares on 2024-09-10.
    if ticker == "SIRI" and transaction_date[:10] < "2024-09-10":
        return quantity / 10
    return quantity


def app_version_suffix() -> str:
    repository = Path(__file__).resolve().parent
    try:
        file_times = (
            path.stat().st_mtime
            for path in repository.rglob("*")
            if path.is_file()
            and ".git" not in path.relative_to(repository).parts
            and "__pycache__" not in path.relative_to(repository).parts
            and path.name not in {"aktier_graf.html", VIEW_RANGE_FILE.name}
        )
        latest_file_time = max(file_times)
        return datetime.fromtimestamp(latest_file_time).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "filens tidsstämpel saknas"


def make_purchase_panel(
    stocks: list[dict[str, str | bool]], available_tickers: set[str], version_suffix: str,
) -> str:
    options = "".join(
        f'<option value="{stock["ticker"]}">{stock["name"]}</option>'
        for stock in stocks
        if stock["ticker"] in available_tickers
    )
    panel = r"""
<section id="purchase-panel">
  <h2>Registrera aktiehändelse</h2>
  <form id="purchase-form">
    <label>Typ <select id="purchase-type" required><option value="KÖP">Köp</option><option value="SÄLJ">Sälj</option><option value="UTDELNING">Utdelning</option></select></label>
    <label>Aktie <select id="purchase-ticker" required>__OPTIONS__</select></label>
    <label>Datum <input id="purchase-date" type="date" required value="__TODAY__"></label>
    <label id="purchase-quantity-label">Antal <input id="purchase-quantity" type="text" inputmode="decimal" placeholder="0" required></label>
    <label id="purchase-price-label">Kurs (i aktiens valuta) <input id="purchase-price" type="text" inputmode="decimal" size="8" placeholder="189,34" required></label>
    <label id="purchase-fee-label">Courtage <input id="purchase-commission" type="text" inputmode="decimal" placeholder="0,00" value="0,00" required></label>
    <button type="submit">Lägg till aktiehändelse</button>
    <p id="purchase-total" aria-live="polite">Totalbelopp: —</p>
  </form>
  <p id="purchase-status" role="status">Aktiehändelser sparas i aktiekop.csv och visas i diagrammet.</p>
</section>
<style>
  body { margin: 0; }
  #stock-chart-layout { display: flex; flex-direction: column; gap: 8px; padding: 0 12px; }
  #stock-chart-title { margin: 12px 0 0; font: 600 24px sans-serif; color: #2a3f5f; }
  #top-controls-row { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: flex-start; gap: 12px; width: calc(100% - 92px); margin-left: 92px; box-sizing: border-box; }
  #top-controls-row #purchase-panel { margin-left: 0; max-width: 100%; }
  #refresh-controls { display: flex; flex-wrap: wrap; align-items: end; align-self: center; justify-content: flex-end; gap: 10px; margin-left: clamp(20px, 3vw, 44px); max-width: 100%; font: 14px sans-serif; }
  #refresh-controls h2 { margin: 0 0 12px; font-size: 19px; }
  #quote-controls { display: flex; flex-direction: column; gap: 0; }
  #quote-buttons { display: flex; align-items: end; gap: 8px; }
  #refresh-data-button, #current-price-button { box-sizing: border-box; min-height: 42px; padding: 7px 11px; font-size: 16px; cursor: pointer; }
  #visible-date-controls { display: flex; flex-direction: column; align-items: flex-start; gap: 0; }
  #visible-date-fields { display: flex; align-items: end; gap: 8px; }
  #visible-date-controls label { display: grid; gap: 3px; }
  #visible-date-controls label span { font-size: 13px; }
  #visible-date-controls input { box-sizing: border-box; width: 12ch; padding: 6px; font: inherit; }
  #visible-date-status { color: #a33; }
  #refresh-data-status { color: #435064; }
  #visible-date-status, #refresh-data-status { text-align: right; }
  #stock-chart-viewport { display: flex; align-items: stretch; gap: 14px; width: 100%; min-width: 0; }
  #stock-chart-viewport > .plotly-graph-div { box-sizing: border-box; flex: 1 1 auto; width: 0 !important; min-width: 0; height: 82vh !important; min-height: 650px; }
  #stock-custom-legend { box-sizing: border-box; flex: 0 0 clamp(330px, 31vw, 460px); max-height: max(650px, 82vh); overflow: auto; padding: 46px 8px 10px 0; font: 14px sans-serif; }
  .stock-legend-row { display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: start; gap: 5px; padding: 4px 0; border-bottom: 1px solid #e8ebef; }
  .stock-legend-header { position: sticky; top: 0; z-index: 1; display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: center; gap: 5px; padding: 6px 0; background: white; border-bottom: 1px solid #cbd2dc; font-weight: 600; }
  .stock-legend-header span { font-size: 12px; }
  .stock-legend-help { grid-column: 1 / -1; color: #576273; font-size: 12px !important; font-weight: 400; }
  .stock-legend-row input { margin: 2px 0 0; }
  .stock-legend-name { border: 0; padding: 0; background: transparent; color: #263448; cursor: pointer; font: inherit; text-align: left; }
  .stock-legend-name:hover { text-decoration: underline; }
  .stock-legend-profit { display: block; color: #435064; font-size: 12px; line-height: 1.35; }
  .stock-legend-row.is-hidden .stock-legend-name, .stock-legend-row.is-hidden .stock-legend-profit { opacity: .48; }
  #purchase-panel, #purchase-list { margin-left: 92px; }
  #purchase-panel { box-sizing: border-box; width: fit-content; max-width: calc(100% - 92px); align-self: flex-start; font: 15px sans-serif; margin-top: 0; margin-right: 0; margin-bottom: 0; padding: 0; border: 0; }
  #purchase-panel h2 { margin: 0 0 12px; font-size: 19px; }
  #purchase-form { display: grid; grid-template-columns: max-content max-content max-content max-content max-content max-content max-content max-content; align-items: end; gap: 12px; }
  #purchase-form label { display: grid; gap: 5px; }
  #purchase-form input, #purchase-form select, #purchase-form button { box-sizing: border-box; width: 100%; min-width: 0; font: inherit; padding: 7px 9px; }
  #purchase-ticker { width: max-content !important; max-width: min(38vw, 32ch); }
  #purchase-type { width: max-content !important; }
  #purchase-date { width: 16ch !important; min-width: 16ch; }
  #purchase-quantity { box-sizing: content-box !important; width: 6ch !important; min-width: 6ch; }
  #purchase-price-label { width: max-content; }
  #purchase-price { width: 100% !important; min-width: 0; }
  #purchase-commission { width: 8ch !important; min-width: 8ch; }
  #purchase-form button { box-sizing: border-box; width: auto; min-height: 42px; padding: 7px 11px; font-size: 16px; white-space: nowrap; }
  #purchase-status { max-width: 60ch; margin: 12px 0 0; color: #435064; }
  #purchase-total { align-self: end; white-space: nowrap; margin: 0 0 8px; font-weight: 600; color: #263448; }
  #purchase-list { width: fit-content; max-width: calc(100% - 92px); box-sizing: border-box; margin-top: 12px; margin-right: 0; margin-bottom: 28px; padding: 0; border: 0; font: 15px sans-serif; }
  #purchase-list h2 { margin: 0; font-size: 19px; }
  .transaction-list-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 12px; }
  #import-events-button { padding: 7px 11px; cursor: pointer; white-space: nowrap; }
  #toggle-dividends-button { padding: 7px 11px; cursor: pointer; white-space: nowrap; }
  #sort-purchases-by-stock { padding: 0; border: 0; background: none; color: inherit; font: inherit; font-weight: 600; text-decoration: underline dotted; cursor: pointer; }
  #import-events-file { max-width: 280px; font: 14px sans-serif; }
  #show-unmapped-button { margin-left: 8px; padding: 5px 9px; cursor: pointer; }
  #import-events-default { flex-basis: 100%; color: #576273; font-size: 12px; }
  #import-events-status { min-height: 1.2em; margin: 0 0 8px; color: #435064; }
  #unmapped-rows-dialog { width: min(1200px, 94vw); max-width: 94vw; max-height: 88vh; box-sizing: border-box; padding: 18px; border: 1px solid #aab3c0; border-radius: 8px; }
  #unmapped-rows-dialog::backdrop { background: rgb(18 27 41 / 45%); }
  #unmapped-rows-dialog h2 { margin: 0 0 12px; }
  #unmapped-rows-content { max-width: 100%; max-height: 70vh; overflow: auto; border: 1px solid #d4dae2; }
  #unmapped-rows-content table { width: max-content; max-width: none; border-collapse: collapse; text-align: left; font: 13px sans-serif; }
  #unmapped-rows-content th, #unmapped-rows-content td { max-width: 360px; padding: 6px 8px; border-bottom: 1px solid #e1e5eb; white-space: nowrap; }
  #unmapped-rows-content td { overflow: hidden; text-overflow: ellipsis; }
  #unmapped-rows-content th { position: sticky; top: 0; z-index: 1; background: #f3f5f8; }
  .unmapped-dialog-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
  #purchase-list table { width: max-content; max-width: 100%; border-collapse: collapse; text-align: left; }
  #purchase-list th, #purchase-list td { padding: 7px 9px; border-bottom: 1px solid #e1e5eb; white-space: nowrap; }
  #purchase-list th { background: #f3f5f8; }
  #purchase-list td.number { text-align: right; font-variant-numeric: tabular-nums; }
  #purchase-list-content { width: max-content; max-width: 100%; max-height: 320px; overflow: auto; }
  #purchase-list-content thead th { position: sticky; top: 0; z-index: 1; }
  #transaction-tables { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); align-items: start; column-gap: 8px; row-gap: 0; width: calc(100% - 92px); margin: 12px 0 28px 92px; box-sizing: border-box; }
  #transaction-tables > section { display: contents; font: 15px sans-serif; }
  #transaction-tables .transaction-panel-controls { min-width: 0; min-height: 112px; box-sizing: border-box; padding: 0 0 10px; }
  #transaction-list-controls { grid-column: 2; grid-row: 2; }
  #portfolio-summary-controls { grid-column: 1; grid-row: 2; display: flex; align-items: flex-start; flex-wrap: wrap; gap: 14px; }
  #purchase-list-content { grid-column: 2; grid-row: 3; }
  #portfolio-summary-content { grid-column: 1; grid-row: 3; }
  #purchase-list > h2 { grid-column: 2; grid-row: 1; }
  #portfolio-summary > h2 { grid-column: 1; grid-row: 1; }
  #portfolio-summary-controls label { display: inline-flex; align-items: center; gap: 7px; }
  #transaction-tables h2 { margin: 0 0 12px; font-size: 19px; }
  #transaction-tables table { width: max-content; max-width: 100%; border-collapse: collapse; text-align: left; }
  #transaction-tables th, #transaction-tables td { padding: 7px 9px; border-bottom: 1px solid #e1e5eb; white-space: nowrap; }
  #transaction-tables th { background: #f3f5f8; }
  #transaction-tables td.number { text-align: right; font-variant-numeric: tabular-nums; }
  .transaction-table-content { max-width: 100%; overflow-x: auto; }
  #portfolio-summary-content table { width: 100%; table-layout: fixed; }
  #portfolio-summary-content td.current-price-updated { font-style: italic; }
  #portfolio-summary-content th, #portfolio-summary-content td { padding: 5px 6px; white-space: normal; overflow-wrap: anywhere; }
  #portfolio-summary-content th:last-child, #portfolio-summary-content td:last-child { width: 34px; padding: 3px; text-align: center; }
  .portfolio-row-update { display: inline-grid; place-items: center; width: 28px; height: 28px; padding: 3px; }
  .portfolio-row-update svg { display: block; width: 18px; height: 18px; }
  @media (max-width: 900px) { #stock-chart-viewport { flex-direction: column; } #stock-chart-viewport > .plotly-graph-div { flex: none; width: 100% !important; height: 70vh !important; min-height: 520px; } #stock-custom-legend { flex: 0 1 auto; width: 100%; max-height: 280px; padding: 8px; border: 1px solid #e1e5eb; } #top-controls-row { width: calc(100% - 92px); margin-left: 92px; justify-content: flex-start; } #top-controls-row #purchase-panel { max-width: 100%; margin-right: auto; } #purchase-list { max-width: calc(100% - 92px); } #refresh-controls { margin-left: clamp(12px, 3vw, 24px); } #purchase-panel, #purchase-list { margin-left: 92px; } #transaction-tables { grid-template-columns: minmax(0, 1fr); width: calc(100% - 92px); margin-left: 92px; row-gap: 0; } #purchase-list > h2 { grid-column: 1; grid-row: 1; } #transaction-list-controls { grid-column: 1; grid-row: 2; } #purchase-list-content { grid-column: 1; grid-row: 3; } #portfolio-summary > h2 { grid-column: 1; grid-row: 4; } #transaction-tables #portfolio-summary-controls { grid-column: 1; grid-row: 5; } #transaction-tables #portfolio-summary-content { grid-column: 1; grid-row: 6; } #purchase-form { grid-template-columns: repeat(2, max-content); } #purchase-form button { grid-column: 1 / -1; justify-self: start; } #purchase-total { grid-column: 1 / -1; justify-self: start; margin: 0; } }
</style>
<script>
const purchaseGraph = document.querySelector('.plotly-graph-div');
window.currentPriceTickers = new Set();
const purchasePanel = document.getElementById('purchase-panel');
const chartLayout = document.createElement('div');
chartLayout.id = 'stock-chart-layout';
const chartTitle = document.createElement('h1');
chartTitle.id = 'stock-chart-title';
chartTitle.textContent = 'Aktiernas utveckling (index 100 vid synligt startdatum) · __VERSION_SUFFIX__';
const refreshControls = document.createElement('div');
refreshControls.id = 'refresh-controls';
const refreshButton = document.createElement('button');
refreshButton.id = 'refresh-data-button';
refreshButton.type = 'button';
refreshButton.textContent = 'Hämta slutkurser';
const currentPriceButton = document.createElement('button');
currentPriceButton.id = 'current-price-button';
currentPriceButton.type = 'button';
currentPriceButton.textContent = 'Hämta aktuell kurs';
const quoteControls = document.createElement('div');
quoteControls.id = 'quote-controls';
const quoteHeading = document.createElement('h2');
quoteHeading.textContent = 'Kurser';
const quoteButtons = document.createElement('div');
quoteButtons.id = 'quote-buttons';
quoteButtons.append(refreshButton, currentPriceButton);
quoteControls.append(quoteHeading, quoteButtons);
const visibleDateControls = document.createElement('div');
visibleDateControls.id = 'visible-date-controls';
const visibleDateHeading = document.createElement('h2');
visibleDateHeading.textContent = 'Datumintervall';
const visibleDateFields = document.createElement('div');
visibleDateFields.id = 'visible-date-fields';
const visibleStartLabel = document.createElement('label');
visibleStartLabel.innerHTML = '<span>Start</span>';
const visibleStartInput = document.createElement('input');
visibleStartInput.id = 'visible-start-date';
visibleStartInput.type = 'text';
visibleStartInput.inputMode = 'numeric';
visibleStartInput.maxLength = 10;
visibleStartLabel.append(visibleStartInput);
const visibleEndLabel = document.createElement('label');
visibleEndLabel.innerHTML = '<span>Slut</span>';
const visibleEndInput = document.createElement('input');
visibleEndInput.id = 'visible-end-date';
visibleEndInput.type = 'text';
visibleEndInput.inputMode = 'numeric';
visibleEndInput.maxLength = 10;
visibleEndLabel.append(visibleEndInput);
visibleDateFields.append(visibleStartLabel, visibleEndLabel);
visibleDateControls.append(visibleDateHeading, visibleDateFields);
const visibleDateStatusElement = document.createElement('span');
visibleDateStatusElement.id = 'visible-date-status';
visibleDateStatusElement.setAttribute('role', 'status');
const refreshStatus = document.createElement('span');
refreshStatus.id = 'refresh-data-status';
refreshStatus.setAttribute('role', 'status');
refreshControls.append(quoteControls, visibleDateControls, visibleDateStatusElement, refreshStatus);
const topControlsRow = document.createElement('div');
topControlsRow.id = 'top-controls-row';
topControlsRow.append(purchasePanel, refreshControls);
const purchaseList = document.createElement('section');
purchaseList.id = 'purchase-list';
purchaseList.innerHTML = '<h2>Transaktioner</h2><div id="transaction-list-controls" class="transaction-panel-controls"><div class="transaction-list-heading"><button id="import-events-button" type="button">Importera aktiehändelser</button><input id="import-events-file" type="file" accept=".csv,text/csv" aria-label="Välj CSV-fil med aktiehändelser"><button id="toggle-dividends-button" type="button">Dölj utdelningar</button><span id="import-events-default">Standard: ~/Downloads/transactions-and-notes-export.csv</span></div><p id="import-events-status" role="status"></p><button id="show-unmapped-button" type="button" hidden></button><dialog id="unmapped-rows-dialog"><div class="unmapped-dialog-heading"><h2>Omappade källrader</h2><button id="close-unmapped-dialog" type="button">Stäng</button></div><div id="unmapped-rows-content"></div></dialog></div><div id="purchase-list-content"></div>';
const portfolioSummary = document.createElement('section');
portfolioSummary.id = 'portfolio-summary';
portfolioSummary.innerHTML = '<h2>Resultat</h2><div id="portfolio-summary-controls" class="transaction-panel-controls"><label><input id="filter-solo-lists" type="checkbox" checked> Filtrera aktier enligt solo-val</label><label><input id="filter-chart-lists" type="checkbox"> Filtrera enligt diagramval</label></div><div id="portfolio-summary-content" class="transaction-table-content"></div>';
const transactionTables = document.createElement('div');
transactionTables.id = 'transaction-tables';
transactionTables.append(purchaseList, portfolioSummary);
const chartViewport = document.createElement('div');
chartViewport.id = 'stock-chart-viewport';
const customLegend = document.createElement('aside');
customLegend.id = 'stock-custom-legend';
customLegend.setAttribute('aria-label', 'Aktielegend');
purchaseGraph.parentNode.insertBefore(chartLayout, purchaseGraph);
chartLayout.append(chartTitle, topControlsRow, chartViewport, transactionTables);
chartViewport.append(purchaseGraph, customLegend);
function finishStockChartLayout(attempt = 0) {
  if (window.renderCustomLegend && window.rebaseVisibleStockGraph) {
    window.renderCustomLegend();
    if (window.suggestPurchasePrice) window.suggestPurchasePrice();
    Plotly.Plots.resize(purchaseGraph);
    requestAnimationFrame(() => window.rebaseVisibleStockGraph({}));
  } else if (attempt < 100) {
    setTimeout(() => finishStockChartLayout(attempt + 1), 50);
  }
}
finishStockChartLayout();
const purchaseForm = document.getElementById('purchase-form');
const purchaseStatus = document.getElementById('purchase-status');
const purchaseTotal = document.getElementById('purchase-total');
const refreshDataButton = document.getElementById('refresh-data-button');
const refreshDataStatus = document.getElementById('refresh-data-status');
const visibleStartDate = document.getElementById('visible-start-date');
const visibleEndDate = document.getElementById('visible-end-date');
const visibleDateStatus = document.getElementById('visible-date-status');
function toIsoDate(value) {
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString().slice(0, 10) : '';
}
function stockholmToday() {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Stockholm', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
const initialAxisRange = purchaseGraph.layout.xaxis.range;
const sliderDataRange = purchaseGraph.layout.xaxis.rangeslider.range;
const allowedStartDate = toIsoDate(sliderDataRange[0]);
let allowedEndDate = toIsoDate(initialAxisRange[1]);
let viewRangeSaveTimer = null;
window.persistVisibleDateRange = function(range) {
  if (!Array.isArray(range) || range.length < 2) return;
  const start = toIsoDate(range[0]);
  const end = toIsoDate(range[1]);
  if (!isIsoDate(start) || !isIsoDate(end) || start >= end) return;
  clearTimeout(viewRangeSaveTimer);
  viewRangeSaveTimer = setTimeout(async () => {
    try {
      const response = await fetch('/api/view-range', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({start, end}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Datumintervallet kunde inte sparas.');
    } catch (error) {
      visibleDateStatus.textContent = `Datumintervallet kunde inte sparas: ${error.message}`;
    }
  }, 250);
};
function isIsoDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}
function applyVisibleDateRange() {
  const start = visibleStartDate.value.trim();
  const end = visibleEndDate.value.trim();
  if (!isIsoDate(start) || !isIsoDate(end)) {
    visibleDateStatus.textContent = 'Ange datum i formatet ÅÅÅÅ-MM-DD.';
    return;
  }
  if (start < allowedStartDate || end > allowedEndDate) {
    visibleDateStatus.textContent = `Välj datum mellan ${allowedStartDate} och ${allowedEndDate}.`;
    return;
  }
  if (start >= end) {
    visibleDateStatus.textContent = 'Startdatum måste vara före slutdatum.';
    return;
  }
  visibleDateStatus.textContent = '';
  Plotly.relayout(purchaseGraph, {'xaxis.range': [start, end]});
}
visibleStartDate.addEventListener('change', applyVisibleDateRange);
visibleEndDate.addEventListener('change', applyVisibleDateRange);
if (window.syncVisibleDateInputs) window.syncVisibleDateInputs();
function parsePurchaseNumber(value) {
  const normalized = String(value).trim().replace(/\s/g, '').replace(',', '.');
  return normalized ? Number(normalized) : NaN;
}
window.updatePurchaseTotal = function() {
  const quantity = parsePurchaseNumber(document.getElementById('purchase-quantity').value);
  const price = parsePurchaseNumber(document.getElementById('purchase-price').value);
  const commission = parsePurchaseNumber(document.getElementById('purchase-commission').value);
  if (!(quantity > 0) || !(price > 0) || !(commission >= 0)) {
    purchaseTotal.textContent = 'Totalbelopp: —';
    return;
  }
  const ticker = document.getElementById('purchase-ticker').value;
  const type = document.getElementById('purchase-type').value;
  const currency = ticker.endsWith('.ST') || ticker === 'BRK-B' ? 'kr'
    : (['NDAQ', 'SIRI', 'RIVN'].includes(ticker) ? 'USD' : (ticker === 'RHM.DE' ? 'EUR' : ''));
  const total = quantity * price + commission;
  const dividend = type === 'UTDELNING';
  const label = dividend ? 'Utdelning efter källskatt' : type === 'SÄLJ' ? 'Nettobelopp' : 'Totalbelopp';
  const amount = type === 'SÄLJ' || dividend ? quantity * price - commission : total;
  purchaseTotal.textContent = `${label}: ${amount.toLocaleString('sv-SE', {minimumFractionDigits: 2, maximumFractionDigits: 2})}${currency ? ` ${currency}` : ''}`;
};
['purchase-quantity', 'purchase-price', 'purchase-commission'].forEach(id =>
  document.getElementById(id).addEventListener('input', window.updatePurchaseTotal)
);
document.getElementById('purchase-commission').addEventListener('change', event => {
  const value = parsePurchaseNumber(event.target.value);
  if (Number.isFinite(value) && value >= 0) {
    event.target.value = value.toLocaleString('sv-SE', {
      useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    window.updatePurchaseTotal();
  }
});
window.updateSuggestedCommission = function() {
  if (document.getElementById('purchase-type').value === 'UTDELNING') return;
  const quantity = parsePurchaseNumber(document.getElementById('purchase-quantity').value);
  const price = parsePurchaseNumber(document.getElementById('purchase-price').value);
  if (!(quantity > 0) || !(price > 0)) return;
  // Courtaget anges i aktiens inmatningsvaluta; procentsatsen är densamma
  // efter SEK-konvertering och värdet sparas med två decimaler.
  const commission = Math.round(quantity * price * 0.0025 * 100) / 100;
  document.getElementById('purchase-commission').value = commission.toLocaleString('sv-SE', {
    useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  window.updatePurchaseTotal();
};
['purchase-quantity', 'purchase-price'].forEach(id =>
  document.getElementById(id).addEventListener('input', window.updateSuggestedCommission)
);
document.getElementById('purchase-price').addEventListener('change', event => {
  const value = parsePurchaseNumber(event.target.value);
  if (Number.isFinite(value) && value > 0) {
    event.target.value = value.toLocaleString('sv-SE', {
      useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    window.updateSuggestedCommission();
    window.updatePurchaseTotal();
  }
});
document.getElementById('purchase-ticker').addEventListener('change', () => {
  if (window.suggestPurchasePrice) window.suggestPurchasePrice();
  window.updatePurchaseTotal();
});
let previousPurchaseType = 'KÖP';
document.getElementById('purchase-type').addEventListener('change', event => {
  const type = event.target.value;
  const dividend = type === 'UTDELNING';
  const quantityLabel = document.getElementById('purchase-quantity-label');
  const quantityField = document.getElementById('purchase-quantity');
  quantityLabel.hidden = false;
  quantityLabel.firstChild.textContent = dividend ? 'Antal aktier ' : 'Antal ';
  quantityField.required = true;
  document.getElementById('purchase-price-label').firstChild.textContent = dividend ? 'Utdelning per aktie (i aktiens valuta) ' : 'Kurs (i aktiens valuta) ';
  document.getElementById('purchase-fee-label').firstChild.textContent = dividend ? 'Källskatt ' : 'Courtage ';
  if (dividend) {
    document.getElementById('purchase-price').value = '';
    document.getElementById('purchase-commission').value = '0,00';
  } else if (previousPurchaseType === 'UTDELNING') {
    quantityField.value = '';
    document.getElementById('purchase-price').value = '';
    document.getElementById('purchase-commission').value = '0,00';
    if (window.suggestPurchasePrice) window.suggestPurchasePrice();
    if (window.updateSuggestedCommission) window.updateSuggestedCommission();
  }
  previousPurchaseType = type;
  window.updatePurchaseTotal();
});
document.getElementById('purchase-date').addEventListener('change', () => {
  if (window.suggestPurchasePrice) window.suggestPurchasePrice();
});
let hideDividendTransactions = false;
let purchaseListAlphabetical = false;
window.renderPurchaseList = function() {
  const container = document.getElementById('purchase-list-content');
  if (!container || !purchaseGraph.data) return;
  const stocksByTicker = {};
  purchaseGraph.data.forEach(trace => {
    if (trace.meta && trace.meta.role === 'stock') stocksByTicker[trace.meta.ticker] = trace.meta.displayName || trace.name;
  });
  const rows = [];
  purchaseGraph.data.forEach(trace => {
    if (!trace.meta || trace.meta.role !== 'purchase') return;
    trace.x.forEach((date, index) => {
      const details = trace.customdata[index];
      const type = details[6] || 'KÖP';
      const quantity = Number(String(details[2]).replace(/\s/g, '').replace(',', '.'));
      const price = Number(String(details[1]).replace(/\s/g, '').replace(',', '.'));
      const commission = Number(String(details[5] || '0').replace(/\s/g, '').replace(',', '.')) || 0;
      const total = type === 'SÄLJ' || type === 'UTDELNING' ? quantity * price - commission : quantity * price + commission;
      const rawCurrency = details[4] || '';
      const currency = rawCurrency === 'SEK' ? 'kr' : rawCurrency;
      const formatted = value => Number.isFinite(value)
        ? value.toLocaleString('sv-SE', {minimumFractionDigits: 2, maximumFractionDigits: 2})
        : String(value ?? '');
      rows.push({
        ticker: trace.meta.ticker,
        date: String(date).slice(0, 10),
        type,
        name: stocksByTicker[trace.meta.ticker] || trace.meta.ticker,
        quantity: String(details[2]),
        price: `${formatted(price)}${currency ? ` ${currency}` : ''}`,
        commission: `${formatted(commission)}${currency ? ` ${currency}` : ''}`,
        total: `${total.toLocaleString('sv-SE', {minimumFractionDigits: 2, maximumFractionDigits: 2})}${currency ? ` ${currency}` : ''}`,
      });
    });
  });
  const soloFilter = document.getElementById('filter-solo-lists');
  const soloTicker = soloFilter && soloFilter.checked && window.getSoloTicker ? window.getSoloTicker() : null;
  const chartFilter = document.getElementById('filter-chart-lists');
  const chartTickers = chartFilter && chartFilter.checked && window.getVisibleTickers
    ? new Set(window.getVisibleTickers()) : null;
  const visibleRows = rows.filter(row =>
    (!hideDividendTransactions || row.type !== 'UTDELNING') &&
    (!soloTicker || row.ticker === soloTicker) &&
    (!chartTickers || chartTickers.has(row.ticker))
  );
  visibleRows.sort(purchaseListAlphabetical
    ? (a, b) => a.name.localeCompare(b.name, 'sv') || b.date.localeCompare(a.date)
    : (a, b) => b.date.localeCompare(a.date) || a.name.localeCompare(b.name, 'sv'));
  container.replaceChildren();
  if (!visibleRows.length) {
    const empty = document.createElement('p');
    if (chartTickers && !chartTickers.size) empty.textContent = 'Inga aktier visas i diagrammet.';
    else if (soloTicker) empty.textContent = 'Inga transaktioner för den solo-valda aktien.';
    else if (hideDividendTransactions && rows.length) empty.textContent = 'Inga köp eller försäljningar visas när utdelningar är dolda.';
    else empty.textContent = 'Inga köp är registrerade ännu.';
    container.append(empty);
    return;
  }
  const table = document.createElement('table');
  table.innerHTML = `<thead><tr><th>Datum</th><th>Typ</th><th aria-sort="${purchaseListAlphabetical ? 'ascending' : 'none'}"><button id="sort-purchases-by-stock" type="button" aria-label="Sortera transaktioner på aktie">Aktie${purchaseListAlphabetical ? ' A–Z' : ''}</button></th><th>Antal</th><th>Kurs/belopp</th><th>Courtage/källskatt</th><th>Total/nettobelopp</th></tr></thead>`;
  const sortStockButton = table.querySelector('#sort-purchases-by-stock');
  sortStockButton.addEventListener('click', () => {
    purchaseListAlphabetical = !purchaseListAlphabetical;
    window.renderPurchaseList();
  });
  const body = document.createElement('tbody');
  visibleRows.forEach(row => {
    const tr = document.createElement('tr');
    [row.date, row.type, row.name, row.quantity, row.price, row.commission, row.total].forEach((value, index) => {
      const td = document.createElement('td');
      td.textContent = value;
      if (index > 1) td.className = 'number';
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(body);
  container.append(table);
};
window.renderPortfolioSummary = function() {
  const container = document.getElementById('portfolio-summary-content');
  if (!container || !purchaseGraph.data) return;
  const stocks = new Map();
  const transactions = new Map();
  purchaseGraph.data.forEach(trace => {
    if (!trace.meta) return;
    const ticker = trace.meta.ticker;
    if (trace.meta.role === 'stock') stocks.set(ticker, trace);
    if (trace.meta.role === 'purchase') transactions.set(ticker, trace);
  });
  const number = value => Number(String(value ?? '').replace(/\s/g, '').replace(',', '.'));
  const money = value => Number(value).toLocaleString('sv-SE', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const amount = value => Number(value).toLocaleString('sv-SE', {maximumFractionDigits: 4});
  const currencyFor = ticker => ticker.endsWith('.ST') || ticker === 'BRK-B' ? 'kr'
    : (['NDAQ', 'SIRI', 'RIVN'].includes(ticker) ? 'USD' : (ticker === 'RHM.DE' ? 'EUR' : ''));
  const rows = [];
  transactions.forEach((trace, ticker) => {
    const stock = stocks.get(ticker);
    if (!stock || !trace.x.length) return;
    const currency = currencyFor(ticker);
    const events = trace.x.map((date, index) => ({date: String(date).slice(0, 10), details: trace.customdata[index]}))
      .sort((a, b) => a.date.localeCompare(b.date));
    let held = 0, localBasis = 0, sekBasis = 0, totalBuyCostSek = 0, realizedSek = 0;
    let dividendSek = 0, fxMissing = false, dividendFxMissing = false;
    events.forEach(({date, details}) => {
      const price = number(details[1]), enteredQuantity = number(details[2]);
      const quantity = ticker === 'SIRI' && date < '2024-09-10' ? enteredQuantity / 10 : enteredQuantity;
      const commission = number(details[5] || 0) || 0;
      const fx = Number(details[7]);
      const transactionType = details[6] || 'KÖP';
      if (!(enteredQuantity > 0) || (transactionType === 'UTDELNING' ? price === 0 : price <= 0)) return;
      const rate = Number.isFinite(fx) && fx > 0 ? fx : (currency === 'kr' ? 1 : null);
      if (transactionType === 'UTDELNING') {
        if (rate !== null) dividendSek += (price * enteredQuantity - commission) * rate;
        else dividendFxMissing = true;
      } else if ((details[6] || 'KÖP') === 'SÄLJ') {
        if (rate !== null) realizedSek += (price * enteredQuantity - commission) * rate;
        else fxMissing = true;
        const removed = Math.min(quantity, held);
        if (held > 0 && removed > 0) {
          const fraction = removed / held;
          if (rate !== null) realizedSek -= sekBasis * fraction;
          localBasis *= 1 - fraction;
          sekBasis *= 1 - fraction;
          held -= removed;
        }
      } else {
        const localCost = price * enteredQuantity + commission;
        held += quantity;
        localBasis += localCost;
        if (rate === null) fxMissing = true;
        else { sekBasis += localCost * rate; totalBuyCostSek += localCost * rate; }
      }
    });
    const latestIndex = stock.x.length - 1;
    const latestData = stock.customdata[latestIndex] || [];
    const latestPrice = Number(latestData[0]);
    const currentFx = Number(latestData[3]);
    const validFx = Number.isFinite(currentFx) && currentFx > 0;
    const profitExDividendSek = validFx && !fxMissing ? realizedSek + latestPrice * currentFx * held - sekBasis : null;
    const profitSek = profitExDividendSek !== null && !dividendFxMissing ? profitExDividendSek + dividendSek : null;
    const profitPercent = profitSek !== null && totalBuyCostSek > 0 ? profitSek / totalBuyCostSek * 100 : null;
    const formatKr = value => value === null ? '—' : `${value > 0 ? '+' : ''}${money(value)} kr`;
    rows.push({
      ticker,
      name: stock.meta.displayName || ticker,
      date: String(stock.x[latestIndex]).slice(0, 10),
      quantity: amount(held),
      price: latestData[1] || `${money(latestPrice)}${currency ? ` ${currency}` : ''}`,
      value: `${money(latestPrice * held)}${currency ? ` ${currency}` : ''}`,
      valueSek: validFx ? `${money(latestPrice * currentFx * held)} kr` : '—',
      gav: `${held > 0 ? money(localBasis / held) : '—'}${held > 0 && currency ? ` ${currency}` : ''}`,
      profitExDividend: formatKr(profitExDividendSek),
      dividend: formatKr(dividendFxMissing ? null : dividendSek),
      profit: formatKr(profitSek),
      percent: profitPercent === null ? '—' : `${profitPercent > 0 ? '+' : ''}${money(profitPercent)}%`,
    });
  });
  const soloFilter = document.getElementById('filter-solo-lists');
  const soloTicker = soloFilter && soloFilter.checked && window.getSoloTicker ? window.getSoloTicker() : null;
  const chartFilter = document.getElementById('filter-chart-lists');
  const chartTickers = chartFilter && chartFilter.checked && window.getVisibleTickers
    ? new Set(window.getVisibleTickers()) : null;
  const visibleRows = rows.filter(row =>
    (!soloTicker || row.ticker === soloTicker) &&
    (!chartTickers || chartTickers.has(row.ticker))
  );
  visibleRows.sort((a, b) => a.name.localeCompare(b.name, 'sv'));
  container.replaceChildren();
  if (!visibleRows.length) {
    const empty = document.createElement('p');
    if (chartTickers && !chartTickers.size) empty.textContent = 'Inga aktier visas i diagrammet.';
    else if (soloTicker) empty.textContent = 'Inga resultat för den solo-valda aktien.';
    else empty.textContent = 'Inga aktier med registrerade transaktioner.';
    container.append(empty);
    return;
  }
  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>Aktie</th><th>Antal</th><th>Senaste kurs</th><th>GAV</th><th>Resultat exkl utd</th><th>Utdelning</th><th>Resultat (kr)</th><th>Resultat (%)</th><th>Värde</th><th>Värde SEK</th><th></th></tr></thead>';
  const body = document.createElement('tbody');
  visibleRows.forEach(row => {
    const tr = document.createElement('tr');
    [row.name, row.quantity, row.price, row.gav, row.profitExDividend, row.dividend, row.profit, row.percent, row.value, row.valueSek].forEach((value, index) => {
      const td = document.createElement('td');
      td.textContent = value;
      if (index > 1) td.className = 'number';
      if (index === 2 && window.currentPriceTickers.has(row.ticker)) td.classList.add('current-price-updated');
      tr.append(td);
    });
    const actionCell = document.createElement('td');
    const updateButton = document.createElement('button');
    updateButton.type = 'button';
    updateButton.className = 'portfolio-row-update';
    updateButton.setAttribute('aria-label', `Uppdatera kurs och resultat för ${row.name}`);
    updateButton.title = `Uppdatera kurs och resultat för ${row.name}`;
    updateButton.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5"/><path d="M19 12a7 7 0 0 0-12.8-3L4 12"/><path d="M4 17v-5h5"/><path d="M5 12a7 7 0 0 0 12.8 3L20 12"/></svg>';
    updateButton.addEventListener('click', () => window.updatePortfolioRow(row.ticker, updateButton));
    actionCell.append(updateButton);
    tr.append(actionCell);
    body.append(tr);
  });
  table.append(body);
  container.append(table);
  window.syncTransactionListHeight();
};
window.updatePortfolioRow = async function(ticker, button) {
  const originalMarkup = button.innerHTML;
  button.disabled = true;
  button.textContent = 'Hämtar…';
  try {
    const response = await fetch('/api/current-prices', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Kursen kunde inte hämtas.');
    const update = result.updates && result.updates[ticker];
    const traceIndex = window.stockGraphState.stockTraceByTicker[ticker];
    if (!update || traceIndex === undefined) throw new Error((result.warnings || []).join(' ') || 'Ingen aktuell kurs hittades.');
    const trace = purchaseGraph.data[traceIndex];
    const x = Array.from(trace.x), y = Array.from(trace.y), customdata = Array.from(trace.customdata);
    const date = String(update.date).slice(0, 10), lastDate = String(x[x.length - 1]).slice(0, 10);
    const base = Number(window.stockGraphState.visibleBases[traceIndex]) || Number(customdata[0][0]);
    const point = [Number(update.price), update.customdata[1], 1.0, update.customdata[3]];
    if (date >= lastDate) {
      if (date === lastDate) {
        y[y.length - 1] = Number(update.price) / base * 100;
        customdata[customdata.length - 1] = point;
      } else {
        x.push(date); y.push(Number(update.price) / base * 100); customdata.push(point);
      }
      await Plotly.restyle(purchaseGraph, {x: [x], y: [y], customdata: [customdata]}, [traceIndex]);
    } else {
      const pointIndex = x.findIndex(value => String(value).slice(0, 10) === date);
      if (pointIndex < 0) throw new Error('Kursen är äldre än diagrammets data.');
      y[pointIndex] = Number(update.price) / base * 100;
      customdata[pointIndex] = point;
      await Plotly.restyle(purchaseGraph, {y: [y], customdata: [customdata]}, [traceIndex]);
    }
    const axisRange = purchaseGraph._fullLayout.xaxis.range.map(toIsoDate);
    const rangeEnd = [axisRange[1], stockholmToday()].sort().at(-1);
    allowedEndDate = rangeEnd > allowedEndDate ? rangeEnd : allowedEndDate;
    if (rangeEnd !== axisRange[1]) {
      await Plotly.relayout(purchaseGraph, {
        'xaxis.range': [axisRange[0], rangeEnd],
        'xaxis.rangeslider.range': [allowedStartDate, rangeEnd],
      });
    }
    window.rebaseVisibleStockGraph({});
    if (window.syncVisibleDateInputs) window.syncVisibleDateInputs();
    window.currentPriceTickers.add(ticker);
    window.renderPortfolioSummary();
    window.refreshProfitLabel(ticker);
    button.title = `Kurs och resultat för ${ticker} uppdaterade`;
  } catch (error) {
    button.title = error.message;
  } finally {
    setTimeout(() => {
      if (button.isConnected) { button.innerHTML = originalMarkup; button.disabled = false; }
    }, 1800);
  }
};
window.syncTransactionListHeight = function() {
  const list = document.getElementById('purchase-list-content');
  const summaryTable = document.querySelector('#portfolio-summary-content table');
  if (!list) return;
  const height = summaryTable ? summaryTable.getBoundingClientRect().height : 320;
  list.style.maxHeight = `${Math.max(120, Math.ceil(height))}px`;
};
window.addEventListener('resize', () => requestAnimationFrame(window.syncTransactionListHeight));
window.renderPurchaseList();
window.renderPortfolioSummary();
document.getElementById('filter-solo-lists').addEventListener('change', () => {
  window.renderPurchaseList();
  window.renderPortfolioSummary();
});
document.getElementById('filter-chart-lists').addEventListener('change', () => {
  window.renderPurchaseList();
  window.renderPortfolioSummary();
});
const importEventsButton = document.getElementById('import-events-button');
const importEventsFile = document.getElementById('import-events-file');
const importEventsStatus = document.getElementById('import-events-status');
const toggleDividendsButton = document.getElementById('toggle-dividends-button');
toggleDividendsButton.addEventListener('click', () => {
  hideDividendTransactions = !hideDividendTransactions;
  toggleDividendsButton.textContent = hideDividendTransactions ? 'Visa utdelningar' : 'Dölj utdelningar';
  window.renderPurchaseList();
});
const showUnmappedButton = document.getElementById('show-unmapped-button');
const unmappedRowsDialog = document.getElementById('unmapped-rows-dialog');
const unmappedRowsContent = document.getElementById('unmapped-rows-content');
document.getElementById('close-unmapped-dialog').addEventListener('click', () => unmappedRowsDialog.close());
showUnmappedButton.addEventListener('click', () => unmappedRowsDialog.showModal());
function showUnmappedRows(headers, rows) {
  unmappedRowsContent.replaceChildren();
  if (!rows.length) {
    showUnmappedButton.hidden = true;
    return;
  }
  const table = document.createElement('table');
  const allHeaders = ['Källrad', 'Orsak', ...headers];
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  allHeaders.forEach(label => {
    const th = document.createElement('th');
    th.textContent = label;
    headerRow.append(th);
  });
  thead.append(headerRow);
  const tbody = document.createElement('tbody');
  rows.forEach(row => {
    const tr = document.createElement('tr');
    [row.row_number, row.reason, ...(row.cells || [])].forEach(value => {
      const td = document.createElement('td');
      td.textContent = String(value ?? '');
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(thead, tbody);
  unmappedRowsContent.append(table);
  showUnmappedButton.textContent = `Visa ${rows.length} omappade rader`;
  showUnmappedButton.hidden = false;
}
importEventsButton.addEventListener('click', async () => {
  importEventsButton.disabled = true;
  showUnmappedButton.hidden = true;
  importEventsStatus.textContent = 'Importerar aktiehändelser…';
  try {
    const selectedFile = importEventsFile.files && importEventsFile.files[0];
    const request = {method: 'POST'};
    if (selectedFile) {
      request.headers = {'Content-Type': 'application/octet-stream'};
      request.body = await selectedFile.arrayBuffer();
    }
    const response = await fetch('/api/import-events', request);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Importen misslyckades.');
    showUnmappedRows(result.source_headers || [], result.unmapped_rows || []);
    const grouped = new Map();
    (result.markers || []).forEach(marker => {
      const stockIndex = window.stockGraphState.stockTraceByTicker[marker.ticker];
      if (stockIndex === undefined) return;
      const purchaseIndex = purchaseGraph.data.findIndex(trace =>
        trace.meta && trace.meta.role === 'purchase' && trace.meta.ticker === marker.ticker
      );
      if (purchaseIndex < 0) return;
      const base = Number(window.stockGraphState.visibleBases[stockIndex]) || Number(purchaseGraph.data[stockIndex].customdata[0][0]);
      if (!grouped.has(purchaseIndex)) grouped.set(purchaseIndex, {x: [], y: [], customdata: [], symbols: []});
      const points = grouped.get(purchaseIndex);
      points.x.push(marker.date);
      points.y.push(marker.chart_price / base * 100);
      points.customdata.push([marker.chart_price, marker.price, marker.quantity, marker.ticker, marker.currency, marker.commission, marker.transaction_type, marker.sek_fx_rate, marker.ticker_fx_rate, marker.amount_currency]);
      points.symbols.push(marker.transaction_type === 'UTDELNING' ? 'diamond' : (marker.transaction_type === 'KÖP' ? 'circle' : 'triangle-down'));
    });
    for (const [traceIndex, points] of grouped) {
      await Plotly.extendTraces(purchaseGraph, {
        x: [points.x], y: [points.y], customdata: [points.customdata], 'marker.symbol': [points.symbols],
      }, [traceIndex]);
    }
    const tickers = new Set((result.markers || []).map(marker => marker.ticker));
    tickers.forEach(ticker => window.refreshProfitLabel(ticker, false));
    window.renderCustomLegend();
    window.renderPurchaseList();
    window.renderPortfolioSummary();
    window.rebaseVisibleStockGraph({});
    const notShown = Number(result.added || 0) - (result.markers || []).length;
    importEventsStatus.textContent = `${result.added} nya händelser importerade; ${result.duplicates} dubbletter hoppades över.${notShown > 0 ? ` ${notShown} händelser kunde inte visas i diagrammet.` : ''}`;
    if (result.warning_count) importEventsStatus.textContent += ` ${result.warning_count} källrader kunde inte mappas.`;
  } catch (error) {
    importEventsStatus.textContent = error.message;
  } finally {
    importEventsButton.disabled = false;
  }
});
currentPriceButton.addEventListener('click', async () => {
  currentPriceButton.disabled = true;
  refreshStatus.textContent = 'Hämtar senaste tillgängliga intradagskurser…';
  try {
    const response = await fetch('/api/current-prices', {method: 'POST'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Aktuella kurser kunde inte hämtas.');
    const updates = [];
    let latestDate = '';
    Object.entries(result.updates || {}).forEach(([ticker, update]) => {
      const traceIndex = window.stockGraphState.stockTraceByTicker[ticker];
      if (traceIndex === undefined) return;
      const trace = purchaseGraph.data[traceIndex];
      const x = Array.from(trace.x);
      const y = Array.from(trace.y);
      const customdata = Array.from(trace.customdata);
      const date = String(update.date).slice(0, 10);
      const lastDate = String(x[x.length - 1]).slice(0, 10);
      if (!date || date < lastDate) return;
      const base = Number(window.stockGraphState.visibleBases[traceIndex]) || Number(customdata[0][0]);
      const point = [Number(update.price), update.customdata[1], 1.0, update.customdata[3]];
      if (date === lastDate) {
        x[x.length - 1] = date;
        y[y.length - 1] = Number(update.price) / base * 100;
        customdata[customdata.length - 1] = point;
      } else {
        x.push(date);
        y.push(Number(update.price) / base * 100);
        customdata.push(point);
      }
      updates.push({ticker, traceIndex, x, y, customdata});
      if (date > latestDate) latestDate = date;
    });
    if (!updates.length) {
      refreshStatus.textContent = 'Inga aktuella kurser kunde hämtas.';
    } else {
      await Promise.all(updates.map(update => Plotly.restyle(purchaseGraph, {
        x: [update.x], y: [update.y], customdata: [update.customdata],
      }, [update.traceIndex])));
      const axisRange = purchaseGraph._fullLayout.xaxis.range.map(toIsoDate);
      const rangeEnd = [latestDate, axisRange[1], stockholmToday()].sort().at(-1);
      allowedEndDate = rangeEnd > allowedEndDate ? rangeEnd : allowedEndDate;
      if (rangeEnd !== axisRange[1]) {
        const firstDate = purchaseGraph.data
          .filter(trace => trace.meta && trace.meta.role === 'stock')
          .reduce((first, trace) => String(trace.x[0]).slice(0, 10) < first ? String(trace.x[0]).slice(0, 10) : first, '9999-12-31');
        await Plotly.relayout(purchaseGraph, {
          'xaxis.range': [axisRange[0], rangeEnd],
          'xaxis.rangeslider.range': [firstDate, rangeEnd],
        });
      }
      updates.forEach(update => window.currentPriceTickers.add(update.ticker));
      updates.forEach(update => window.refreshProfitLabel(update.ticker, false));
      window.renderCustomLegend();
      window.renderPortfolioSummary();
      window.rebaseVisibleStockGraph({});
      if (window.syncVisibleDateInputs) window.syncVisibleDateInputs();
      refreshStatus.textContent = `Aktuella kurser uppdaterade för ${updates.length} aktier t.o.m. ${latestDate}.`;
    }
    if (result.warnings && result.warnings.length) refreshStatus.textContent += ` ${result.warnings.join(' ')}`;
  } catch (error) {
    refreshStatus.textContent = error.message;
  } finally {
    currentPriceButton.disabled = false;
  }
});
refreshDataButton.addEventListener('click', async () => {
  refreshDataButton.disabled = true;
  refreshDataStatus.textContent = 'Hämtar slutkurser från Yahoo Finance…';
  try {
    const response = await fetch('/api/refresh', {method: 'POST'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Kursdata kunde inte hämtas.');
    const indices = [];
    const payload = {x: [], y: [], customdata: []};
    const updatedTickers = [];
    let latestDate = '';
    Object.entries(result.updates || {}).forEach(([ticker, update]) => {
      const traceIndex = window.stockGraphState.stockTraceByTicker[ticker];
      if (traceIndex === undefined || !update.x || !update.x.length) return;
      const trace = purchaseGraph.data[traceIndex];
      const lastDate = String(trace.x[trace.x.length - 1]).slice(0, 10);
      const newPoints = update.x.map((date, index) => ({date, price: update.prices[index], customdata: update.customdata[index]}))
        .filter(point => String(point.date).slice(0, 10) > lastDate);
      if (!newPoints.length) return;
      const base = Number(window.stockGraphState.visibleBases[traceIndex]) || Number(trace.customdata[0][0]);
      indices.push(traceIndex);
      payload.x.push(newPoints.map(point => point.date));
      payload.y.push(newPoints.map(point => Number(point.price) / base * 100));
      payload.customdata.push(newPoints.map(point => point.customdata));
      updatedTickers.push(ticker);
      latestDate = newPoints.reduce((latest, point) => String(point.date).slice(0, 10) > latest ? String(point.date).slice(0, 10) : latest, latestDate);
    });
    if (indices.length) {
      await Plotly.extendTraces(purchaseGraph, payload, indices);
      const allStockTraces = purchaseGraph.data.filter(trace => trace.meta && trace.meta.role === 'stock');
      const firstDate = allStockTraces.reduce((first, trace) => String(trace.x[0]).slice(0, 10) < first ? String(trace.x[0]).slice(0, 10) : first, '9999-12-31');
      const lastDate = allStockTraces.reduce((last, trace) => String(trace.x[trace.x.length - 1]).slice(0, 10) > last ? String(trace.x[trace.x.length - 1]).slice(0, 10) : last, '0000-01-01');
      const axisRange = purchaseGraph._fullLayout.xaxis.range.map(toIsoDate);
      allowedEndDate = lastDate > allowedEndDate ? lastDate : allowedEndDate;
      await Plotly.relayout(purchaseGraph, {
        'xaxis.range': [axisRange[0], lastDate],
        'xaxis.rangeslider.range': [firstDate, lastDate],
      });
      updatedTickers.forEach(ticker => window.refreshProfitLabel(ticker, false));
      window.renderCustomLegend();
      window.renderPortfolioSummary();
      window.rebaseVisibleStockGraph({});
      if (window.syncVisibleDateInputs) window.syncVisibleDateInputs();
      refreshDataStatus.textContent = `${indices.length} aktier uppdaterade med slutkurser t.o.m. ${latestDate}.`;
    } else {
      refreshDataStatus.textContent = 'Inga senare slutkurser tillgängliga ännu.';
    }
    if (result.warnings && result.warnings.length) {
      refreshDataStatus.textContent += ` ${result.warnings.join(' ')}`;
    }
  } catch (error) {
    refreshDataStatus.textContent = error.message;
  } finally {
    refreshDataButton.disabled = false;
  }
});
purchaseForm.addEventListener('submit', async event => {
  event.preventDefault();
  const purchase = {
    transaction_type: document.getElementById('purchase-type').value,
    ticker: document.getElementById('purchase-ticker').value,
    date: document.getElementById('purchase-date').value,
    quantity: document.getElementById('purchase-quantity').value.trim(),
    price: document.getElementById('purchase-price').value.trim(),
    commission: document.getElementById('purchase-commission').value.trim(),
  };
  purchaseStatus.textContent = 'Sparar aktiehändelse…';
  try {
    const response = await fetch('/api/purchases', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(purchase),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Aktiehändelsen kunde inte sparas.');
    const markerIndex = purchaseGraph.data.findIndex(trace =>
      trace.meta && trace.meta.role === 'purchase' && trace.meta.ticker === result.ticker
    );
    const stockIndex = purchaseGraph.data.findIndex(trace =>
      trace.meta && trace.meta.role === 'stock' && trace.meta.ticker === result.ticker
    );
    // Nya köp ska synas direkt även om aktien var dold i diagrammet.
    await Plotly.restyle(purchaseGraph, {visible: true}, [stockIndex, markerIndex]);
    const base = window.stockGraphState.visibleBases[stockIndex] || result.chart_price;
    await Plotly.extendTraces(purchaseGraph, {
      x: [[result.date]],
      y: [[result.chart_price / base * 100]],
      customdata: [[[result.chart_price, result.price, result.quantity, result.ticker, result.currency, result.commission, result.transaction_type, result.sek_fx_rate, result.ticker_fx_rate]]],
      'marker.symbol': [[result.transaction_type === 'UTDELNING' ? 'diamond' : (result.transaction_type === 'KÖP' ? 'circle' : 'triangle-down')]],
    }, [markerIndex]);
    if (window.refreshProfitLabel) window.refreshProfitLabel(result.ticker);
    window.renderPurchaseList();
    window.renderPortfolioSummary();
    purchaseStatus.textContent = result.transaction_type === 'UTDELNING'
      ? `${result.name}: utdelning ${result.date}, ${result.quantity} st à ${result.price} ${result.currency}, minus ${result.commission} ${result.currency} källskatt. Sparad i aktiekop.csv.`
      : `${result.name}: ${result.transaction_type.toLowerCase()} ${result.quantity} st ${result.date} för ${result.price} ${result.currency} + ${result.commission} ${result.currency} courtage. Sparat i aktiekop.csv.`;
    document.getElementById('purchase-quantity').value = '';
    document.getElementById('purchase-price').value = '';
    document.getElementById('purchase-commission').value = '0,00';
    window.updatePurchaseTotal();
  } catch (error) {
    purchaseStatus.textContent = error.message;
  }
});
</script>
"""
    return (panel.replace("__OPTIONS__", options)
            .replace("__TODAY__", date.today().isoformat())
            .replace("__VERSION_SUFFIX__", version_suffix))


def run_local_chart_server(
    chart_html: str,
    stocks: list[dict[str, str | bool]],
    price_series_by_ticker: dict[str, pd.Series],
    fx_rates_by_currency: dict[str, pd.Series],
) -> None:
    """Servera diagrammet lokalt så köpformuläret kan spara CSV-filen."""
    available_tickers = set(price_series_by_ticker)
    options_panel = make_purchase_panel(stocks, available_tickers, app_version_suffix())
    page = chart_html.replace("</body>", options_panel + "</body>")
    stock_visibility_lock = Lock()
    view_range_lock = Lock()

    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path == "/api/import-events":
                try:
                    source_bytes = None
                    if self.headers.get("Content-Type", "").startswith("application/octet-stream"):
                        payload_length = int(self.headers.get("Content-Length", "0"))
                        if payload_length <= 0 or payload_length > 20 * 1024 * 1024:
                            raise ValueError("Välj en CSV-fil som är större än 0 byte och mindre än 20 MB.")
                        source_bytes = self.rfile.read(payload_length)
                    prepared = prepare_import(source_bytes=source_bytes)
                    existing = prepared["existing"]
                    new_rows = prepared["new_rows"]
                    if new_rows:
                        write_rows(existing + new_rows)
                    markers = []
                    for row in new_rows:
                        ticker = row["ticker"]
                        series = price_series_by_ticker.get(ticker)
                        if series is None:
                            continue
                        event_date = pd.Timestamp(row["date"])
                        earlier_prices = series.loc[series.index <= event_date]
                        if earlier_prices.empty:
                            continue
                        amount_currency = row.get("amount_currency") or ""
                        markers.append({
                            "ticker": ticker,
                            "date": event_date.strftime("%Y-%m-%d"),
                            "chart_price": float(earlier_prices.iloc[-1]),
                            "price": row["price"],
                            "quantity": row["quantity"],
                            "currency": amount_currency or currency_for_ticker(ticker),
                            "commission": row["commission"],
                            "transaction_type": row["transaction_type"],
                            "amount_currency": amount_currency,
                            "sek_fx_rate": fx_rate_for_currency(
                                amount_currency or fx_currency_for_ticker(ticker),
                                event_date,
                                fx_rates_by_currency,
                            ),
                            "ticker_fx_rate": fx_rate_to_sek(ticker, event_date, fx_rates_by_currency),
                        })
                    self.send_json(200, {
                        "added": len(new_rows),
                        "duplicates": prepared["duplicates"],
                        "warning_count": len(prepared["warnings"]),
                        "source_headers": prepared["source_headers"],
                        "unmapped_rows": prepared["unmapped_rows"],
                        "markers": markers,
                    })
                except Exception as error:
                    self.send_json(500, {"error": f"Kunde inte importera aktiehändelser: {error}"})
                return
            if self.path == "/api/stock-visibility":
                try:
                    payload_length = int(self.headers.get("Content-Length", "0"))
                    request_payload = json.loads(self.rfile.read(payload_length)) if payload_length else {}
                    ticker = str(request_payload.get("ticker") or "").strip()
                    visible = request_payload.get("visible")
                    if not ticker or not isinstance(visible, bool):
                        self.send_json(400, {"error": "Ticker och synlighetsvärde krävs."})
                        return
                    with stock_visibility_lock:
                        stock = next((item for item in stocks if item["ticker"] == ticker), None)
                        if stock is None:
                            self.send_json(404, {"error": "Aktien finns inte i appens lista."})
                            return
                        stock["show_initially"] = visible
                        with STOCK_LIST_FILE.open("w", encoding="utf-8", newline="") as file:
                            writer = csv.DictWriter(file, fieldnames=["ticker", "name", "show_initially"])
                            writer.writeheader()
                            for item in stocks:
                                writer.writerow({
                                    "ticker": item["ticker"],
                                    "name": item["name"],
                                    "show_initially": "true" if item["show_initially"] else "false",
                                })
                    self.send_json(200, {"ticker": ticker, "show_initially": visible})
                except Exception as error:
                    self.send_json(500, {"error": f"Kunde inte spara aktiens synlighet: {error}"})
                return
            if self.path == "/api/view-range":
                try:
                    payload_length = int(self.headers.get("Content-Length", "0"))
                    request_payload = json.loads(self.rfile.read(payload_length)) if payload_length else {}
                    start_text = str(request_payload.get("start") or "")
                    end_text = str(request_payload.get("end") or "")
                    start = date.fromisoformat(start_text)
                    end = date.fromisoformat(end_text)
                    if start.isoformat() != start_text or end.isoformat() != end_text or start >= end:
                        self.send_json(400, {"error": "Ange ett giltigt datumintervall i formatet ÅÅÅÅ-MM-DD."})
                        return
                    with view_range_lock:
                        temporary_file = VIEW_RANGE_FILE.with_suffix(".json.tmp")
                        temporary_file.write_text(
                            json.dumps({"start": start_text, "end": end_text}, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        temporary_file.replace(VIEW_RANGE_FILE)
                    self.send_json(200, {"start": start_text, "end": end_text})
                except (ValueError, TypeError, json.JSONDecodeError) as error:
                    self.send_json(400, {"error": f"Kunde inte spara datumintervallet: {error}"})
                except Exception as error:
                    self.send_json(500, {"error": f"Kunde inte spara datumintervallet: {error}"})
                return
            if self.path == "/api/current-prices":
                try:
                    warnings: list[str] = []
                    payload_length = int(self.headers.get("Content-Length", "0"))
                    request_payload = json.loads(self.rfile.read(payload_length)) if payload_length else {}
                    requested_ticker = request_payload.get("ticker")
                    selected_stocks = [stock for stock in stocks if not requested_ticker or stock["ticker"] == requested_ticker]
                    if requested_ticker and not selected_stocks:
                        self.send_json(400, {"error": "Aktien finns inte i appens lista."})
                        return
                    needed_currencies = {
                        "USD" if stock["ticker"] == "BRK-B" else fx_currency_for_ticker(stock["ticker"])
                        for stock in selected_stocks
                    }
                    fx_symbols = {currency: symbol for currency, symbol in FX_TICKERS.items() if currency in needed_currencies}
                    symbols = [stock["ticker"] for stock in selected_stocks]

                    def fetch_intraday(symbol: str):
                        history = yf.Ticker(symbol).history(
                            period="1d", interval="1m", auto_adjust=True,
                        )
                        if history.empty or "Close" not in history:
                            return symbol, None
                        close = history["Close"].dropna()
                        if isinstance(close, pd.DataFrame):
                            close = close.iloc[:, 0].dropna()
                        return symbol, close if not close.empty else None

                    intraday: dict[str, pd.Series] = {}
                    with ThreadPoolExecutor(max_workers=8) as executor:
                        futures = [executor.submit(fetch_intraday, symbol) for symbol in [*symbols, *fx_symbols.values()]]
                        for future in as_completed(futures):
                            try:
                                symbol, close = future.result()
                                if close is not None:
                                    intraday[symbol] = close
                            except Exception as error:
                                warnings.append(f"En kurs kunde inte hämtas ({error}).")

                    current_fx: dict[str, float] = {}
                    for currency, symbol in fx_symbols.items():
                        close = intraday.get(symbol)
                        if close is not None and len(close):
                            current_fx[currency] = float(close.iloc[-1])
                        else:
                            previous = fx_rates_by_currency.get(currency)
                            if previous is not None and not previous.empty:
                                current_fx[currency] = float(previous.iloc[-1])

                    updates: dict[str, dict[str, object]] = {}
                    for stock in selected_stocks:
                        ticker = stock["ticker"]
                        close = intraday.get(ticker)
                        if close is None or close.empty:
                            warnings.append(f"{stock['name']}: ingen aktuell kurs hittades.")
                            continue
                        timestamp = pd.Timestamp(close.index[-1])
                        price = float(close.iloc[-1])
                        if ticker == "BRK-B":
                            rate = current_fx.get("USD")
                            if rate is None:
                                warnings.append("Berkshire kunde inte räknas om till SEK; USD/SEK saknas.")
                                continue
                            price *= rate
                        currency = currency_for_ticker(ticker)
                        fx_rate = fx_rate_for_currency(fx_currency_for_ticker(ticker), timestamp, fx_rates_by_currency)
                        if ticker in US_MARKET_TICKERS - {"BRK-B"}:
                            fx_rate = current_fx.get("USD", fx_rate)
                        elif ticker == "RHM.DE":
                            fx_rate = current_fx.get("EUR", fx_rate)
                        if fx_rate is None:
                            fx_rate = 1.0 if fx_currency_for_ticker(ticker) == "SEK" else None
                        display = f"{price:,.2f}".replace(",", " ").replace(".", ",") + (f" {currency}" if currency else "")
                        updates[ticker] = {
                            "date": timestamp.strftime("%Y-%m-%d"),
                            "price": price,
                            "customdata": [price, display, 1.0, fx_rate],
                        }
                    if not updates and not warnings:
                        raise RuntimeError("Yahoo Finance returnerade inga aktuella kurser.")
                    self.send_json(200, {"updates": updates, "warnings": warnings})
                except Exception as error:
                    self.send_json(500, {"error": f"Kunde inte hämta aktuella kurser: {error}"})
                return
            if self.path == "/api/refresh":
                try:
                    updates: dict[str, dict[str, list]] = {}
                    warnings: list[str] = []
                    for currency, fx_ticker in FX_TICKERS.items():
                        existing_fx = fx_rates_by_currency.get(currency)
                        if existing_fx is None or existing_fx.empty:
                            continue
                        ref_ticker = "NDAQ" if currency == "USD" else "RHM.DE"
                        fx_start = pd.Timestamp(existing_fx.index[-1]).date().isoformat()
                        fx_end = (latest_closed_market_date(ref_ticker) + timedelta(days=1)).isoformat()
                        fx_data = yf.download(fx_ticker, start=fx_start, end=fx_end, auto_adjust=True, progress=False)
                        if fx_data.empty or "Close" not in fx_data:
                            continue
                        rates = fx_data["Close"]
                        if isinstance(rates, pd.DataFrame): rates = rates.iloc[:, 0]
                        if getattr(rates.index, "tz", None) is not None: rates.index = rates.index.tz_localize(None)
                        rates.index = pd.to_datetime(rates.index).normalize()
                        rates = rates.dropna().sort_index()
                        combined_rates = pd.concat([existing_fx, rates]).sort_index()
                        fx_rates_by_currency[currency] = combined_rates[~combined_rates.index.duplicated(keep="last")]

                    for stock in stocks:
                        ticker = stock["ticker"]
                        existing = price_series_by_ticker.get(ticker)
                        if existing is None or existing.empty:
                            continue
                        try:
                            if getattr(existing.index, "tz", None) is not None:
                                existing.index = existing.index.tz_localize(None)
                            existing.index = pd.to_datetime(existing.index).normalize()
                            start = pd.Timestamp(existing.index[-1]).date().isoformat()
                            latest_closed_date = latest_closed_market_date(ticker)
                            end = (latest_closed_date + timedelta(days=1)).isoformat()
                            data = yf.download(
                                ticker, start=start, end=end,
                                # Use unadjusted close prices so the graph and
                                # transaction values match quoted market prices.
                                auto_adjust=False, progress=False,
                            )
                            if data.empty or "Close" not in data:
                                continue
                            close = data["Close"]
                            if isinstance(close, pd.DataFrame):
                                close = close.iloc[:, 0]
                            if getattr(close.index, "tz", None) is not None:
                                close.index = close.index.tz_localize(None)
                            close.index = pd.to_datetime(close.index).normalize()
                            close = close.dropna().sort_index()
                            if ticker == "BRK-B":
                                usdsek_close = fx_rates_by_currency.get("USD")
                                if usdsek_close is None:
                                    warnings.append("USD/SEK-data saknades; Berkshire uppdaterades inte.")
                                    continue
                                fx_rates = usdsek_close.reindex(close.index, method="ffill")
                                close = (close * fx_rates).dropna()
                            close = close.loc[
                                (close.index.date <= latest_closed_date)
                                & (close.index.date > existing.index[-1].date())
                            ]
                            if close.empty:
                                continue
                            combined = pd.concat([existing, close]).sort_index()
                            combined = combined[~combined.index.duplicated(keep="last")]
                            price_series_by_ticker[ticker] = combined
                            currency = currency_for_ticker(ticker)
                            updates[ticker] = {
                                "x": [index.strftime("%Y-%m-%d") for index in close.index],
                                "prices": [float(value) for value in close.values],
                                "customdata": [
                                    [
                                        float(value),
                                        f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
                                        + (f" {currency}" if currency else ""),
                                        1.0,
                                        fx_rate_to_sek(ticker, index, fx_rates_by_currency),
                                    ]
                                    for index, value in close.items()
                                ],
                            }
                        except Exception as error:
                            warnings.append(f"{ticker}: kursuppdateringen misslyckades ({error}).")
                    self.send_json(200, {"updates": updates, "warnings": warnings})
                except Exception as error:
                    self.send_json(500, {"error": f"Kunde inte hämta slutkurser: {error}"})
                return
            if self.path != "/api/purchases":
                self.send_error(404)
                return
            try:
                payload_length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(payload_length))
                ticker = str(payload.get("ticker", "")).strip()
                transaction_type = str(payload.get("transaction_type", "KÖP")).strip().upper()
                purchase_date = date.fromisoformat(str(payload.get("date", "")))
                quantity_text = str(payload.get("quantity", "")).strip()
                price_text = str(payload.get("price", "")).strip()
                commission_text = str(payload.get("commission", "0")).strip() or "0"
                quantity = Decimal("".join(quantity_text.split()).replace(",", "."))
                price = Decimal("".join(price_text.split()).replace(",", "."))
                commission = Decimal("".join(commission_text.split()).replace(",", "."))
                if ticker not in price_series_by_ticker:
                    raise ValueError("Aktien saknar hämtad kursdata.")
                if transaction_type not in {"KÖP", "SÄLJ", "UTDELNING"}:
                    raise ValueError("Transaktionstyp måste vara köp, sälj eller utdelning.")
                if not quantity.is_finite() or quantity <= 0:
                    raise ValueError("Antalet måste vara större än noll.")
                if not price.is_finite() or price <= 0:
                    raise ValueError("Kursen eller utdelningsbeloppet måste vara större än noll.")
                price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                price_text = format(price, ".2f")
                if not commission.is_finite() or commission < 0:
                    raise ValueError("Courtage eller källskatt måste vara noll eller större.")
                commission = commission.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                commission_text = format(commission, ".2f")
                series = price_series_by_ticker[ticker]
                earlier_prices = series.loc[series.index <= pd.Timestamp(purchase_date)]
                if earlier_prices.empty:
                    raise ValueError("Det finns ingen kursdata före köpdatumet.")

                entry = {
                    "date": purchase_date.isoformat(),
                    "transaction_type": transaction_type,
                    "ticker": ticker,
                    "quantity": quantity_text,
                    # Spara användarens inmatning, inte kursen hämtad till diagrammet.
                    "price": price_text,
                    "commission": commission_text,
                }
                if PURCHASES_FILE.exists() and PURCHASES_FILE.stat().st_size:
                    with PURCHASES_FILE.open("r", encoding="utf-8-sig", newline="") as file:
                        existing_header = next(csv.reader(file, delimiter=";"), [])
                    if "commission" not in existing_header or "transaction_type" not in existing_header or "source_id" not in existing_header or "amount_currency" not in existing_header:
                        previous_purchases = load_purchases()
                        with PURCHASES_FILE.open("w", encoding="utf-8", newline="") as file:
                            writer = csv.DictWriter(file, fieldnames=["date", "transaction_type", "ticker", "quantity", "price", "commission", "source_id", "amount_currency"], delimiter=";")
                            writer.writeheader()
                            for previous in previous_purchases:
                                writer.writerow({**previous, "transaction_type": previous.get("transaction_type") or "KÖP", "commission": previous.get("commission") or "0", "source_id": previous.get("source_id") or "", "amount_currency": previous.get("amount_currency") or ""})
                needs_header = not PURCHASES_FILE.exists() or PURCHASES_FILE.stat().st_size == 0
                with PURCHASES_FILE.open("a", encoding="utf-8", newline="") as file:
                    writer = csv.DictWriter(
                        file,
                        fieldnames=["date", "transaction_type", "ticker", "quantity", "price", "commission", "source_id", "amount_currency"],
                        delimiter=";",
                    )
                    if needs_header:
                        writer.writeheader()
                    writer.writerow(entry)

                stock_name = next(stock["name"] for stock in stocks if stock["ticker"] == ticker)
                result = {
                    **entry,
                    "name": stock_name,
                    "currency": currency_for_ticker(ticker),
                    "chart_price": float(earlier_prices.iloc[-1]),
                    "commission": commission_text,
                    "sek_fx_rate": fx_rate_to_sek(ticker, purchase_date, fx_rates_by_currency),
                    "ticker_fx_rate": fx_rate_to_sek(ticker, purchase_date, fx_rates_by_currency),
                    "amount_currency": "",
                }
                self.send_json(200, result)
            except (ValueError, InvalidOperation, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error) or "Ogiltiga köpdata."})
            except Exception as error:
                self.send_json(500, {"error": f"Kunde inte spara köpet: {error}"})

        def send_json(self, status: int, value: dict[str, object]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    # En kopia kan också öppnas direkt som HTML, men köpformuläret fungerar via servern.
    Path(__file__).with_name("aktier_graf.html").write_text(page, encoding="utf-8")
    print(f"Diagrammet öppnas på {url}. Stäng servern med Ctrl+C.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDiagramservern stoppas.")
    finally:
        server.server_close()


def main() -> None:
    prices = {}
    stocks = load_stocks()
    purchases = load_purchases()
    price_series_by_ticker = {}
    fx_rates_by_currency: dict[str, pd.Series] = {}
    required_fx = {fx_currency_for_ticker(stock["ticker"]) for stock in stocks} - {"SEK"}
    for currency in required_fx:
        ref_ticker = "NDAQ" if currency == "USD" else "RHM.DE"
        fx_end = (latest_closed_market_date(ref_ticker) + timedelta(days=1)).isoformat()
        fx_data = yf.download(FX_TICKERS[currency], start=START_DATE, end=fx_end, auto_adjust=True, progress=False)
        if not fx_data.empty and "Close" in fx_data:
            rates = fx_data["Close"]
            if isinstance(rates, pd.DataFrame): rates = rates.iloc[:, 0]
            if getattr(rates.index, "tz", None) is not None: rates.index = rates.index.tz_localize(None)
            rates.index = pd.to_datetime(rates.index).normalize()
            fx_rates_by_currency[currency] = rates.dropna().sort_index()

    for stock in stocks:
        ticker = stock["ticker"]
        label = stock["name"]
        end = (latest_closed_market_date(ticker) + timedelta(days=1)).isoformat()
        data = yf.download(
            ticker,
            start=START_DATE,
            end=end,
            # Use actual close prices; dividend-adjusted prices distort the
            # quoted share price before an ex-dividend date.
            auto_adjust=False,
            progress=False,
        )

        if data.empty or "Close" not in data:
            print(f"Kunde inte hämta kursdata för {ticker}; serien hoppas över.")
            continue

        close = data["Close"]
        # Hantera även kolumnformatet MultiIndex som vissa yfinance-versioner ger.
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]

        close = close.dropna()
        if close.empty or close.iloc[0] == 0:
            print(f"Saknar användbara kurser för {ticker}; serien hoppas över.")
            continue

        if ticker == "BRK-B":
            usdsek_close = fx_rates_by_currency.get("USD")
            if usdsek_close is None:
                print("Kunde inte hämta USD/SEK-data för Berkshire; serien hoppas över.")
                continue
            fx_rates = usdsek_close.reindex(close.index, method="ffill")
            close = close * fx_rates
            close = close.dropna()
            if close.empty:
                print("Saknar användbara USD/SEK-kurser för Berkshire; serien hoppas över.")
                continue

        prices[label] = close
        price_series_by_ticker[ticker] = close

    if not prices:
        raise RuntimeError("Ingen kursdata kunde hämtas. Kontrollera internetanslutningen.")

    comparison = pd.DataFrame(prices).sort_index()
    figure = go.Figure()
    stock_colors = {}

    for stock in stocks:
        label = stock["name"]
        if label not in comparison.columns:
            continue
        series = comparison[label].dropna()
        color = PLOT_COLORS[len(stock_colors) % len(PLOT_COLORS)]
        stock_colors[stock["ticker"]] = color
        currency = currency_for_ticker(stock["ticker"])
        figure.add_trace(
            go.Scatter(
                x=series.index,
                y=(series / series.iloc[0] * 100).values,
                # Vanliga listor behövs här: Plotly kan annars serialisera
                # NumPy-arrayer som base64-objekt som callbacken inte kan läsa.
                customdata=[
                    [
                        float(value),
                        f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
                        + (f" {currency}" if currency else ""),
                        1.0,
                        fx_rate_to_sek(stock["ticker"], index, fx_rates_by_currency),
                    ]
                    for index, value in series.items()
                ],
                mode="lines",
                cliponaxis=False,
                name=label,
                meta={"role": "stock", "ticker": stock["ticker"], "displayName": label},
                legendgroup=stock["ticker"],
                visible=True if stock["show_initially"] else "legendonly",
                line={"width": 2, "color": color},
                hovertemplate=(
                    f"{label}: %{{customdata[1]}}<br>Index: %{{y:.1f}}"
                    "<extra></extra>"
                ),
            )
        )

    purchases_by_ticker = {ticker: [] for ticker in price_series_by_ticker}
    for purchase in purchases:
        ticker = (purchase.get("ticker") or "").strip()
        series = price_series_by_ticker.get(ticker)
        if series is None:
            continue
        try:
            purchase_date = pd.Timestamp(purchase["date"])
            purchase_price = Decimal(purchase["price"].replace(",", "."))
            quantity = Decimal(purchase["quantity"].replace(",", "."))
            commission = Decimal((purchase.get("commission") or "0").replace(",", "."))
        except (KeyError, InvalidOperation, ValueError):
            continue
        earlier_prices = series.loc[series.index <= purchase_date]
        is_dividend = (purchase.get("transaction_type") or "KÖP").upper() == "UTDELNING"
        if not earlier_prices.empty and (purchase_price != 0 if is_dividend else purchase_price > 0) and quantity > 0:
            purchases_by_ticker[ticker].append({
                "date": purchase_date.strftime("%Y-%m-%d"),
                "chart_price": float(earlier_prices.iloc[-1]),
                "price": str(purchase["price"]),
                "quantity": str(purchase["quantity"]),
                "commission": str(commission),
                "transaction_type": (purchase.get("transaction_type") or "KÖP").upper(),
                "currency": purchase.get("amount_currency") or currency_for_ticker(ticker),
                "amount_currency": purchase.get("amount_currency") or "",
                "sek_fx_rate": fx_rate_for_currency(
                    purchase.get("amount_currency") or fx_currency_for_ticker(ticker),
                    purchase_date,
                    fx_rates_by_currency,
                ),
                "ticker_fx_rate": fx_rate_to_sek(ticker, purchase_date, fx_rates_by_currency),
            })

    for stock in stocks:
        ticker = stock["ticker"]
        series = price_series_by_ticker.get(ticker)
        if series is None:
            continue
        markers = purchases_by_ticker[ticker]
        figure.add_trace(
            go.Scatter(
                x=[purchase["date"] for purchase in markers],
                xaxis="x2",
                y=[purchase["chart_price"] / series.iloc[0] * 100 for purchase in markers],
                customdata=[
                    [
                        purchase["chart_price"], purchase["price"],
                        purchase["quantity"], ticker, purchase["currency"], purchase["commission"], purchase.get("transaction_type", "KÖP"), purchase.get("sek_fx_rate"), purchase.get("ticker_fx_rate"), purchase.get("amount_currency"),
                    ]
                    for purchase in markers
                ],
                mode="markers",
                name=f"{stock['name']} köp",
                meta={"role": "purchase", "ticker": ticker},
                legendgroup=ticker,
                showlegend=False,
                visible=True if stock["show_initially"] else "legendonly",
                marker={
                    "symbol": ["diamond" if purchase.get("transaction_type") == "UTDELNING" else ("circle" if purchase.get("transaction_type", "KÖP") == "KÖP" else "triangle-down") for purchase in markers],
                    "size": 10,
                    "color": stock_colors[ticker],
                    "line": {"color": "#333333", "width": 1.5},
                },
                hovertemplate=(
                    f"{stock['name']} %{{customdata[6]}} · "
                    "%{customdata[1]} %{customdata[4]}<extra></extra>"
                ),
            )
        )

    # Sätt vinst/förlust direkt i legendnamnet redan när figuren byggs.
    # JavaScript uppdaterar därefter namnet när nya transaktioner registreras.
    stock_traces = {
        trace.meta["ticker"]: trace
        for trace in figure.data
        if trace.meta and trace.meta.get("role") == "stock"
    }
    for ticker, transactions in purchases_by_ticker.items():
        stock_trace = stock_traces.get(ticker)
        series = price_series_by_ticker.get(ticker)
        if stock_trace is None or series is None:
            continue
        invested = 0.0
        profit = 0.0
        held_quantity = 0.0
        capital_months = 0.0
        for transaction in transactions:
            transaction_quantity = float(transaction["quantity"].replace(",", "."))
            price = float(transaction["price"].replace(",", "."))
            commission = float(transaction["commission"].replace(",", "."))
            quantity = quantity_in_current_shares(
                ticker, transaction["date"], transaction_quantity
            )
            if transaction["transaction_type"] == "SÄLJ":
                held_quantity -= quantity
                profit += price * transaction_quantity - commission
            elif transaction["transaction_type"] == "UTDELNING":
                amount_currency = transaction.get("amount_currency") or fx_currency_for_ticker(ticker)
                amount_fx = fx_rate_for_currency(amount_currency, transaction["date"], fx_rates_by_currency)
                ticker_fx = fx_rate_to_sek(ticker, transaction["date"], fx_rates_by_currency)
                if amount_fx is not None and ticker_fx:
                    profit += price * transaction_quantity * amount_fx / ticker_fx
            else:
                cost = price * transaction_quantity + commission
                invested += cost
                held_quantity += quantity
                profit -= cost
                months = max(
                    0.0,
                    (series.index[-1] - pd.Timestamp(transaction["date"])).total_seconds()
                    / (86400 * 30.4375),
                )
                capital_months += cost * months
        if invested <= 0:
            continue
        profit += float(series.iloc[-1]) * held_quantity
        currency = currency_for_ticker(ticker)
        monthly_profit = profit * invested / capital_months if capital_months > 0 else None
        monthly_rate = monthly_profit / invested * 100 if monthly_profit is not None else None

        def format_amount(value: float) -> str:
            return f"{value:+,.2f}".replace(",", " ").replace(".", ",")

        monthly_text = (
            f"{monthly_profit:+,.2f} {currency}/mån · {monthly_rate:+.2f}%/mån"
            .replace(",", " ").replace(".", ",")
            if monthly_profit is not None and monthly_rate is not None
            else "—/mån"
        )
        stock_trace.meta["profitLabel"] = (
            f"{format_amount(profit)} {currency} · {monthly_text}"
        )
        stock_trace.name = f"{stock_trace.meta['displayName']}   {stock_trace.meta['profitLabel']}"

    figure.update_layout(
        title="",
        xaxis_title="Datum",
        yaxis_title="Index (100 = synligt startdatum)",
        hovermode="x unified",
        hoverdistance=50,
        spikedistance=50,
        clickmode="event",
        showlegend=False,
        margin={"r": 145},
    )
    visible_start, visible_end = load_visible_date_range(comparison.index.min(), comparison.index.max())
    figure.update_xaxes(
        range=[visible_start, visible_end],
        hoverformat="%Y-%m-%d",
        rangeslider={
            "visible": True,
            "range": [comparison.index.min(), comparison.index.max()],
            "thickness": 0.12,
        },
    )
    # Håll köp/sälj-symbolerna synkroniserade med huvudaxeln men utanför
    # rangesliderns nedre översiktsdiagram.
    figure.update_layout(xaxis2={"overlaying": "x", "matches": "x", "visible": False, "showticklabels": False})

    # Räkna om varje kurva mot dess första datapunkt i det synliga intervallet.
    # Det gör att index 100 följer datumreglagets vänsterkant.
    post_script = r"""
const graph = document.getElementById('{plot_id}');
let rebaseTimer;
let firstComparisonPoint = null;
const comparisons = [];
let endpointLabels = [];
const stockTraceByTicker = {};
let highlightedTicker = null;
graph.data.forEach((trace, index) => {
  if (trace.meta && trace.meta.role === 'stock') stockTraceByTicker[trace.meta.ticker] = index;
});
const visibleBases = graph.data.map(trace => {
  if (trace.meta && trace.meta.role === 'purchase') return null;
  const firstRow = trace.customdata && trace.customdata[0];
  return Number(Array.isArray(firstRow) ? firstRow[0] : firstRow) || null;
});
window.stockGraphState = {visibleBases, stockTraceByTicker};
function clearLineHighlight() {
  if (!highlightedTicker) return;
  const index = stockTraceByTicker[highlightedTicker];
  highlightedTicker = null;
  if (index !== undefined) Plotly.restyle(graph, {'line.width': 2}, [index]);
}
function setLineHighlight(ticker, highlighted) {
  const index = stockTraceByTicker[ticker];
  if (index === undefined) return;
  if (highlighted) {
    if (highlightedTicker && highlightedTicker !== ticker) clearLineHighlight();
    highlightedTicker = ticker;
    Plotly.restyle(graph, {'line.width': 5}, [index]);
  } else if (highlightedTicker === ticker) {
    highlightedTicker = null;
    Plotly.restyle(graph, {'line.width': 2}, [index]);
  }
}
const traceIsVisible = graph.data.map(
  trace => trace.visible !== 'legendonly' && trace.visible !== false
);
let soloTicker = null;
let soloVisibilitySnapshot = null;
window.getSoloTicker = () => soloTicker;
window.getVisibleTickers = () => Object.entries(stockTraceByTicker)
  .filter(([, index]) => graph.data[index].visible !== 'legendonly' && graph.data[index].visible !== false)
  .map(([ticker]) => ticker);
let visibilitySaveQueue = Promise.resolve();
function saveStockVisibility(ticker, visible) {
  visibilitySaveQueue = visibilitySaveQueue.then(async () => {
    const response = await fetch('/api/stock-visibility', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker, visible}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Visningsläget kunde inte sparas.');
  }).catch(error => {
    const status = document.getElementById('refresh-data-status');
    if (status) status.textContent = `Visningsläget kunde inte sparas: ${error.message}`;
  });
  return visibilitySaveQueue;
}
function refreshFilteredTables() {
  if (window.renderPurchaseList) window.renderPurchaseList();
  if (window.renderPortfolioSummary) window.renderPortfolioSummary();
}
function applyVisibility(visibility) {
  const indices = [];
  const values = [];
  graph.data.forEach((trace, index) => {
    const currentlyVisible = trace.visible !== 'legendonly' && trace.visible !== false;
    if (currentlyVisible !== visibility[index]) {
      indices.push(index);
      values.push(visibility[index] ? true : 'legendonly');
    }
  });
  if (!indices.length) {
    window.renderCustomLegend();
    return Promise.resolve();
  }
  return Plotly.restyle(
    graph,
    {visible: values},
    indices
  ).then(() => {
    window.renderCustomLegend();
  });
}
function setSolo(ticker, checked) {
  if (checked) {
    if (!soloVisibilitySnapshot) {
      soloVisibilitySnapshot = graph.data.map(
        trace => trace.visible !== 'legendonly' && trace.visible !== false
      );
    }
    soloTicker = ticker;
    return applyVisibility(graph.data.map(
      trace => Boolean(trace.meta && trace.meta.ticker === ticker)
    )).then(refreshFilteredTables);
  }
  if (soloTicker === ticker && soloVisibilitySnapshot) {
    const previous = soloVisibilitySnapshot;
    soloTicker = null;
    soloVisibilitySnapshot = null;
    return applyVisibility(previous).then(refreshFilteredTables);
  }
}
function toggleTicker(ticker) {
  const stockIndex = stockTraceByTicker[ticker];
  if (stockIndex === undefined) return;
  const stockVisible = graph.data[stockIndex].visible !== 'legendonly' && graph.data[stockIndex].visible !== false;
  if (soloVisibilitySnapshot) {
    const previous = soloVisibilitySnapshot.slice();
    const next = !previous[stockIndex];
    soloVisibilitySnapshot = null;
    soloTicker = null;
    graph.data.forEach((trace, index) => {
      if (trace.meta && trace.meta.ticker === ticker) previous[index] = next;
    });
    return applyVisibility(previous)
      .then(() => saveStockVisibility(ticker, next))
      .then(refreshFilteredTables);
  }
  const indices = graph.data.map((trace, index) =>
    trace.meta && trace.meta.ticker === ticker ? index : -1
  ).filter(index => index >= 0);
  return Plotly.restyle(
    graph,
    {visible: indices.map(() => stockVisible ? 'legendonly' : true)},
    indices
  ).then(() => saveStockVisibility(ticker, !stockVisible)).then(() => {
    window.renderCustomLegend();
    refreshFilteredTables();
  });
}
window.renderCustomLegend = function() {
  const container = document.getElementById('stock-custom-legend');
  if (!container) return;
  clearLineHighlight();
  container.replaceChildren();
  const header = document.createElement('div');
  header.className = 'stock-legend-header';
  const soloHeading = document.createElement('span');
  soloHeading.textContent = 'Solo';
  const stockHeading = document.createElement('span');
  stockHeading.textContent = 'Aktie / resultat';
  const help = document.createElement('span');
  help.className = 'stock-legend-help';
  help.id = 'stock-legend-help';
  help.textContent = 'Klicka på aktien för av/på. Klicka två punkter på samma kurva för jämförelse.';
  header.append(help, soloHeading, stockHeading);
  container.append(header);
  Object.entries(stockTraceByTicker).forEach(([ticker, index]) => {
    const trace = graph.data[index];
    const row = document.createElement('div');
    const visible = trace.visible !== 'legendonly' && trace.visible !== false;
    row.className = `stock-legend-row${visible ? '' : ' is-hidden'}`;
    const solo = document.createElement('input');
    solo.type = 'checkbox';
    solo.checked = soloTicker === ticker;
    solo.setAttribute('aria-label', `Visa endast ${trace.meta.displayName}`);
    solo.addEventListener('change', () => setSolo(ticker, solo.checked));
    const nameButton = document.createElement('button');
    nameButton.type = 'button';
    nameButton.className = 'stock-legend-name';
    nameButton.addEventListener('click', () => toggleTicker(ticker));
    nameButton.addEventListener('pointerenter', () => setLineHighlight(ticker, true));
    nameButton.addEventListener('pointerleave', () => setLineHighlight(ticker, false));
    nameButton.append(document.createTextNode(trace.meta.displayName));
    if (trace.meta.profitLabel) {
      const profit = document.createElement('span');
      profit.className = 'stock-legend-profit';
      profit.textContent = trace.meta.profitLabel;
      nameButton.append(profit);
    }
    row.append(solo, nameButton);
    container.append(row);
  });
};
window.suggestPurchasePrice = function() {
  const tickerField = document.getElementById('purchase-ticker');
  const dateField = document.getElementById('purchase-date');
  if (!tickerField || !dateField) return;
  if (document.getElementById('purchase-type').value === 'UTDELNING') return;
  const ticker = tickerField.value;
  const selectedDate = dateField.value;
  const traceIndex = stockTraceByTicker[ticker];
  if (!selectedDate || traceIndex === undefined) {
    document.getElementById('purchase-price').value = '';
    if (window.updatePurchaseTotal) window.updatePurchaseTotal();
    return;
  }
  const trace = graph.data[traceIndex];
  let priceIndex = -1;
  trace.x.forEach((value, index) => {
    if (String(value).slice(0, 10) <= selectedDate) priceIndex = index;
  });
  if (priceIndex < 0) {
    document.getElementById('purchase-price').value = '';
    if (window.updatePurchaseTotal) window.updatePurchaseTotal();
    return;
  }
  const row = trace.customdata[priceIndex];
      const rawPrice = Number(Array.isArray(row) ? row[0] : row);
      const fxFactor = Array.isArray(row) ? Number(row[2] || 1) : 1;
      const price = rawPrice * fxFactor;
  if (Number.isFinite(price) && price > 0) {
    document.getElementById('purchase-price').value = price.toLocaleString('sv-SE', {useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (window.updateSuggestedCommission) window.updateSuggestedCommission();
    if (window.updatePurchaseTotal) window.updatePurchaseTotal();
  }
};
const swedishNumber = value => Number(value).toLocaleString('sv-SE', {minimumFractionDigits: 2, maximumFractionDigits: 2});
function refreshProfitLabel(ticker, render = true) {
  const stockIndex = stockTraceByTicker[ticker];
  if (stockIndex === undefined) return;
  const stockTrace = graph.data[stockIndex];
  const purchaseIndex = graph.data.findIndex(trace =>
    trace.meta && trace.meta.role === 'purchase' && trace.meta.ticker === ticker
  );
  const purchaseTrace = purchaseIndex < 0 ? null : graph.data[purchaseIndex];
  const latestRow = stockTrace.customdata[stockTrace.customdata.length - 1];
  const currentRawPrice = Number(Array.isArray(latestRow) ? latestRow[0] : latestRow);
  const currentFxFactor = Array.isArray(latestRow) ? Number(latestRow[2] || 1) : 1;
  const currentPrice = currentRawPrice * currentFxFactor;
  const latestDate = new Date(stockTrace.x[stockTrace.x.length - 1]);
  let invested = 0;
  let profit = 0;
  let heldQuantity = 0;
  let capitalMonths = 0;
  if (purchaseTrace) {
    purchaseTrace.x.forEach((purchaseDate, index) => {
      const details = purchaseTrace.customdata[index];
      const purchasePrice = Number(String(details[1]).replace(/\s/g, '').replace(',', '.'));
      const enteredQuantity = Number(String(details[2]).replace(/\s/g, '').replace(',', '.'));
      const quantity = ticker === 'SIRI' && String(purchaseDate).slice(0, 10) < '2024-09-10'
        ? enteredQuantity / 10
        : enteredQuantity;
      const commission = Number(String(details[5] || '0').replace(/\s/g, '').replace(',', '.'));
      const transactionType = details[6] || 'KÖP';
      if (!(enteredQuantity > 0) || (transactionType === 'UTDELNING' ? purchasePrice === 0 : purchasePrice <= 0)) return;
      const fee = Number.isFinite(commission) ? commission : 0;
      if (transactionType === 'UTDELNING') {
        const amountFx = Number(details[7]);
        const tickerFx = Number(details[8]);
        if (Number.isFinite(amountFx) && amountFx > 0 && Number.isFinite(tickerFx) && tickerFx > 0) {
          profit += (purchasePrice * enteredQuantity - fee) * amountFx / tickerFx;
        }
      } else if (transactionType === 'SÄLJ') {
        heldQuantity -= quantity;
        profit += purchasePrice * enteredQuantity - fee;
      } else {
        const cost = purchasePrice * enteredQuantity + fee;
        invested += cost;
        heldQuantity += quantity;
        profit -= cost;
        const months = Math.max(0, (latestDate - new Date(purchaseDate)) / (86400000 * 30.4375));
        capitalMonths += cost * months;
      }
    });
  }
  if (!(invested > 0)) return;
  profit += currentPrice * heldQuantity;
  const currency = ticker.endsWith('.ST') || ticker === 'BRK-B' ? 'kr'
    : (['NDAQ', 'SIRI', 'RIVN'].includes(ticker) ? 'USD' : (ticker === 'RHM.DE' ? 'EUR' : ''));
  const monthlyProfit = capitalMonths > 0 ? profit * invested / capitalMonths : null;
  const monthlyPercent = monthlyProfit === null ? null : monthlyProfit / invested * 100;
  const signed = value => `${value > 0 ? '+' : ''}${swedishNumber(value)}`;
  const monthText = monthlyProfit === null
    ? '—/mån'
    : `${signed(monthlyProfit)} ${currency}/mån · ${signed(monthlyPercent)}%/mån`;
  const profitText = `${signed(profit)} ${currency}`;
  stockTrace.meta.profitLabel = `${profitText} · ${monthText}`;
  if (render) window.renderCustomLegend();
}
window.refreshProfitLabel = refreshProfitLabel;
function comparisonOverlays(targetYRange) {
  const shapes = [];
  const annotations = [];
  const plotHeight = graph._fullLayout && graph._fullLayout._size.h || 600;
  const yRange = targetYRange || (graph._fullLayout && graph._fullLayout.yaxis.range) || [0, 100];
  const endpointSlots = endpointLabels.map(label => ({
    ...label,
    pixel: (yRange[1] - label.y) / (yRange[1] - yRange[0]) * plotHeight,
  })).sort((a, b) => a.pixel - b.pixel);
  const labelGap = Math.min(17, (plotHeight - 16) / Math.max(1, endpointSlots.length - 1));
  endpointSlots.forEach((label, index) => {
    label.slot = Math.max(label.pixel, index === 0 ? 8 : endpointSlots[index - 1].slot + labelGap);
  });
  if (endpointSlots.length) {
    const overflow = endpointSlots[endpointSlots.length - 1].slot - (plotHeight - 8);
    if (overflow > 0) endpointSlots.forEach(label => { label.slot -= overflow; });
    const underflow = 8 - endpointSlots[0].slot;
    if (underflow > 0) endpointSlots.forEach(label => { label.slot += underflow; });
  }
  endpointSlots.forEach(label => {
    annotations.push({
      x: label.x,
      y: label.y,
      xref: 'x',
      yref: 'y',
      text: label.name,
      showarrow: true,
      arrowhead: 0,
      arrowcolor: '#222',
      arrowwidth: 1,
      ax: 28,
      ay: label.slot - label.pixel,
      xanchor: 'left',
      bgcolor: 'rgba(255,255,255,0.85)',
      borderwidth: 0,
      borderpad: 1,
      font: {color: '#222', size: 11},
      captureevents: false,
    });
  });
  comparisons.forEach(comparison => {
    const base = visibleBases[comparison.curveNumber];
    if (!base) return;
    const y1 = comparison.price1 / base * 100;
    const y2 = comparison.price2 / base * 100;
    const lowY = Math.min(y1, y2);
    const highY = Math.max(y1, y2);
    const color = graph._fullData[comparison.curveNumber].line.color || '#444';
    shapes.push(
      {type: 'line', xref: 'x', yref: 'y', x0: comparison.x1, x1: comparison.x2, y0: y1, y1: y1, line: {color, width: 1.5, dash: 'dot'}},
      {type: 'line', xref: 'x', yref: 'y', x0: comparison.x1, x1: comparison.x2, y0: y2, y1: y2, line: {color, width: 1.5, dash: 'dot'}},
      {type: 'line', xref: 'x', yref: 'y', x0: comparison.x2, x1: comparison.x2, y0: lowY, y1: highY, line: {color, width: 2}},
    );
    annotations.push({
      x: comparison.x2,
      y: (y1 + y2) / 2,
      xref: 'x',
      yref: 'y',
      text: comparison.label,
      showarrow: false,
      xshift: 36,
      bgcolor: 'white',
      bordercolor: color,
      borderwidth: 1,
      font: {color, size: 13},
    });
  });

  if (firstComparisonPoint) {
    const base = visibleBases[firstComparisonPoint.curveNumber];
    if (base) {
      annotations.push({
        x: firstComparisonPoint.x,
        y: firstComparisonPoint.price / base * 100,
        xref: 'x',
        yref: 'y',
        text: `Punkt 1: ${firstComparisonPoint.formattedPrice}<br>Klicka på ${firstComparisonPoint.name} igen`,
        showarrow: true,
        arrowhead: 2,
        ax: 0,
        ay: -38,
        bgcolor: 'white',
        bordercolor: '#555',
        borderwidth: 1,
      });
    }
  }
  return {shapes, annotations};
}
function rebaseToVisibleStart(eventData) {
  let start = eventData && eventData['xaxis.range[0]'];
  let end = eventData && eventData['xaxis.range[1]'];
  if ((!start || !end) && eventData && Array.isArray(eventData['xaxis.range'])) {
    const eventRange = eventData['xaxis.range'];
    start = start || eventRange[0];
    end = end || eventRange[1];
  }
  const activeRange = graph._fullLayout && graph._fullLayout.xaxis.range;
  const layoutRange = graph.layout.xaxis.range;
  const sliderRange = graph.layout.xaxis.rangeslider && graph.layout.xaxis.rangeslider.range;
  const fallbackRange = activeRange || layoutRange || sliderRange;
  if (fallbackRange) {
    start = start || fallbackRange[0];
    end = end || fallbackRange[1];
  }
  if (!start || !end) return;

  const startTime = new Date(start).getTime();
  const endTime = new Date(end).getTime();
  const ys = [];
  const indices = [];
  const visibleValues = [];
  const nextEndpointLabels = [];
  graph.data.forEach((trace, index) => {
    const xs = Array.from(trace.x);
    const rawPrices = Array.from(trace.customdata, row =>
      Number(Array.isArray(row) ? row[0] : row)
    );
    if (trace.meta && trace.meta.role === 'purchase') {
      const stockIndex = stockTraceByTicker[trace.meta.ticker];
      const base = stockIndex === undefined ? null : visibleBases[stockIndex];
      if (!base) return;
      ys.push(rawPrices.map(price => Number(price) / base * 100));
      indices.push(index);
      return;
    }
    const baseIndex = xs.findIndex((x, point) => {
      const time = new Date(x).getTime();
      const price = Number(rawPrices[point]);
      return time >= startTime && time <= endTime && Number.isFinite(price) && price !== 0;
    });
    if (baseIndex < 0) return;
    const base = Number(rawPrices[baseIndex]);
    visibleBases[index] = base;
    ys.push(rawPrices.map(price => Number(price) / base * 100));
    let lastVisibleIndex = -1;
    let lastVisibleValue = null;
    xs.forEach((x, point) => {
      const time = new Date(x).getTime();
      if (time >= startTime && time <= endTime) {
        lastVisibleIndex = point;
        const value = Number(rawPrices[point]) / base * 100;
        lastVisibleValue = value;
        if (Number.isFinite(value) && trace.visible !== 'legendonly' && trace.visible !== false) {
          visibleValues.push(value);
        }
      }
    });
    if (lastVisibleIndex >= 0 && trace.visible !== 'legendonly' && trace.visible !== false) {
      nextEndpointLabels.push({
        ticker: trace.meta.ticker,
        name: trace.meta.displayName || trace.name,
        x: xs[lastVisibleIndex],
        y: lastVisibleValue,
      });
    }
    indices.push(index);
  });
  endpointLabels = nextEndpointLabels;
  if (indices.length) {
    Plotly.restyle(graph, {y: ys}, indices).then(() => {
      let targetYRange = null;
      if (visibleValues.length) {
        const min = Math.min(...visibleValues);
        const max = Math.max(...visibleValues);
        const padding = Math.max((max - min) * 0.05, 1);
        targetYRange = [min - padding, max + padding];
      }
      const layoutUpdate = comparisonOverlays(targetYRange);
      if (targetYRange) layoutUpdate['yaxis.range'] = targetYRange;
      Plotly.relayout(graph, layoutUpdate);
    });
  }
}
window.rebaseVisibleStockGraph = rebaseToVisibleStart;
function refreshVisibleView() {
  const fullLayout = graph._fullLayout;
  const range = fullLayout && fullLayout.xaxis && fullLayout.xaxis.range;
  if (!range) return;
  const startTime = new Date(range[0]).getTime();
  const endTime = new Date(range[1]).getTime();
  const visibleValues = [];
  const nextEndpointLabels = [];
  graph.data.forEach(trace => {
    if (!trace.meta || trace.meta.role !== 'stock' ||
        trace.visible === 'legendonly' || trace.visible === false) return;
    let lastIndex = -1;
    for (let index = 0; index < trace.x.length; index++) {
      const time = new Date(trace.x[index]).getTime();
      if (time >= startTime && time <= endTime) {
        lastIndex = index;
        const value = Number(trace.y[index]);
        if (Number.isFinite(value)) visibleValues.push(value);
      }
    }
    if (lastIndex >= 0) {
      nextEndpointLabels.push({
        ticker: trace.meta.ticker,
        name: trace.meta.displayName || trace.name,
        x: trace.x[lastIndex],
        y: Number(trace.y[lastIndex]),
      });
    }
  });
  endpointLabels = nextEndpointLabels;
  let targetYRange = null;
  if (visibleValues.length) {
    const min = Math.min(...visibleValues);
    const max = Math.max(...visibleValues);
    const padding = Math.max((max - min) * 0.05, 1);
    targetYRange = [min - padding, max + padding];
  }
  const layoutUpdate = comparisonOverlays(targetYRange);
  if (targetYRange) layoutUpdate['yaxis.range'] = targetYRange;
  Plotly.relayout(graph, layoutUpdate);
}
function setComparisonHelp(text) {
  const help = document.getElementById('stock-legend-help');
  if (help) help.textContent = text;
}
let lastHoveredStockPoints = [];
let lastHoverMouseEvent = null;
let lastHoverAt = 0;
let lastPlotlyClickAt = 0;
function isVisibleStockPoint(point) {
  const trace = graph.data[point.curveNumber];
  return Boolean(trace && trace.visible !== 'legendonly' && trace.visible !== false &&
    trace.meta && trace.meta.role === 'stock');
}
graph.on('plotly_hover', eventData => {
  lastHoveredStockPoints = eventData && eventData.points
    ? eventData.points.filter(isVisibleStockPoint)
    : [];
  lastHoverMouseEvent = eventData && eventData.event || null;
  lastHoverAt = Date.now();
});
graph.on('plotly_unhover', () => {
  lastHoverMouseEvent = null;
});
function handleGraphClick(eventData) {
  const clickPoints = eventData && eventData.points && eventData.points.length
    ? eventData.points
    : (Date.now() - lastHoverAt < 1000 ? lastHoveredStockPoints : []);
  const candidates = clickPoints.filter(isVisibleStockPoint);
  let point = candidates[0];
  const mouseEvent = eventData && eventData.event || lastHoverMouseEvent;
  const yAxis = graph._fullLayout && graph._fullLayout.yaxis;
  if (candidates.length && mouseEvent && yAxis) {
    const graphBounds = graph.getBoundingClientRect();
    const plotY = mouseEvent.clientY - graphBounds.top - graph._fullLayout._size.t;
    const pointerY = yAxis.range[1]
      - plotY / graph._fullLayout._size.h * (yAxis.range[1] - yAxis.range[0]);
    const axisRange = yAxis.range;
    const plotHeight = graph._fullLayout._size.h;
    const pixelsPerUnit = plotHeight / Math.abs(axisRange[1] - axisRange[0]);
    const closest = candidates.reduce((nearest, candidate) => {
      const distance = Math.abs(Number(candidate.y) - pointerY) * pixelsPerUnit;
      return distance < nearest.distance ? {point: candidate, distance} : nearest;
    }, {point, distance: Math.abs(Number(point.y) - pointerY) * pixelsPerUnit});
    if (closest.distance > 80) return;
    point = closest.point;
  }
  if (!point || point.curveNumber >= graph.data.length) return;
  const clickedTrace = graph.data[point.curveNumber];
  const tickerSelect = document.getElementById('purchase-ticker');
  if (clickedTrace.meta && clickedTrace.meta.ticker && tickerSelect) {
    tickerSelect.value = clickedTrace.meta.ticker;
    document.getElementById('purchase-date').value = new Date(point.x).toISOString().slice(0, 10);
    const pointData = point.customdata;
    const clickedRawPrice = Number(Array.isArray(pointData) ? pointData[0] : pointData);
    const clickedFxFactor = Array.isArray(pointData) ? Number(pointData[2] || 1) : 1;
    const clickedPrice = clickedRawPrice * clickedFxFactor;
    if (document.getElementById('purchase-type').value !== 'UTDELNING' && Number.isFinite(clickedPrice) && clickedPrice > 0) {
      document.getElementById('purchase-price').value = clickedPrice.toLocaleString('sv-SE', {useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2});
      if (window.updatePurchaseTotal) window.updatePurchaseTotal();
    }
  }
  const price = Number(Array.isArray(point.customdata) ? point.customdata[0] : point.customdata);
  if (!Number.isFinite(price) || price <= 0) return;

  const selectedPoint = {
    curveNumber: point.curveNumber,
    name: graph.data[point.curveNumber].meta.displayName || graph.data[point.curveNumber].name,
    x: point.x,
    y: Number(point.y),
    price: price,
    formattedPrice: Array.isArray(point.customdata) ? point.customdata[1] : String(price),
  };

  if (!firstComparisonPoint || firstComparisonPoint.curveNumber !== selectedPoint.curveNumber) {
    firstComparisonPoint = selectedPoint;
    setComparisonHelp(`Första punkten vald på ${selectedPoint.name}. Klicka en gång till på samma kurva.`);
    Plotly.relayout(graph, comparisonOverlays());
    return;
  }

  const first = firstComparisonPoint;
  firstComparisonPoint = null;
  setComparisonHelp('Klicka på aktien för av/på. Klicka två punkter på samma kurva för jämförelse.');
  const firstTime = new Date(first.x).getTime();
  const secondTime = new Date(selectedPoint.x).getTime();
  const earlier = firstTime <= secondTime ? first : selectedPoint;
  const later = firstTime <= secondTime ? selectedPoint : first;
  const change = (later.price / earlier.price - 1) * 100;
  const elapsedMonths = (new Date(later.x).getTime() - new Date(earlier.x).getTime())
    / (24 * 60 * 60 * 1000 * 30.4375);
  const formatPercent = value => `${value > 0 ? '+' : ''}${value.toFixed(2).replace('.', ',')}%`;
  const elapsedLabel = `${elapsedMonths.toFixed(1).replace('.', ',')} mån`;
  const monthlyChange = elapsedMonths > 0 ? change / elapsedMonths : null;
  const changeLabel = monthlyChange === null
    ? `${formatPercent(change)} · —/månad · ${elapsedLabel}`
    : `${formatPercent(change)} · ${formatPercent(monthlyChange)}/månad · ${elapsedLabel}`;
  comparisons.push({
    curveNumber: selectedPoint.curveNumber,
    x1: first.x,
    x2: selectedPoint.x,
    price1: first.price,
    price2: selectedPoint.price,
    label: `${selectedPoint.name}: ${changeLabel}`,
  });
  Plotly.relayout(graph, comparisonOverlays());
}
graph.on('plotly_click', eventData => {
  lastPlotlyClickAt = Date.now();
  handleGraphClick(eventData);
});
graph.addEventListener('click', event => {
  setTimeout(() => {
    if (Date.now() - lastPlotlyClickAt < 250) return;
    const bounds = graph.getBoundingClientRect();
    const layout = graph._fullLayout;
    if (!layout || !lastHoveredStockPoints.length || Date.now() - lastHoverAt > 1000) return;
    const pointX = event.clientX - bounds.left;
    const pointY = event.clientY - bounds.top;
    const plot = layout._size;
    if (pointX < plot.l || pointX > plot.l + plot.w || pointY < plot.t || pointY > plot.t + plot.h) return;
    handleGraphClick({points: lastHoveredStockPoints, event});
  }, 0);
});
graph.addEventListener('dblclick', event => {
  const clickedCurve = event.target && event.target.closest('.scatterlayer .trace');
  if (clickedCurve) return;
  firstComparisonPoint = null;
  setComparisonHelp('Klicka på aktien för av/på. Klicka två punkter på samma kurva för jämförelse.');
  comparisons.length = 0;
  Plotly.relayout(graph, comparisonOverlays());
});
window.syncVisibleDateInputs = function(range) {
  const startField = document.getElementById('visible-start-date');
  const endField = document.getElementById('visible-end-date');
  if (!startField || !endField) return;
  const activeRange = range || (graph._fullLayout && graph._fullLayout.xaxis && graph._fullLayout.xaxis.range);
  if (!activeRange || activeRange.length < 2) return;
  const asIsoDate = value => {
    if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed.toISOString().slice(0, 10) : '';
  };
  startField.value = asIsoDate(activeRange[0]);
  endField.value = asIsoDate(activeRange[1]);
  const status = document.getElementById('visible-date-status');
  if (status) status.textContent = '';
};
graph.on('plotly_relayout', eventData => {
  if (!eventData || !Object.keys(eventData).some(key => key.startsWith('xaxis.'))) return;
  let range = eventData['xaxis.range'];
  if (!Array.isArray(range) && (eventData['xaxis.range[0]'] || eventData['xaxis.range[1]'])) {
    const activeRange = graph._fullLayout && graph._fullLayout.xaxis && graph._fullLayout.xaxis.range;
    range = [eventData['xaxis.range[0]'] || (activeRange && activeRange[0]),
      eventData['xaxis.range[1]'] || (activeRange && activeRange[1])];
  }
  if (window.syncVisibleDateInputs) window.syncVisibleDateInputs(range);
  const rangeToPersist = Array.isArray(range) && range.length >= 2 && range[0] && range[1]
    ? range : (graph._fullLayout && graph._fullLayout.xaxis && graph._fullLayout.xaxis.range);
  if (window.persistVisibleDateRange) window.persistVisibleDateRange(rangeToPersist);
  clearTimeout(rebaseTimer);
  rebaseTimer = setTimeout(() => rebaseToVisibleStart(eventData), 100);
});
graph.on('plotly_restyle', eventData => {
  const [update, changedIndices] = eventData || [];
  if (!update || !Object.prototype.hasOwnProperty.call(update, 'visible')) return;
  const changedVisibility = update.visible;
  let indices = changedIndices;
  indices = indices || graph.data.map((_, index) => index);
  indices.forEach((traceIndex, position) => {
    const value = Array.isArray(changedVisibility)
      ? changedVisibility[position]
      : changedVisibility;
    traceIsVisible[traceIndex] = value !== 'legendonly' && value !== false;
  });
  clearTimeout(rebaseTimer);
  rebaseTimer = setTimeout(refreshVisibleView, 60);
});
setTimeout(() => rebaseToVisibleStart({}), 0);
setTimeout(() => window.renderPurchaseList && window.renderPurchaseList(), 0);
"""
    chart_html = figure.to_html(
        full_html=True,
        include_plotlyjs=True,
        post_script=post_script,
        config={"doubleClick": False},
    )
    run_local_chart_server(chart_html, stocks, price_series_by_ticker, fx_rates_by_currency)


if __name__ == "__main__":
    main()
