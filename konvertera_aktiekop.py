#!/usr/bin/env python3
"""Convert a broker transaction export into aktiekop.csv.

Run without arguments for a preview. Pass --apply to import mapped rows.
"""

import argparse
import csv
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import os
from pathlib import Path
import tempfile


SOURCE_FILE = Path.home() / "Downloads" / "transactions-and-notes-export.csv"
TARGET_FILE = Path(__file__).with_name("aktiekop.csv")
FIELDNAMES = ["date", "transaction_type", "ticker", "quantity", "price", "commission", "source_id", "amount_currency"]

# Match the broker's Värdepapper text exactly. ISIN is deliberately not used.
SECURITY_TO_TICKER = {
    "ABB": "ABB.ST", "Addtech": "ADDT-B.ST", "Alfa Laval": "ALFA.ST",
    "Assa Abloy": "ASSA-B.ST", "AstraZeneca": "AZN.ST", "Atlas Copco": "ATCO-B.ST",
    "Boliden": "BOL.ST", "Cibus Real Estate": "CIBUS.ST", "Epiroc": "EPI-B.ST", "EQT": "EQT.ST",
    "Ericsson": "ERIC-B.ST", "Essity": "ESSITY-B.ST", "Evolution": "EVO.ST",
    "H&M": "HM-B.ST", "Hexagon": "HEXA-B.ST", "Industrivärden": "INDU-C.ST",
    "Industrivärden C": "INDU-C.ST", "Industrivärden A": "INDU-A.ST",
    "Investor": "INVE-B.ST", "Lifco": "LIFCO-B.ST", "NIBE": "NIBE-B.ST",
    "Nordea": "NDA-SE.ST", "Saab": "SAAB-B.ST", "SAAB B": "SAAB-B.ST",
    "Sandvik": "SAND.ST", "SEB": "SEB-A.ST", "Skanska": "SKA-B.ST",
    "SKF": "SKF-B.ST", "SCA": "SCA-B.ST", "Handelsbanken": "SHB-A.ST",
    "Tele2 B": "TEL2-B.ST",
    "Swedbank": "SWED-A.ST", "Tele2": "TEL2-B.ST", "Telia": "TELIA.ST",
    "Volvo": "VOLV-B.ST", "Volvo B": "VOLV-B.ST",
    "Berkshire Hathaway B": "BRK-B", "Latour B": "LATO-B.ST",
    "Nasdaq": "NDAQ", "Electrolux B": "ELUX-B.ST", "Sirius XM": "SIRI",
    "Rheinmetall": "RHM.DE", "Rivian Automotive A": "RIVN",
}
TICKER_CURRENCY = {
    "BRK-B": "SEK",  # Berkshire-kursen i exporten anges i SEK enligt användaren.
    "NDAQ": "USD", "SIRI": "USD", "RIVN": "USD", "RHM.DE": "EUR",
}


def decimal_value(value: str) -> Decimal:
    return Decimal("".join(value.strip().split()).replace(",", "."))


def canonical_decimal(value: str) -> str:
    return format(decimal_value(value).normalize(), "f")


def read_existing() -> list[dict[str, str]]:
    if not TARGET_FILE.exists() or TARGET_FILE.stat().st_size == 0:
        return []
    with TARGET_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        rows = [row for row in csv.DictReader(file, delimiter=";") if row.get("ticker")]
    for row in rows:
        row["transaction_type"] = (row.get("transaction_type") or "KÖP").upper()
        row["commission"] = row.get("commission") or "0"
        row["source_id"] = row.get("source_id") or ""
        row["amount_currency"] = row.get("amount_currency") or ""
    return rows


def signature(row: dict[str, str]) -> tuple[str, ...]:
    return (
        row["date"], (row.get("transaction_type") or "KÖP").upper(), row["ticker"],
        canonical_decimal(row["quantity"]), canonical_decimal(row["price"]),
        canonical_decimal(row.get("commission") or "0"),
        (row.get("amount_currency") or "").upper(),
    )


