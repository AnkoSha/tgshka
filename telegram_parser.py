import os
import re
import json
import gspread
import traceback
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import PeerChannel
import nest_asyncio

nest_asyncio.apply()

API_ID           = int(os.environ['TELEGRAM_API_ID'])
API_HASH         = os.environ['TELEGRAM_API_HASH']
SESSION_STRING   = os.environ['TELEGRAM_SESSION']
GOOGLE_CREDS_RAW = os.environ['GOOGLE_CREDENTIALS']
SPREADSHEET_ID   = os.environ.get('SPREADSHEET_ID')
SPREADSHEET_NAME = os.environ.get('SPREADSHEET_NAME')

CHANNEL_LINK = PeerChannel(-1001545069749)

DATA_SHEET  = 'data'
MERCH_SHEET = 'merch'
SYS_SHEET   = '_system'

DATA_HEADERS = [
    'message_id', 'msg_num', 'number', 'standart_num', 'value', 'type',
    'date_ins', 'casino_name', 'url', 'pp_url', 'bank_or_operator', 'parsed_at'
]
MERCH_HEADERS = [
    'message_id', 'msg_num', 'service', 'recipient',
    'legal_entity', 'bank', 'parsed_at'
]
SYS_HEADERS = ['key', 'value']


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


def to_standart_phone(num: str) -> str:
    digits = re.sub(r'\D', '', num)
    if not digits:
        return ''
    if len(digits) < 13:
        return digits.zfill(13)
    return digits


def parse_message(text):
    result = {'casino_name': None, 'url': None, 'pp_url': None,
              'cards': [], 'phones': []}
    if not text:
        return result
    lines = [line.strip() for line in str(text).split('\n') if line.strip()]
    if not lines:
        return result

    casino_name = None
    for line in lines:
        ll = line.lower()
        if ('casino' in ll and not line.startswith('Сайт:')
                and not line.startswith('URL:') and not line.startswith('ПП:')
                and 'банк' not in ll and 'сбер' not in ll and 'альфа' not in ll):
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
        ll = line.lower()

        is_bank = False
        for bank in BANKS:
            if bank in ll:
                current_bank = line
                current_operator = None
                is_bank = True
                break
        if is_bank:
            continue

        is_op = False
        for op in OPERATORS:
            if op in ll:
                current_operator = line
                current_bank = None
                is_op = True
                break
        if is_op:
            continue

        is_stop = False
        for w in STOP_WORDS:
            if w in ll and len(line) < 20:
                is_stop = True
                break
        if is_stop:
            continue

        for num in re.findall(r'\d+', line):
            if len(num) < 10 or len(num) > 19:
                continue
            if len(num) >= 13:
                result['cards'].append((num, current_bank))
            else:
                if num.startswith('8'):
                    num = '7' + num[1:]
                elif num.startswith('9') and len(num) == 10:
                    num = '7' + num
                result['phones'].append((num, current_operator))
    return result


RE_RECIPIENT = re.compile(r'(?:Получатель|Получатель\s*платежа|Реквизиты\s*получателя)\s*[:\-]\s*(.+)', re.IGNORECASE)
RE_LEGAL     = re.compile(r'(?:Наименование\s*ЮЛ|ЮЛ|Юр\.?\s*лицо|Наименование\s*получателя)\s*[:\-]\s*(.+)', re.IGNORECASE)
RE_BANK      = re.compile(r'(?:Банк\s*получателя|Банк)\s*[:\-]\s*(.+)', re.IGNORECASE)
RE_SERVICE   = re.compile(r'(?:Casino|Сервис|Казино|Платёжка|Платежка)\s*[:\-]\s*(.+)', re.IGNORECASE)
RE_MSG_NUM   = re.compile(r'(?:Пост|Номер\s*поста|#)\s*[:\-]?\s*(\d+)', re.IGNORECASE)


def parse_merch(text, message_id):
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

    recips = RE_RECIPIENT.findall(text)
    legals = RE_LEGAL.findall(text)
    banks  = RE_BANK.findall(text)
    n = max(len(recips), len(legals), len(banks))
    if n == 0:
        return []

    out = []
    for i in range(n):
        out.append({
            'message_id': message_id,
            'msg_num': msg_num,
            'service': service,
            'recipient': recips[i].strip() if i < len(recips) else '',
            'legal_entity': legals[i].strip() if i < len(legals) else '',
            'bank': banks[i].strip() if i < len(banks) else '',
        })
    return out


def setup_sheets():
    creds_dict = json.loads(GOOGLE_CREDS_RAW)
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else gc.open(SPREADSHEET_NAME)

    def ensure(name, headers):
        try:
            ws = ss.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
        return ws

    return ss, ensure(DATA_SHEET, DATA_HEADERS), \
           ensure(MERCH_SHEET, MERCH_HEADERS), \
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
        print(f"⚠️ sys_set({key}) failed: {e}")


def push_rows(ws, rows):
    if not rows:
        return
    ws.append_rows(rows, value_input_option='RAW')


async def run_telegram():
    ss, ws_data, ws_merch, ws_sys = setup_sheets()
    last_id = sys_get(ws_sys, 'tg_last_id')
    last_id = int(last_id) if last_id and str(last_id).isdigit() else None
    print(f"📌 Telegram last_id: {last_id}")

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        print("✅ Telegram connected")
        await client.get_dialogs()
        channel = await client.get_entity(CHANNEL_LINK)
        print(f"📡 Channel: {channel.title}")

        if last_id:
            messages = await client.get_messages(channel, min_id=last_id, limit=None)
        else:
            messages = await client.get_messages(channel, limit=200)

        print(f"📨 Got {len(messages)} messages")

        data_rows, merch_rows = [], []
        now_iso = datetime.now(timezone.utc).isoformat()

        for msg in messages:
            if not msg.text:
                continue

            try:
                parsed = parse_message(msg.text)
                if parsed['casino_name']:
                    for card, bank in parsed['cards']:
                        data_rows.append([
                            msg.id, '', card, '', '', 'card',
                            str(msg.date), parsed['casino_name'],
                            parsed['url'] or '', parsed['pp_url'] or '',
                            bank or '', now_iso
                        ])
                    for phone, op in parsed['phones']:
                        std = to_standart_phone(phone)
                        data_rows.append([
                            msg.id, '', phone, std, '', 'phone',
                            str(msg.date), parsed['casino_name'],
                            parsed['url'] or '', parsed['pp_url'] or '',
                            op or '', now_iso
                        ])
            except Exception as e:
                print(f"⚠️ parse_message fail on msg {msg.id}: {e}")
                traceback.print_exc()

            try:
                for row in parse_merch(msg.text, msg.id):
                    merch_rows.append([
                        row['message_id'], row['msg_num'] or '',
                        row['service'] or '', row['recipient'] or '',
                        row['legal_entity'] or '', row['bank'] or '',
                        now_iso
                    ])
            except Exception as e:
                print(f"⚠️ parse_merch fail on msg {msg.id}: {e}")
                traceback.print_exc()

        push_rows(ws_data, data_rows)
        push_rows(ws_merch, merch_rows)
        print(f"💾 data: {len(data_rows)} rows, merch: {len(merch_rows)} rows")

        if messages:
            new_last = max(m.id for m in messages)
            sys_set(ws_sys, 'tg_last_id', new_last)
            print(f"✅ Updated tg_last_id -> {new_last}")
