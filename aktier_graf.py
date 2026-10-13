"""Jämför ABB.ST, BRK-B och SCA-B.ST i ett gemensamt diagram."""

from datetime import date, datetime, time, timedelta
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import webbrowser

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf


STOCK_LIST_FILE = Path(__file__).with_name("aktier.csv")
PURCHASES_FILE = Path(__file__).with_name("aktiekop.csv")
MAX_STOCKS = 50
PLOT_COLORS = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]
START_DATE = "2010-01-01"
INITIAL_VISIBLE_START = pd.Timestamp("2020-01-01")
US_MARKET_TICKERS = {"BRK-B", "NDAQ", "SIRI", "RIVN"}


def load_stocks() -> list[dict[str, str | bool]]:
    """Läs aktielista och initial synlighet från aktier.csv."""
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


def load_purchases() -> list[dict[str, str]]:
    """Läs tidigare registrerade köp från CSV-filen."""
    if not PURCHASES_FILE.exists():
        return []
    with PURCHASES_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        rows = [row for row in csv.DictReader(file, delimiter=";") if row.get("ticker")]
    for row in rows:
        row.setdefault("transaction_type", "KÖP")
        row.setdefault("commission", "0")
    return rows


def currency_for_ticker(ticker: str) -> str:
    if ticker.endswith(".ST") or ticker == "BRK-B":
        return "kr"
    if ticker in {"NDAQ", "SIRI", "RIVN"}:
        return "USD"
    if ticker == "RHM.DE":
        return "EUR"
    return ""


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


