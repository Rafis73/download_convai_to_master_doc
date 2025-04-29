#!/usr/bin/env python3
import os
import time
import datetime
import sys
import socket
import requests
import pickle
from socket import timeout as SocketTimeout
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ----------------- Глобальный таймаут сокетов -----------------
socket.setdefaulttimeout(60)  # 60 секунд по умолчанию для всех операций чтения

# ----------------- НАСТРОЙКИ -----------------
API_KEY        = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE      = 100
MIN_DURATION   = 60  # секунды
SINCE          = int(datetime.datetime(2025, 4, 1, 0, 0).timestamp())
LAST_RUN_FILE  = "last_run.txt"
CREDENTIALS    = "credentials.json"
SCOPES         = ["https://www.googleapis.com/auth/documents"]
TZ_OFFSET      = int(os.environ.get("TZ_OFFSET_HOURS", "0"))

# ----------------- Google OAuth -----------------
def get_credentials():
    creds = None
    if os.path.exists("token.pickle"):
        creds = pickle.load(open("token.pickle", "rb"))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline")
        pickle.dump(creds, open("token.pickle", "wb"))
    return creds

creds = get_credentials()
docs_service = build("docs", "v1", credentials=creds)

# ----------------- ConvAI API -----------------
session = requests.Session()
session.trust_env = False
session.headers.update({"xi-api-key": API_KEY, "Accept": "application/json"})

def fetch_all_calls():
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    params = {"page_size": PAGE_SIZE}
    all_calls = []
    while True:
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
        except (requests.exceptions.ReadTimeout, SocketTimeout):
            print("⚠️ Таймаут при получении списка звонков, прерываюся.")
            sys.exit(1)
        data = r.json()
        all_calls.extend(data.get("conversations", []))
        if not data.get("has_more", False):
            break
        params["cursor"] = data["next_cursor"]
    return all_calls

def fetch_call_detail(cid):
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{cid}"
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except (requests.exceptions.ReadTimeout, SocketTimeout):
        print(f"⚠️ Таймаут при получении деталей звонка {cid}, пропускаю.")
        return {}
    return r.json()

# ----------------- Вспомогалки -----------------
def load_last_run():
    if os.path.exists(LAST_RUN_FILE):
        return int(open(LAST_RUN_FILE).read().strip())
    return 0

def save_last_run(ts):
    with open(LAST_RUN_FILE, "w") as f:
        f.write(str(int(ts)))

# ----------------- Формат звонка -----------------
def format_call(detail, fallback_ts):
    st = detail.get("metadata", {}).get("start_time_unix_secs", fallback_ts)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st + TZ_OFFSET*3600))
    summ = detail.get("analysis", {}).get("transcript_summary", "").strip()
    transcript = detail.get("transcript", [])
    lines, prev = [], None
    for m in transcript:
        role = (m.get("role") or "").upper()
        txt  = (m.get("message") or "").strip()
        if not txt:
            continue
        sec = m.get("time_in_call_secs", 0.0)
        line = f"[{sec:06.2f}s] {role}: {txt}"
        if prev and prev != role:
            lines.append("")
        if prev == role:
            lines[-1] += "\n" + line
        else:
            lines.append(line)
        prev = role

    header = f"=== Call at {ts} ===\n"
    if summ:
        header += f"Summary:\n{summ}\n"
    return header + "\n" + "\n".join(lines) + "\n\n" + "―"*40 + "\n\n"

# ----------------- Основной Flow -----------------
def main():
    doc_id = os.environ.get("MASTER_DOC_ID")
    if not doc_id:
        print("❌ Ошибка: переменная MASTER_DOC_ID не установлена.")
        sys.exit(1)

    calls = fetch_all_calls()
    sel   = [c for c in calls
             if c.get("start_time_unix_secs", 0) >= SINCE
             and c.get("call_duration_secs", 0) > MIN_DURATION]
    last  = load_last_run()
    new   = [c for c in sel if c["start_time_unix_secs"] > last]
    if not new:
        print("🔍 Нет новых звонков.")
        return

    # сортируем старые сначала, чтобы новые шли по очереди
    new.sort(key=lambda x: x["start_time_unix_secs"])
    full_text, max_ts = "", last

    for c in new:
        detail = fetch_call_detail(c["conversation_id"])
        if not detail:
            continue
        full_text += format_call(detail, c["start_time_unix_secs"])
        st = detail.get("metadata", {}).get("start_time_unix_secs", 0)
        if st > max_ts:
            max_ts = st

    # Вставляем в конец документа через endOfSegmentLocation
    requests_body = [
        {
            "insertText": {
                "endOfSegmentLocation": {},
                "text": full_text
            }
        }
    ]

    try:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests_body}
        ).execute()
    except (HttpError, SocketTimeout) as e:
        print("❌ Ошибка при обновлении документа:", e)
        sys.exit(1)

    save_last_run(max_ts)
    print(f"✅ Добавлено {len(new)} звонков в документ {doc_id}.")

if __name__ == "__main__":
    main()
