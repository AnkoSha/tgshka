import os
import re
import json
import gspread
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio
import nest_asyncio

nest_asyncio.apply()

# ---------------- ENV ----------------
API_ID           = int(os.environ['TELEGRAM_API_ID'])
API_HASH         = os.environ['TELEGRAM_API_HASH']
SESSION_STRING   = os.environ['TELEGRAM_SESSION']
GOOGLE_CREDS_RAW = os.environ['GOOGLE_CREDENTIALS']
SPREADSHEET_ID   = os.environ.get('SPREADSHEET_ID')
SPREADSHEET_NAME = os.environ.get('SPREADSHEET_NAME')  # fallback

CHANNEL_LINK = '@badbank7'

# Имена листов
DATA_SHEET  = 'data'
MERCH_SHEET = 'merch'
SYS_SHEET   = '_system'

# Заголовки для каждого листа
DATA_HEADERS = [
    'message_id', 'msg_num', 'number', 'value', 'type', 'date_ins',
    'casino_name', 'url', 'pp_url', 'bank_or_operator', 'raw_text', 'parsed_at'
]
MERCH_HEADERS = [
    'message_id', 'msg_num', 'service', 'recipient',
    'legal_entity', 'bank', 'raw_text', 'parsed_at'
]
SYS_HEADERS = ['last_id']


# ==========================================================
#  ПАРСИНГ: КАРТЫ / ТЕЛЕФОНЫ (для badbanks_data)
# ==========================================================

BANKS = [
    'банк', 'bank', 'сбер', 'альфа', 'тинькофф', 'т-банк', 'т банк',
    'втб', 'открытие', 'газпром', 'райффайзен', 'совком', 'юнистрим',
    'озон', 'псб', 'почта', 'мтс банк', 'киви', 'wildberries', 'отп',
    'солидарность', 'промсвязь', 'ренессанс', 'уралсиб', 'ак барс',
    'россельхоз', 'рсхб', 'хоум кредит', 'связь-банк', 'рнкб',
    'челябинвест', 'убрир', 'меткомбанк', 'приморье', 'дом.рф',
    'русский стандарт', 'никобанк', 'мир привилегий', 'душанбе сити',
    'спитамен', 'алиф', 'васл', 'эсхата', 'арванд', 'амонет',
    'актив', 'хумо', 'тавхид', 'кубань кредит', 'центр-инвест',
    'рокет', 'сдм', 'левобережный', 'долинск', 'зенит', 'бкс',
    'точка', 'синара', 'инбанк', 'цифра', 'ингосстрах', 'рост',
    'авангард', 'пойдём', 'алмазэргиэн', 'хакасский', 'кредит урал',
    'лко', 'свой', 'реалист', 'вологжанин', 'кузнецкбизнес',
    'мкб', 'ткб', 'севергаз', 'энергобанк', 'кредит европа',
    'объединенный капитал', 'аврора', 'кибит', 'универсал',
    'примсоц', 'абсолют', 'ставропольпром', 'экси', 'юнибанк',
    'savdogar', 'poytaxt', 'octobank', 'alliance', 'garant',
    'alokabank', 'ipakyuli', 'hamkorbank', 'ravnaq', 'trustbank',
    'kapital', 'companion', 'rsk', 'belgazprombank', 'optima',
    'wbbank', 'сдм-банк', 'транскапиталбанк', 'ситибанк'
]

OPERATORS = [
    'билайн', 'beeline', 'мтс', 'mts', 'мегафон', 'megafon',
    'теле2', 'tele2', 'йота', 'yota', 'ростелеком', 'tcell', 'fast shift'
]

STOP_WORDS = ['сайт', 'url', 'пп', 'банк', 'казино', 'casino', 'оплата', 'пополнение']


def parse_message(text):
    """Парсит сообщение и извлекает данные для badbanks_data."""
    result = {
        'casino_name': None,
        'url': None,
        'pp_url': None,
        'cards': [],
        'phones': []
    }
    if not text:
        return result

    lines = [line.strip() for line in str(text).split('\n') if line.strip()]
    if not lines:
        return result

    casino_name = None
    for line in lines:
        lower_line = line.lower()
        if ('casino' in lower_line and
                not line.startswith('Сайт:') and
                not line.startswith('URL:') and
                not line.startswith('ПП:') and
                'банк' not in lower_line and
                'сбер' not in lower_line and
                'альфа' not in lower_line):
            casino_name = line
            break
    if not casino_name:
        casino_name = lines[0]
    result['casino_name'] = casino_name

    for line in lines:
        if line.startswith('URL:'):
            if not result['url']:
                result['url'] = line.replace('URL:', '').strip()
        elif line.startswith('Сайт:'):
            result['url'] = line.replace('Сайт:', '').strip()
        elif line.startswith('ПП:'):
            result['pp_url'] = line.replace('ПП:', '').strip()

    current_bank = None
    current_operator = None

    for line in lines:
        if line.startswith(('URL:', 'Сайт:', 'ПП:')):
            continue
        if line == casino_name:
            continue

        lower_line = line.lower()

        is_bank = False
        for bank in BANKS:
            if bank in lower_line:
                current_bank = line
                current_operator = None
                is_bank = True
                break
        if is_bank:
            continue

        is_operator = False
        for op in OPERATORS:
            if op in lower_line:
                current_operator = line
                current_bank = None
                is_operator = True
                break
        if is_operator:
            continue

        is_stop = False
        for word in STOP_WORDS:
            if word in lower_line and len(line) < 20:
                is_stop = True
                break
        if is_stop:
            continue

        numbers = re.findall(r'\d+', line)
        for num in numbers:
            if len(num) < 10 or len(num) > 16:
                continue
            if len(num) == 16:
                result['cards'].append((num, current_bank))
            elif 10 <= len(num) <= 12:
                if num.startswith('8'):
                    num = '7' + num[1:]
                elif num.startswith('9') and len(num) == 10:
                    num = '7' + num
                result['phones'].append((num, current_operator))

    return result


