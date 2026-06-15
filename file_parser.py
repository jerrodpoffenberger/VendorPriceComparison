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