def make_purchase_panel(stocks: list[dict[str, str | bool]], available_tickers: set[str]) -> str:
    options = "".join(
        f'<option value="{stock["ticker"]}">{stock["name"]}</option>'
        for stock in stocks
        if stock["ticker"] in available_tickers
    )
    panel = r"""
<section id="purchase-panel">
  <h2>Registrera aktieköp</h2>
  <form id="purchase-form">
    <label>Typ <select id="purchase-type" required><option value="KÖP">Köp</option><option value="SÄLJ">Sälj</option></select></label>
    <label>Aktie <select id="purchase-ticker" required>__OPTIONS__</select></label>
    <label>Datum <input id="purchase-date" type="date" required value="__TODAY__"></label>
    <label>Antal <input id="purchase-quantity" type="text" inputmode="decimal" placeholder="0" required></label>
    <label id="purchase-price-label">Kurs (i aktiens valuta) <input id="purchase-price" type="text" inputmode="decimal" size="8" placeholder="189,34" required></label>
    <label>Courtage <input id="purchase-commission" type="text" inputmode="decimal" placeholder="0,00" value="0,00" required></label>
    <button type="submit">Lägg till köp</button>
    <p id="purchase-total" aria-live="polite">Totalbelopp: —</p>
  </form>
  <p id="purchase-status" role="status">Köp sparas i aktiekop.csv och visas som stjärnor i diagrammet.</p>
</section>
<style>
  body { margin: 0; }
  #stock-chart-layout { display: flex; flex-direction: column; gap: 8px; padding: 0 12px; }
  #stock-chart-title { margin: 12px 0 0; font: 600 24px sans-serif; color: #2a3f5f; }
  #refresh-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; font: 14px sans-serif; }
  #refresh-data-button { padding: 7px 11px; cursor: pointer; }
  #visible-date-controls { display: flex; align-items: end; gap: 8px; }
  #visible-date-controls label { display: grid; gap: 3px; }
  #visible-date-controls input { box-sizing: border-box; width: 12ch; padding: 6px; font: inherit; }
  #visible-date-status { color: #a33; }
  #refresh-data-status { color: #435064; }
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
  #purchase-form button { width: auto; white-space: nowrap; }
  #purchase-status { max-width: 60ch; margin: 12px 0 0; color: #435064; }
  #purchase-total { align-self: end; white-space: nowrap; margin: 0 0 8px; font-weight: 600; color: #263448; }
  #purchase-list { width: fit-content; max-width: calc(100% - 92px); box-sizing: border-box; margin-top: 12px; margin-right: 0; margin-bottom: 28px; padding: 0; border: 0; font: 15px sans-serif; }
  #purchase-list h2 { margin: 0 0 12px; font-size: 19px; }
  #purchase-list table { width: max-content; max-width: 100%; border-collapse: collapse; text-align: left; }
  #purchase-list th, #purchase-list td { padding: 7px 9px; border-bottom: 1px solid #e1e5eb; white-space: nowrap; }
  #purchase-list th { background: #f3f5f8; }
  #purchase-list td.number { text-align: right; font-variant-numeric: tabular-nums; }
  #purchase-list-content { width: max-content; max-width: 100%; overflow-x: auto; }
  @media (max-width: 900px) { #stock-chart-viewport { flex-direction: column; } #stock-chart-viewport > .plotly-graph-div { flex: none; width: 100% !important; height: 70vh !important; min-height: 520px; } #stock-custom-legend { flex: 0 1 auto; width: 100%; max-height: 280px; padding: 8px; border: 1px solid #e1e5eb; } #purchase-panel, #purchase-list { margin-left: 92px; max-width: calc(100% - 92px); } #purchase-form { grid-template-columns: repeat(2, max-content); } #purchase-form button { grid-column: 1 / -1; justify-self: start; } #purchase-total { grid-column: 1 / -1; justify-self: start; margin: 0; } }
</style>
<script>
const purchaseGraph = document.querySelector('.plotly-graph-div');
const purchasePanel = document.getElementById('purchase-panel');
const chartLayout = document.createElement('div');
chartLayout.id = 'stock-chart-layout';
const chartTitle = document.createElement('h1');
chartTitle.id = 'stock-chart-title';
chartTitle.textContent = 'Aktiernas utveckling (index 100 vid synligt startdatum)';
const refreshControls = document.createElement('div');
refreshControls.id = 'refresh-controls';
const refreshButton = document.createElement('button');
refreshButton.id = 'refresh-data-button';
refreshButton.type = 'button';
refreshButton.textContent = 'Hämta senaste slutkurser';
const visibleDateControls = document.createElement('div');
visibleDateControls.id = 'visible-date-controls';
const visibleStartLabel = document.createElement('label');
visibleStartLabel.textContent = 'Startdatum (ÅÅÅÅ-MM-DD)';
const visibleStartInput = document.createElement('input');
visibleStartInput.id = 'visible-start-date';
visibleStartInput.type = 'text';
visibleStartInput.inputMode = 'numeric';
visibleStartInput.maxLength = 10;
visibleStartInput.placeholder = '2020-01-01';
visibleStartLabel.append(visibleStartInput);
const visibleEndLabel = document.createElement('label');
visibleEndLabel.textContent = 'Slutdatum (ÅÅÅÅ-MM-DD)';
const visibleEndInput = document.createElement('input');
visibleEndInput.id = 'visible-end-date';
visibleEndInput.type = 'text';
visibleEndInput.inputMode = 'numeric';
visibleEndInput.maxLength = 10;
visibleEndInput.placeholder = '2026-10-31';
visibleEndLabel.append(visibleEndInput);
visibleDateControls.append(visibleStartLabel, visibleEndLabel);
const visibleDateStatusElement = document.createElement('span');
visibleDateStatusElement.id = 'visible-date-status';
visibleDateStatusElement.setAttribute('role', 'status');
const refreshStatus = document.createElement('span');
refreshStatus.id = 'refresh-data-status';
refreshStatus.setAttribute('role', 'status');
refreshControls.append(refreshButton, visibleDateControls, visibleDateStatusElement, refreshStatus);
const purchaseList = document.createElement('section');
purchaseList.id = 'purchase-list';
purchaseList.innerHTML = '<h2>Registrerade aktieköp</h2><div id="purchase-list-content"></div>';
const chartViewport = document.createElement('div');
chartViewport.id = 'stock-chart-viewport';
const customLegend = document.createElement('aside');
customLegend.id = 'stock-custom-legend';
customLegend.setAttribute('aria-label', 'Aktielegend');
purchaseGraph.parentNode.insertBefore(chartLayout, purchaseGraph);
chartLayout.append(chartTitle, refreshControls, purchasePanel, chartViewport, purchaseList);
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
const initialAxisRange = purchaseGraph.layout.xaxis.range;
const sliderDataRange = purchaseGraph.layout.xaxis.rangeslider.range;
const allowedStartDate = toIsoDate(sliderDataRange[0]);
const allowedEndDate = toIsoDate(initialAxisRange[1]);
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
  const label = type === 'SÄLJ' ? 'Nettobelopp' : 'Totalbelopp';
  const amount = type === 'SÄLJ' ? quantity * price - commission : total;
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
document.getElementById('purchase-price').addEventListener('change', event => {
  const value = parsePurchaseNumber(event.target.value);
  if (Number.isFinite(value) && value > 0) {
    event.target.value = value.toLocaleString('sv-SE', {
      useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    window.updatePurchaseTotal();
  }
});
document.getElementById('purchase-ticker').addEventListener('change', () => {
  if (window.suggestPurchasePrice) window.suggestPurchasePrice();
  window.updatePurchaseTotal();
});
document.getElementById('purchase-type').addEventListener('change', window.updatePurchaseTotal);
document.getElementById('purchase-date').addEventListener('change', () => {
  if (window.suggestPurchasePrice) window.suggestPurchasePrice();
});
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
      const total = type === 'SÄLJ' ? quantity * price - commission : quantity * price + commission;
      const currency = details[4] || '';
      rows.push({
        date: String(date).slice(0, 10),
        type,
        name: stocksByTicker[trace.meta.ticker] || trace.meta.ticker,
        quantity: String(details[2]),
        price: `${details[1]}${currency ? ` ${currency}` : ''}`,
        commission: `${details[5] || '0'}${currency ? ` ${currency}` : ''}`,
        total: `${total.toLocaleString('sv-SE', {minimumFractionDigits: 2, maximumFractionDigits: 2})}${currency ? ` ${currency}` : ''}`,
      });
    });
  });
  rows.sort((a, b) => b.date.localeCompare(a.date) || a.name.localeCompare(b.name, 'sv'));
  container.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.textContent = 'Inga köp är registrerade ännu.';
    container.append(empty);
    return;
  }
  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>Datum</th><th>Typ</th><th>Aktie</th><th>Antal</th><th>Kurs</th><th>Courtage</th><th>Totalbelopp</th></tr></thead>';
  const body = document.createElement('tbody');
  rows.forEach(row => {
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
window.renderPurchaseList();
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
      await Plotly.relayout(purchaseGraph, {'xaxis.rangeslider.range': [firstDate, lastDate]});
      updatedTickers.forEach(ticker => window.refreshProfitLabel(ticker, false));
      window.renderCustomLegend();
      window.rebaseVisibleStockGraph({});
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
  purchaseStatus.textContent = 'Sparar köp…';
  try {
    const response = await fetch('/api/purchases', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(purchase),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Köpet kunde inte sparas.');
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
      customdata: [[[result.chart_price, result.price, result.quantity, result.ticker, result.currency, result.commission, result.transaction_type]]],
      'marker.symbol': [[result.transaction_type === 'KÖP' ? 'circle' : 'triangle-down']],
    }, [markerIndex]);
    if (window.refreshProfitLabel) window.refreshProfitLabel(result.ticker);
    window.renderPurchaseList();
    purchaseStatus.textContent = `${result.name}: ${result.transaction_type.toLowerCase()} ${result.quantity} st ${result.date} för ${result.price} ${result.currency} + ${result.commission} ${result.currency} courtage. Sparat i aktiekop.csv.`;
    document.getElementById('purchase-quantity').value = '';
    document.getElementById('purchase-price').value = '';
    document.getElementById('purchase-commission').value = '0';
    window.updatePurchaseTotal();
  } catch (error) {
    purchaseStatus.textContent = error.message;
  }
});
</script>
"""
    return panel.replace("__OPTIONS__", options).replace("__TODAY__", date.today().isoformat())