# ==========================================================
#  ПАРСИНГ: МЕРЧАНТЫ (для badbanks_merch)
# ==========================================================

RE_RECIPIENT = re.compile(
    r'(?:Получатель|Получатель\s*платежа|Реквизиты\s*получателя)\s*[:\-]\s*(.+)',
    re.IGNORECASE
)
RE_LEGAL = re.compile(
    r'(?:Наименование\s*ЮЛ|ЮЛ|Юр\.?\s*лицо|Наименование\s*получателя)\s*[:\-]\s*(.+)',
    re.IGNORECASE
)
RE_BANK = re.compile(
    r'(?:Банк\s*получателя|Банк)\s*[:\-]\s*(.+)',
    re.IGNORECASE
)
RE_SERVICE = re.compile(
    r'(?:Casino|Сервис|Казино|Платёжка|Платежка)\s*[:\-]\s*(.+)',
    re.IGNORECASE
)
RE_MSG_NUM = re.compile(r'(?:Пост|Номер\s*поста|#)\s*[:\-]?\s*(\d+)', re.IGNORECASE)


def parse_merch(text, message_id):
    """Извлекает данные для badbanks_merch из одного сообщения."""
    if not text:
        return []

    service = None
    m = RE_SERVICE.search(text)
    if m:
        service = m.group(1).strip()

    msg_num = None
    m = RE_MSG_NUM.search(text)
    if m:
        msg_num = int(m.group(1))

    recipients = RE_RECIPIENT.findall(text)
    legals = RE_LEGAL.findall(text)
    banks = RE_BANK.findall(text)

    n = max(len(recipients), len(legals), len(banks))
    if n == 0:
        return []

    rows = []
    for i in range(n):
        rows.append({
            'message_id': message_id,
            'msg_num': msg_num,
            'service': service,
            'recipient': recipients[i].strip() if i < len(recipients) else '',
            'legal_entity': legals[i].strip() if i < len(legals) else '',
            'bank': banks[i].strip() if i < len(banks) else '',
            'raw_text': text,
        })
    return rows


# ==========================================================
#  GOOGLE SHEETS
# ==========================================================

def setup_sheets():
    creds_dict = json.loads(GOOGLE_CREDS_RAW)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    gc = gspread.authorize(creds)

    if SPREADSHEET_ID:
        ss = gc.open_by_key(SPREADSHEET_ID)
    else:
        ss = gc.open(SPREADSHEET_NAME)

    def ensure_sheet(name, headers):
        try:
            ws = ss.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
        return ws

    ws_data  = ensure_sheet(DATA_SHEET, DATA_HEADERS)
    ws_merch = ensure_sheet(MERCH_SHEET, MERCH_HEADERS)
    ws_sys   = ensure_sheet(SYS_SHEET, SYS_HEADERS)
    return ws_data, ws_merch, ws_sys


def get_last_id(ws_sys):
    try:
        val = ws_sys.acell('A2').value  # A1 — заголовок, A2 — значение
        if val and str(val).isdigit():
            return int(val)
    except Exception:
        pass
    return None


def set_last_id(ws_sys, value):
    ws_sys.update('A2', [[str(value)]])


def push_rows(ws, rows):
    if not rows:
        return
    ws.append_rows(rows, value_input_option='RAW')


# ==========================================================
#  MAIN
# ==========================================================

async def main():
    ws_data, ws_merch, ws_sys = setup_sheets()

    last_id = get_last_id(ws_sys)
    print(f"📌 Last processed ID: {last_id}")

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        print("✅ Telegram connected")
        channel = await client.get_entity(CHANNEL_LINK)
        print(f"📡 Channel: {channel.title}")

        if last_id:
            messages = await client.get_messages(channel, min_id=last_id, limit=None)
        else:
            messages = await client.get_messages(channel, limit=200)

        print(f"📨 Got {len(messages)} messages")

        data_rows = []
        merch_rows = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for msg in messages:
            if not msg.text:
                continue

            # --- badbanks_data ---
            parsed = parse_message(msg.text)
            if parsed['casino_name']:
                for card, bank in parsed['cards']:
                    data_rows.append([
                        msg.id, '', card, '', 'card', str(msg.date),
                        parsed['casino_name'], parsed['url'] or '',
                        parsed['pp_url'] or '', bank or '', msg.text, now_iso
                    ])
                for phone, op in parsed['phones']:
                    data_rows.append([
                        msg.id, '', phone, '', 'phone', str(msg.date),
                        parsed['casino_name'], parsed['url'] or '',
                        parsed['pp_url'] or '', op or '', msg.text, now_iso
                    ])

            # --- badbanks_merch ---
            merch = parse_merch(msg.text, msg.id)
            for row in merch:
                merch_rows.append([
                    row['message_id'], row['msg_num'] or '',
                    row['service'] or '', row['recipient'] or '',
                    row['legal_entity'] or '', row['bank'] or '',
                    row['raw_text'], now_iso
                ])

        push_rows(ws_data, data_rows)
        push_rows(ws_merch, merch_rows)
        print(f"💾 data: {len(data_rows)} rows, merch: {len(merch_rows)} rows")

        if messages:
            new_last = max(m.id for m in messages)
            set_last_id(ws_sys, new_last)
            print(f"✅ Updated last_id -> {new_last}")


if __name__ == '__main__':
    asyncio.run(main())
