"""
Парсер platshieldsng.io → Google Sheets.
Листы: shield_card, shield_sbp, _system (shield_last_v).
"""
import os
import re
import json
import time
import logging
import traceback
from datetime import datetime, timezone

import bs4
import requests
import pandas as pd
import numpy as np
import gspread
from google.oauth2.service_account import Credentials


PREFIX = 'shield_sng'

SITE_URLS = [
    'https://platshieldsng.io',
    'https://platshieldsng.com',
    'https://plat-shieldsng.io',
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://platshieldsng.io/sheets/view",
}

MAX_ROWS_PER_SHEET = 100
HARD_MAX_BYTES     = 12_500_000
MAX_ATTEMPTS       = 10
READ_TIMEOUT       = 5

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SPREADSHEET_ID   = os.environ.get('SPREADSHEET_ID')
GOOGLE_CREDS_RAW = os.environ.get('GOOGLE_CREDENTIALS')

SHIELD_CARD_SHEET = 'shield_card'
SHIELD_SBP_SHEET  = 'shield_sbp'
SYS_SHEET         = '_system'

SHIELD_CARD_HEADERS = [
    'date_ins', 'card', 'standart_num', 'casino_name', 'url_mirror',
    'url_pay_system', 'fio', 'bank_name', 'dop_info', 'parsed_at'
]
SHIELD_SBP_HEADERS = [
    'date_ins', 'phone', 'value', 'standart_num', 'casino_name', 'url_mirror',
    'url_pay_system', 'fio', 'bank_name', 'dop_info', 'parsed_at'
]
SYS_HEADERS = ['key', 'value']


# ------------- http -------------
def _get(url, max_attempts=5, read_timeout=READ_TIMEOUT):
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = SESSION.get(url, timeout=(10, read_timeout))
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            logging.warning(f'{PREFIX}: GET {url} — попытка {attempt}: {e}')
            if attempt < max_attempts:
                time.sleep(1)
    logging.error(f'{PREFIX}: GET {url} упал: {last}')
    return None


def _pick_base_url():
    for base in SITE_URLS:
        r = _get(f'{base}/sheets/', max_attempts=3, read_timeout=10)
        if r:
            logging.info(f'{PREFIX}: сайт {base}')
            return base
    raise RuntimeError(f'{PREFIX}: домены недоступны')


def _find_active_slug(base):
    r = _get(f'{base}/sheets/', max_attempts=5, read_timeout=10)
    if not r:
        return None
    soup = bs4.BeautifulSoup(r.text, 'html.parser')
    a = soup.find('a', class_='sheet-selector-btn active')
    if not a:
        return None
    m = re.search(r'sheet=([a-z0-9\-]+)', a.get('href') or '')
    return m.group(1) if m else None


def _get_version(base, slug):
    r = _get(f'{base}/sheets/view?sheet={slug}', max_attempts=5, read_timeout=10)
    if not r:
        return None
    m = re.search(r'SHEET_DATA_URL\s*=\s*"[^"]*v=([a-f0-9]+)', r.text)
    return m.group(1) if m else None


def _download_json(base, slug, v, hard_max_bytes=None):
    """
    Качает JSON до hard_max_bytes (или HARD_MAX_BYTES).
    Возвращает самый полный из полученных ответов.
    """
    if hard_max_bytes is None:
        hard_max_bytes = HARD_MAX_BYTES

    url = f'{base}/sheet-data.php?sheet={slug}'
    if v:
        url += f'&v={v}'

    best = ''
    best_len = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        total = 0
        buf = bytearray()
        t0 = time.time()
        try:
            with SESSION.get(url, timeout=(10, READ_TIMEOUT), stream=True) as r:
                r.raise_for_status()
                for c in r.iter_content(256 * 1024):
                    if not c:
                        continue
                    buf.extend(c)
                    total += len(c)
                    if total >= hard_max_bytes:
                        break
        except Exception as e:
            logging.warning(
                f'{PREFIX}: попытка {attempt}: {type(e).__name__} '
                f'на {total/1e6:.2f} МБ за {time.time()-t0:.1f}с'
            )

        dt = time.time() - t0
        logging.info(
            f'{PREFIX}: попытка {attempt}: {total/1e6:.2f} МБ за {dt:.1f}с'
        )

        if total > best_len:
            best = bytes(buf).decode('utf-8', errors='ignore')
            best_len = total

        # Если скачали хотя бы 95% от hard_max_bytes — хватит
        if total >= hard_max_bytes * 0.95:
            logging.info(f'{PREFIX}: достаточно ({total/1e6:.2f} МБ)')
            return best

        time.sleep(1)

    return best


# ------------- parse -------------
ROW_RE = re.compile(r'\[(?:"[^"]*"|[^\[\]"])(?:,(?:"[^"]*"|[^\[\]"]))*\]')