def run_local_chart_server(
    chart_html: str,
    stocks: list[dict[str, str | bool]],
    price_series_by_ticker: dict[str, pd.Series],
) -> None:
    """Servera diagrammet lokalt så köpformuläret kan spara CSV-filen."""
    available_tickers = set(price_series_by_ticker)
    options_panel = make_purchase_panel(stocks, available_tickers)
    page = chart_html.replace("</body>", options_panel + "</body>")

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
            if self.path == "/api/refresh":
                try:
                    updates: dict[str, dict[str, list]] = {}
                    warnings: list[str] = []
                    usdsek_close = None
                    brk_series = price_series_by_ticker.get("BRK-B")
                    if brk_series is not None:
                        fx_start = pd.Timestamp(brk_series.index[-1]).date().isoformat()
                        fx_end = (latest_closed_market_date("BRK-B") + timedelta(days=1)).isoformat()
                        fx_data = yf.download(
                            "USDSEK=X", start=fx_start, end=fx_end,
                            auto_adjust=True, progress=False,
                        )
                        if not fx_data.empty and "Close" in fx_data:
                            usdsek_close = fx_data["Close"]
                            if isinstance(usdsek_close, pd.DataFrame):
                                usdsek_close = usdsek_close.iloc[:, 0]
                            if getattr(usdsek_close.index, "tz", None) is not None:
                                usdsek_close.index = usdsek_close.index.tz_localize(None)
                            usdsek_close.index = pd.to_datetime(usdsek_close.index).normalize()
                            usdsek_close = usdsek_close.dropna().sort_index()

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
                                auto_adjust=True, progress=False,
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
                                    ]
                                    for value in close.values
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
                if transaction_type not in {"KÖP", "SÄLJ"}:
                    raise ValueError("Transaktionstyp måste vara köp eller sälj.")
                if not quantity.is_finite() or quantity <= 0:
                    raise ValueError("Antalet måste vara större än noll.")
                if not price.is_finite() or price <= 0:
                    raise ValueError("Kursen måste vara större än noll.")
                price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                price_text = format(price, ".2f")
                if not commission.is_finite() or commission < 0:
                    raise ValueError("Courtage måste vara noll eller större.")
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
                    if "commission" not in existing_header or "transaction_type" not in existing_header:
                        previous_purchases = load_purchases()
                        with PURCHASES_FILE.open("w", encoding="utf-8", newline="") as file:
                            writer = csv.DictWriter(file, fieldnames=["date", "transaction_type", "ticker", "quantity", "price", "commission"], delimiter=";")
                            writer.writeheader()
                            for previous in previous_purchases:
                                writer.writerow({**previous, "transaction_type": previous.get("transaction_type") or "KÖP", "commission": previous.get("commission") or "0"})
                needs_header = not PURCHASES_FILE.exists() or PURCHASES_FILE.stat().st_size == 0
                with PURCHASES_FILE.open("a", encoding="utf-8", newline="") as file:
                    writer = csv.DictWriter(
                        file,
                        fieldnames=["date", "transaction_type", "ticker", "quantity", "price", "commission"],
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
    usdsek_close = None

    if any(stock["ticker"] == "BRK-B" for stock in stocks):
        fx_end = (latest_closed_market_date("BRK-B") + timedelta(days=1)).isoformat()
        fx_data = yf.download("USDSEK=X", start=START_DATE, end=fx_end, auto_adjust=True, progress=False)
        if not fx_data.empty and "Close" in fx_data:
            usdsek_close = fx_data["Close"]
            if isinstance(usdsek_close, pd.DataFrame):
                usdsek_close = usdsek_close.iloc[:, 0]
            usdsek_close = usdsek_close.dropna().sort_index()

    for stock in stocks:
        ticker = stock["ticker"]
        label = stock["name"]
        end = (latest_closed_market_date(ticker) + timedelta(days=1)).isoformat()
        data = yf.download(
            ticker,
            start=START_DATE,
            end=end,
            auto_adjust=True,
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
                    ]
                    for value in series
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
        if not earlier_prices.empty and purchase_price > 0 and quantity > 0:
            purchases_by_ticker[ticker].append({
                "date": purchase_date.strftime("%Y-%m-%d"),
                "chart_price": float(earlier_prices.iloc[-1]),
                "price": str(purchase["price"]),
                "quantity": str(purchase["quantity"]),
                "commission": str(commission),
                "transaction_type": (purchase.get("transaction_type") or "KÖP").upper(),
                "currency": currency_for_ticker(ticker),
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
                        purchase["quantity"], ticker, purchase["currency"], purchase["commission"], purchase.get("transaction_type", "KÖP"),
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
                    "symbol": ["circle" if purchase.get("transaction_type", "KÖP") == "KÖP" else "triangle-down" for purchase in markers],
                    "size": 10,
                    "color": stock_colors[ticker],
                    "line": {"color": "#333333", "width": 1.5},
                },
                hovertemplate=(
                    f"{stock['name']} %{{customdata[6]}}<br>%{{customdata[2]}} st · "
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
    current_month_end = pd.Timestamp(date.today()) + pd.offsets.MonthEnd(0)
    visible_end = max(comparison.index.max(), current_month_end)
    figure.update_xaxes(
        range=[INITIAL_VISIBLE_START, visible_end],
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
    ));
  }
  if (soloTicker === ticker && soloVisibilitySnapshot) {
    const previous = soloVisibilitySnapshot;
    soloTicker = null;
    soloVisibilitySnapshot = null;
    return applyVisibility(previous);
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
    return applyVisibility(previous);
  }
  const indices = graph.data.map((trace, index) =>
    trace.meta && trace.meta.ticker === ticker ? index : -1
  ).filter(index => index >= 0);
  return Plotly.restyle(
    graph,
    {visible: indices.map(() => stockVisible ? 'legendonly' : true)},
    indices
  ).then(() => {
    window.renderCustomLegend();
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
      if (!(purchasePrice > 0 && enteredQuantity > 0)) return;
      const fee = Number.isFinite(commission) ? commission : 0;
      if ((details[6] || 'KÖP') === 'SÄLJ') {
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
    if (Number.isFinite(clickedPrice) && clickedPrice > 0) {
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
    run_local_chart_server(chart_html, stocks, price_series_by_ticker)


if __name__ == "__main__":
    main()