def convert_source(
    source_file: Path = SOURCE_FILE, source_bytes: bytes | None = None,
) -> tuple[list[dict[str, str]], list[str], list[dict[str, object]], list[str]]:
    if source_bytes is None:
        source = source_file.open("r", encoding="utf-16", newline="")
    else:
        source = io.StringIO(source_bytes.decode("utf-16"), newline="")
    with source:
        reader = csv.reader(source, delimiter="\t")
        headers = next(reader, [])  # Duplicate Valuta headers are handled by fixed positions.
        converted, warnings, unmapped_rows = [], [], []
        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) < 28:
                reason = "för få kolumner; hoppas över."
                warnings.append(f"Rad {row_number}: {reason}")
                unmapped_rows.append({"row_number": row_number, "reason": reason, "cells": row})
                continue
            security = row[6].strip()
            ticker = SECURITY_TO_TICKER.get(security)
            if not ticker:
                reason = f"okänt värdepapper {security!r}; hoppas över."
                warnings.append(f"Rad {row_number}: {reason}")
                unmapped_rows.append({"row_number": row_number, "reason": reason, "cells": row})
                continue
            source_type = row[5].strip().upper()
            is_dividend = "UTDELNING" in source_type or "KÄLLSKATT" in source_type
            transaction_type = {"KÖPT": "KÖP", "SÅLT": "SÄLJ"}.get(source_type)
            if is_dividend:
                transaction_type = "UTDELNING"
            if not transaction_type:
                reason = f"okänd transaktionstyp {row[5]!r}; hoppas över."
                warnings.append(f"Rad {row_number}: {reason}")
                unmapped_rows.append({"row_number": row_number, "reason": reason, "cells": row})
                continue
            try:
                trade_date = row[2].strip() or row[1].strip()  # Affärsdag, fallback Bokföringsdag.
                target_currency = TICKER_CURRENCY.get(ticker, "SEK" if ticker.endswith(".ST") else "")
                rate = decimal_value(row[21] or "0")
                amount_currency = ""
                if is_dividend:
                    # Preserve the currency of the cash credit. It can differ from
                    # the stock's trading currency (for example, SEK for a US stock).
                    amount = decimal_value(row[13])
                    amount_currency = (row[14] or row[12]).strip().upper() or target_currency
                    if "KÄLLSKATT" in source_type and amount > 0:
                        amount = -amount
                    price = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    quantity = Decimal("1")
                    commission = Decimal("0")
                else:
                    quantity = decimal_value(row[8])
                    price = decimal_value(row[9])
                    commission = decimal_value(row[26] or "0")
                    fee_currency = row[27].strip().upper()
                    if ticker == "BRK-B":
                        # Brokerkursen står i utländsk valuta; lagra kostnaden i SEK i appen.
                        price *= rate
                    if fee_currency and target_currency and fee_currency != target_currency:
                        if rate <= 0:
                            raise ValueError("växlingskurs saknas för courtagekonvertering")
                        commission /= rate
                commission = commission.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                entry = {
                    "date": trade_date,
                    "transaction_type": transaction_type,
                    "ticker": ticker,
                    "quantity": format(quantity.normalize(), "f"),
                    "price": format(price.normalize(), "f"),
                    "commission": format(commission, ".2f"),
                    "source_id": row[0].strip(),
                    "amount_currency": amount_currency,
                }
                signature(entry)
                converted.append(entry)
            except (InvalidOperation, ValueError, IndexError) as error:
                reason = f"ogiltiga värden ({error}); hoppas över."
                warnings.append(f"Rad {row_number}: {reason}")
                unmapped_rows.append({"row_number": row_number, "reason": reason, "cells": row})
    return converted, warnings, unmapped_rows, headers


def write_rows(rows: list[dict[str, str]]) -> None:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=TARGET_FILE.parent, delete=False)
    temp_path = Path(handle.name)
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, TARGET_FILE)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def prepare_import(
    source_file: Path = SOURCE_FILE, source_bytes: bytes | None = None,
) -> dict[str, object]:
    source_rows, warnings, unmapped_rows, source_headers = convert_source(source_file, source_bytes)
    existing = read_existing()
    known_source_ids = {row.get("source_id", "") for row in existing if row.get("source_id")}
    legacy_signatures = Counter(signature(row) for row in existing if not row.get("source_id"))
    source_signatures_without_id: set[tuple[str, ...]] = set()
    new_rows, duplicates = [], 0
    for row in source_rows:
        key = signature(row)
        source_id = row.get("source_id", "")
        if source_id and source_id in known_source_ids:
            duplicates += 1
            continue
        # Older imports and manually entered rows have no source ID. Match those
        # once by their transaction details, then use source IDs for future imports.
        if legacy_signatures[key] > 0:
            legacy_signatures[key] -= 1
            if source_id:
                known_source_ids.add(source_id)
            duplicates += 1
            continue
        if not source_id and key in source_signatures_without_id:
            duplicates += 1
            continue
        if source_id:
            known_source_ids.add(source_id)
        else:
            source_signatures_without_id.add(key)
        new_rows.append(row)
    return {
        "converted": len(source_rows),
        "duplicates": duplicates,
        "new_rows": new_rows,
        "existing": existing,
        "warnings": warnings,
        "unmapped_rows": unmapped_rows,
        "source_headers": source_headers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Spara transaktionerna i aktiekop.csv")
    args = parser.parse_args()
    result = prepare_import()
    source_rows = int(result["converted"])
    duplicates = int(result["duplicates"])
    new_rows = result["new_rows"]
    existing = result["existing"]
    warnings = result["warnings"]
    print(f"Källfil: {SOURCE_FILE}")
    print(f"Konverterbara transaktioner: {source_rows}")
    print(f"Dubbletter som hoppas över: {duplicates}")
    print(f"Nya transaktioner: {len(new_rows)}")
    if warnings:
        print("Rader att granska:")
        for warning in warnings:
            print(f"  {warning}")
    counts = {}
    for row in new_rows:
        key = (row["transaction_type"], row["ticker"])
        counts[key] = counts.get(key, 0) + 1
    for (transaction_type, ticker), count in sorted(counts.items()):
        print(f"  {transaction_type} {ticker}: {count}")
    if not args.apply:
        print("Förhandsvisning klar. Kör med --apply för att spara.")
        return
    write_rows(existing + new_rows)
    print(f"Importerade {len(new_rows)} transaktioner till {TARGET_FILE}.")


if __name__ == "__main__":
    main()