def _parse_all_sheets(text, max_rows):
    names = list(re.finditer(r'"name"\s*:\s*"([^"]+)"', text))
    positions = [m.end() for m in re.finditer(r'"data"\s*:\s*\[', text)]
    sheets = []
    for i, dpos in enumerate(positions):
        dend = positions[i + 1] if i + 1 < len(positions) else len(text)
        chunk = text[dpos:dend]
        name = f'Лист {i+1}'
        for nm in reversed(names):
            if nm.start() < dpos:
                name = nm.group(1)
                break
        rows = []
        for rm in ROW_RE.finditer(chunk):
            try:
                rows.append(json.loads(rm.group(0)))
            except Exception:
                break
            if len(rows) >= max_rows:
                break
        sheets.append({'name': name, 'rows': rows})
    return sheets


def normal_phone(x):
    """
    Нормализация номера.
    РФ: 8XXXXXXXXXX / 7XXXXXXXXXX / 78XXXXXXXXXX → 10 цифр.
    СНГ и всё остальное: 10-13 цифр → как есть.
    Длиннее 13 — это уже карты, отсеиваем (None).
    """
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    x = re.sub(r'\D', '', str(x))
    if not x:
        return None
    # РФ форматы
    if len(x) == 11 and x[0] in ('7', '8'):
        return x[1:]
    if len(x) == 12 and x[:2] == '78':
        return x[2:]
    # СНГ и остальные — 10-13 цифр принимаем как есть
    if 10 <= len(x) <= 13:
        return x
    return None


def to_standart_phone(value):
    """
    Стандартизованный номер.
    Для 10-значных (РФ) — '007'+num.
    Для СНГ (11-13) — сам номер.
    """
    if not value or not str(value).isdigit():
        return ''
    s = str(value)
    if len(s) == 10:
        return '007' + s
    return s


COLS_SBP  = ['date_ins', 'phone', 'casino_name', 'url_mirror',
             'url_pay_system', 'fio', 'bank_name', 'dop_info']
COLS_CARD = ['date_ins', 'card', 'casino_name', 'url_mirror',
             'url_pay_system', 'fio', 'bank_name', 'dop_info']


def _detect_kind(sheet_name):
    low = sheet_name.lower()
    if 'карт' in low:
        return 'card'
    if 'номер' in low or 'телефон' in low or 'сбп' in low:
        return 'sbp'
    return None


# ------------- sheets -------------
def setup_sheets():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_RAW),
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID \
        else gc.open(os.environ['SPREADSHEET_NAME'])

    def ensure(name, headers):
        try:
            ws = ss.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
        return ws

    return ss, ensure(SHIELD_CARD_SHEET, SHIELD_CARD_HEADERS), \
           ensure(SHIELD_SBP_SHEET, SHIELD_SBP_HEADERS), \
           ensure(SYS_SHEET, SYS_HEADERS)


def sys_get(ws_sys, key):
    try:
        for row in ws_sys.get_all_values():
            if row and row[0] == key:
                return row[1] if len(row) > 1 else None
    except Exception:
        pass
    return None


def sys_set(ws_sys, key, value):
    try:
        vals = ws_sys.get_all_values()
        for i, row in enumerate(vals, start=1):
            if row and row[0] == key:
                ws_sys.update_cell(i, 2, str(value))
                return
        ws_sys.append_row([key, str(value)])
    except Exception as e:
        logging.warning(f'{PREFIX}: sys_set({key}) fail: {e}')


def push_rows(ws, rows):
    if rows:
        ws.append_rows(rows, value_input_option='RAW')


# ------------- rows builder -------------
def _build_rows(rows, kind, parsed_at):
    cols = COLS_CARD if kind == 'card' else COLS_SBP
    n = len(cols)
    fixed = [list(r)[:n] + [''] * (n - len(r)) for r in rows]
    df = pd.DataFrame(fixed, columns=cols)

    if 'date_ins' in df.columns:
        df['date_ins'] = (df['date_ins'].astype(str)
                          .str.replace(',', '.', regex=False)
                          .str.replace('[^0-9 .]', '', regex=True)
                          .str.strip())
        df['date_ins'] = pd.to_datetime(
            df['date_ins'], format='mixed', dayfirst=True, errors='coerce'
        ).dt.date

    if 'fio' in df.columns:
        df['fio'] = df['fio'].replace(
            to_replace=r'^http.*$', value=np.nan, regex=True
        )

    out = []

    if kind == 'card':
        df['card'] = df['card'].astype(str).str.replace(r'\s+', '', regex=True)
        df = df[df['card'].notna() & (df['card'] != '') & (df['card'] != 'nan')]
        for _, r in df.iterrows():
            out.append([
                str(r['date_ins']) if pd.notna(r['date_ins']) else '',
                r['card'], '',
                r['casino_name'] or '',
                r['url_mirror'] or '',
                r['url_pay_system'] or '',
                r['fio'] if pd.notna(r['fio']) else '',
                r['bank_name'] or '',
                r['dop_info'] or '',
                parsed_at,
            ])
    else:
        # СБП: чистим и нормализуем
        df['phone'] = df['phone'].astype(str).str.replace('[^0-9]', '', regex=True)
        df['value'] = df['phone'].map(normal_phone)

        before = len(df)
        # 10-13 цифр — это номер. 14+ — карта (отсеиваем).
        df = df[df['value'].notna()]
        df = df[df['value'].str.len().between(10, 13)]
        logging.info(
            f'{PREFIX}: {kind}: после нормализации {len(df)}/{before} '
            f'(длины 10-13)'
        )

        # лог распределения длин
        if len(df) > 0:
            lengths = df['value'].str.len().value_counts().to_dict()
            logging.info(f'{PREFIX}: {kind}: длины номеров: {lengths}')

        for _, r in df.iterrows():
            out.append([
                str(r['date_ins']) if pd.notna(r['date_ins']) else '',
                r['phone'],
                r['value'],
                to_standart_phone(r['value']),
                r['casino_name'] or '',
                r['url_mirror'] or '',
                r['url_pay_system'] or '',
                r['fio'] if pd.notna(r['fio']) else '',
                r['bank_name'] or '',
                r['dop_info'] or '',
                parsed_at,
            ])
    return out


