import os
import re
import pandas as pd
import pdfplumber


def clean_price(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    s = re.sub(r'[$,\s]', '', s)
    match = re.search(r'\d+\.?\d*', s)
    if match:
        try:
            v = float(match.group())
            return v if v > 0 else None
        except ValueError:
            return None
    return None


def _df_to_rows(df: pd.DataFrame) -> list[dict]:
    df = df.dropna(how='all').dropna(axis=1, how='all')
    rows = []
    for _, row in df.iterrows():
        rows.append({str(k): (str(v) if pd.notna(v) else None) for k, v in row.items()})
    return rows


def parse_excel(filepath: str) -> list[dict]:
    # Try reading with header auto-detection first
    try:
        df = pd.read_excel(filepath)
        rows = _df_to_rows(df)
        if rows:
            return rows
    except Exception:
        pass

    # Fallback: scan for the header row manually
    df_raw = pd.read_excel(filepath, header=None)
    header_idx = 0
    for i, row in df_raw.iterrows():
        non_null = row.dropna()
        if len(non_null) >= 2 and any(isinstance(v, str) for v in non_null):
            header_idx = i
            break

    df_raw.columns = [
        str(v) if pd.notna(v) else f'col_{j}'
        for j, v in enumerate(df_raw.iloc[header_idx])
    ]
    df_raw = df_raw.iloc[header_idx + 1:].reset_index(drop=True)
    return _df_to_rows(df_raw)


def parse_csv(filepath: str) -> list[dict]:
    df = pd.read_csv(filepath)
    return _df_to_rows(df)


def parse_pdf(filepath: str) -> list[dict]:
    rows = []
    with pdfplumber.open(filepath) as pdf:
        headers = None
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                if headers is None:
                    headers = [str(h).strip() if h else f'col_{i}' for i, h in enumerate(table[0])]
                    data_rows = table[1:]
                else:
                    data_rows = table
                for row in data_rows:
                    if any(cell for cell in row):
                        rows.append({
                            headers[i] if i < len(headers) else f'col_{i}': (str(cell).strip() if cell else None)
                            for i, cell in enumerate(row)
                        })

    # Fallback: extract raw text lines
    if not rows:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ''
                for line in text.split('\n'):
                    line = line.strip()
                    if line:
                        rows.append({'text': line})

    return rows


def parse_excel_raw(filepath: str) -> tuple[list[list], int]:
    """Return (raw_rows, col_count) preserving column positions for multi-group picking."""
    import openpyxl
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    raw, col_count = [], 0
    for row in ws.iter_rows(values_only=True):
        values = [str(v) if v is not None else '' for v in row]
        if any(v for v in values):
            raw.append(values)
            col_count = max(col_count, len(values))
    return raw, col_count


_UNIT_WORDS = {'lb', 'lbs', 'kg', 'oz', 'ea', 'each', 'cs', 'case', 'pc', 'pcs',
               'box', 'bag', '/lb', '/kg', 'cwt', 'ton', 'doz', 'dozen'}


def detect_columns(rows: list[dict]) -> dict:
    """Heuristic: score each column as description, price, or unit."""
    if not rows:
        return {'desc_col': None, 'price_col': None, 'unit_col': None, 'confidence': 'low'}

    columns = list(rows[0].keys())
    p_score: dict[str, float] = {}
    d_score: dict[str, float] = {}
    u_score: dict[str, float] = {}

    for col in columns:
        values = [str(row.get(col) or '').strip() for row in rows]
        non_empty = [v for v in values if v]
        if not non_empty:
            p_score[col] = d_score[col] = u_score[col] = 0.0
            continue

        cl = col.lower()
        ps = ds = us = 0.0

        if any(w in cl for w in ('price', 'cost', 'rate', 'amt', 'amount', 'each', 'per')):
            ps += 5
        if any(w in cl for w in ('desc', 'item', 'cut', 'product', 'name', 'detail', 'commodity', 'label')):
            ds += 5
        if any(w in cl for w in ('unit', 'uom', 'measure', 'pkg', 'pack')):
            us += 5

        numeric_vals = [v for v in non_empty if clean_price(v) is not None]
        num_ratio = len(numeric_vals) / len(non_empty)
        text_vals = [v for v in non_empty if clean_price(v) is None]
        avg_len = sum(len(v) for v in text_vals) / len(text_vals) if text_vals else 0.0
        unit_ratio = sum(1 for v in non_empty if v.lower() in _UNIT_WORDS) / len(non_empty)

        if num_ratio > 0.6:
            ps += 4
            try:
                parsed = [clean_price(v) for v in numeric_vals]
                valid = [x for x in parsed if x]
                if valid:
                    avg_p = sum(valid) / len(valid)
                    if 0.05 <= avg_p <= 600:
                        ps += 2
            except Exception:
                pass

        if num_ratio < 0.15 and avg_len > 6:
            ds += 4
        elif num_ratio < 0.3 and avg_len > 10:
            ds += 2

        if unit_ratio > 0.4:
            us += 6
        elif 0 < avg_len < 5 and num_ratio < 0.3:
            us += 1

        p_score[col] = ps
        d_score[col] = ds
        u_score[col] = us

    best_price = max(columns, key=lambda c: p_score[c]) if columns else None
    best_desc  = max(columns, key=lambda c: d_score[c]) if columns else None
    rest = [c for c in columns if c not in (best_price, best_desc)]
    best_unit = max(rest, key=lambda c: u_score[c]) if rest else None
    if best_unit and u_score.get(best_unit, 0) < 2:
        best_unit = None

    pc = p_score.get(best_price, 0)
    dc = d_score.get(best_desc, 0)
    confidence = 'high' if pc >= 4 and dc >= 4 else 'medium' if pc >= 2 and dc >= 2 else 'low'

    return {'desc_col': best_desc, 'price_col': best_price,
            'unit_col': best_unit, 'confidence': confidence}


def detect_multigroup(raw_rows: list[list], col_count: int) -> dict:
    """Heuristic: detect side-by-side column groups in complex Excel sheets."""
    if not raw_rows or col_count < 3:
        return {'groups': [], 'confidence': 'low'}

    num_ratio = []
    has_content = []
    for ci in range(col_count):
        vals = [str(row[ci]).strip() for row in raw_rows if ci < len(row) and str(row[ci]).strip()]
        if not vals:
            num_ratio.append(0.0)
            has_content.append(False)
            continue
        n = sum(1 for v in vals if clean_price(v) is not None)
        num_ratio.append(n / len(vals))
        has_content.append(True)

    groups = []
    visited: set[int] = set()

    for ci in range(col_count):
        if ci in visited or not has_content[ci]:
            continue
        if num_ratio[ci] < 0.3:
            prices = []
            for pci in range(ci + 1, min(ci + 5, col_count)):
                if pci in visited:
                    break
                if num_ratio[pci] > 0.5:
                    prices.append(pci)
                    visited.add(pci)
                elif has_content[pci] and num_ratio[pci] < 0.3:
                    break
            if prices:
                groups.append({'desc': ci, 'prices': prices})
                visited.add(ci)

    conf = 'high' if len(groups) >= 2 else ('medium' if len(groups) == 1 else 'low')
    return {'groups': groups, 'confidence': conf}


def parse_file(filepath: str) -> list[dict]:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ('.xlsx', '.xls', '.xlsm'):
        return parse_excel(filepath)
    elif ext == '.csv':
        return parse_csv(filepath)
    elif ext == '.pdf':
        return parse_pdf(filepath)
    else:
        raise ValueError(f'Unsupported file type: {ext}')