# ------------- публичная функция -------------
def run_shield(force_refresh=False, full=False):
    """
    force_refresh=True — игнорировать v.
    full=True          — полный дамп без лимитов (100 МБ, все строки).
    """
    global MAX_ROWS_PER_SHEET, HARD_MAX_BYTES

    saved_rows = MAX_ROWS_PER_SHEET
    saved_bytes = HARD_MAX_BYTES

    if full:
        MAX_ROWS_PER_SHEET = 500_000
        HARD_MAX_BYTES     = 100_000_000

    tag = 'FULL DUMP' if full else ('FORCE' if force_refresh else 'NORMAL')
    logging.info(f'{PREFIX}: === старт ({tag}) ===')
    logging.info(f'{PREFIX}: MAX_ROWS_PER_SHEET={MAX_ROWS_PER_SHEET}, '
                 f'HARD_MAX_BYTES={HARD_MAX_BYTES/1e6:.1f} МБ')

    try:
        ss, ws_card, ws_sbp, ws_sys = setup_sheets()

        base = _pick_base_url()
        slug = _find_active_slug(base)
        if not slug:
            logging.error(f'{PREFIX}: активной таблицы нет')
            return
        logging.info(f'{PREFIX}: slug={slug}')

        current_v = _get_version(base, slug)
        last_v = sys_get(ws_sys, 'shield_last_v')
        logging.info(f'{PREFIX}: last_v={last_v!r}, current_v={current_v!r}')

        if not force_refresh and not full \
                and current_v is not None and last_v == current_v:
            logging.info(f'{PREFIX}: не менялось, пропуск')
            return

        text = _download_json(base, slug, current_v,
                              hard_max_bytes=HARD_MAX_BYTES)
        logging.info(f'{PREFIX}: скачано {len(text)/1e6:.2f} МБ текста')
        if not text:
            logging.warning(f'{PREFIX}: не удалось скачать')
            return

        sheets = _parse_all_sheets(text, MAX_ROWS_PER_SHEET)
        logging.info(f'{PREFIX}: листов {len(sheets)}')
        for sh in sheets:
            logging.info(f'{PREFIX}:   "{sh["name"]}" — {len(sh["rows"])} строк')

        if not sheets:
            logging.warning(f'{PREFIX}: ничего не распарсилось')
            if full:
                with open('shield_dump_raw.txt', 'w', encoding='utf-8') as f:
                    f.write(text[:5_000_000])
                logging.info(f'{PREFIX}: сырой ответ сохранён в shield_dump_raw.txt')
            return

        parsed_at = datetime.now(timezone.utc).isoformat()
        total_card = total_sbp = 0

        for sh in sheets:
            try:
                kind = _detect_kind(sh['name'])
                if kind is None:
                    logging.warning(f'{PREFIX}/{sh["name"]}: тип не распознан')
                    continue

                rows = _build_rows(sh['rows'], kind, parsed_at)
                if not rows:
                    logging.info(
                        f'{PREFIX}/{sh["name"]} ({kind}): 0 строк после фильтра. '
                        f'Исходно было {len(sh["rows"])} строк'
                    )
                    continue

                push_rows(ws_card if kind == 'card' else ws_sbp, rows)
                if kind == 'card':
                    total_card += len(rows)
                else:
                    total_sbp += len(rows)
                logging.info(f'{PREFIX}/{sh["name"]} ({kind}): +{len(rows)}')

            except Exception as e:
                logging.error(f'{PREFIX}/{sh["name"]}: ошибка: {e}')
                traceback.print_exc()

        if current_v:
            sys_set(ws_sys, 'shield_last_v', current_v)

        logging.info(f'{PREFIX}: ИТОГО card={total_card}, sbp={total_sbp}')

    except Exception as e:
        logging.error(f'{PREFIX}: общая ошибка: {e}')
        traceback.print_exc()

    finally:
        # вернуть прежние значения
        MAX_ROWS_PER_SHEET = saved_rows
        HARD_MAX_BYTES     = saved_bytes
        logging.info(f'{PREFIX}: === конец ===')
