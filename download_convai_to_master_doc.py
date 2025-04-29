#!/usr/bin/env python3
# download_convai_to_master_doc.py

import os
import re
import requests
import pickle
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ----------------- НАСТРОЙКИ -----------------
API_KEY         = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE       = 100
MIN_DURATION    = 60
SINCE           = int(datetime(2025, 4, 1).timestamp())
CREDENTIALS     = "credentials.json"
SCOPES          = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
# Ваш часовой пояс относительно UTC
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "4"))

# ----------------- Google OAuth -----------------
def get_google_services():
    creds = None
    if os.path.exists("token.pickle"):
        creds = pickle.load(open("token.pickle","rb"))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline")
        pickle.dump(creds, open("token.pickle","wb"))
    docs_svc  = build("docs","v1",credentials=creds)
    drive_svc = build("drive","v3",credentials=creds)
    return docs_svc, drive_svc

docs_service, drive_service = get_google_services()

# ----------------- ConvAI API -----------------
session = requests.Session()
session.trust_env = False
session.headers.update({
    "xi-api-key": API_KEY,
    "Accept":     "application/json"
})

def fetch_all_calls():
    url, params, out = "https://api.elevenlabs.io/v1/convai/conversations", {"page_size": PAGE_SIZE}, []
    while True:
        r = session.get(url, params=params); r.raise_for_status()
        j = r.json()
        out.extend(j.get("conversations",[]))
        if not j.get("has_more", False): break
        params["cursor"] = j["next_cursor"]
    return out

def fetch_call_detail(cid):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}")
    r.raise_for_status()
    return r.json()

# ----------------- DOC ID -----------------
def get_doc_id():
    # Сначала из Secrets
    env = os.getenv("MASTER_DOC_ID")
    if env:
        return env
    # Иначе спрашиваем у пользователя (первый раз)
    print("MASTER_DOC_ID не задан. Будет создан новый документ.")
    file = drive_service.files().create(
        body={"name":"ConvAI_Master_Log","mimeType":"application/vnd.google-apps.document"},
        fields="id"
    ).execute()
    doc_id = file["id"]
    print("Новый документ создан, его ID =", doc_id)
    print("Скопируйте этот ID в Secrets → MASTER_DOC_ID и перезапустите скрипт.")
    exit(0)

# ----------------- Читаем последний timestamp из документа -----------------
def load_last_from_doc(doc_id):
    doc = docs_service.documents().get(documentId=doc_id).execute()
    text = ""
    for elem in doc.get("body",{}).get("content",[]):
        if "paragraph" not in elem: 
            continue
        for run in elem["paragraph"].get("elements",[]):
            txt = run.get("textRun",{}).get("content","")
            text += txt
    # ищем все строки === Call at YYYY-MM-DD HH:MM:SS ===
    matches = re.findall(r"=== Call at ([0-9\-: ]{19}) ===", text)
    if not matches:
        return SINCE  # если ещё нет ни одного блока
    # самый первый match — самый старый, мы хотят последний (самый свежий)
    last_ts_str = matches[-1]
    dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
    # конвертим обратно в UTC
    dt_utc = dt - timedelta(hours=TZ_OFFSET_HOURS)
    return int(dt_utc.timestamp())

# ----------------- Формат звонка -----------------
def format_call(detail, fallback_ts):
    st = detail.get("metadata",{}).get("start_time_unix_secs", fallback_ts)
    dt = datetime.utcfromtimestamp(st) + timedelta(hours=TZ_OFFSET_HOURS)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    summ = detail.get("analysis",{}).get("transcript_summary","").strip()
    transcript = detail.get("transcript",[])
    lines, prev = [], None
    for m in transcript:
        role = (m.get("role") or "").upper()
        txt  = (m.get("message") or "").strip()
        if not txt: continue
        sec = m.get("time_in_call_secs",0.0)
        prefix = f"[{sec:06.2f}s] {role}: "
        if role == prev:
            lines[-1] += "\n" + prefix + txt
        else:
            if prev and prev != role: lines.append("")
            lines.append(prefix + txt)
        prev = role
    header = f"=== Call at {ts} ===\n"
    if summ: header += f"Summary:\n{summ}\n"
    body = "\n".join(lines)
    return header + "\n" + body + "\n\n" + "―"*40 + "\n\n"

# ----------------- MAIN -----------------
def main():
    doc_id = get_doc_id()

    # Узнаём последний сохранённый timestamp
    last_ts = load_last_from_doc(doc_id)

    # Берём все звонки
    calls = fetch_all_calls()
    sel   = [
        c for c in calls
        if c.get("start_time_unix_secs",0) >= SINCE
        and c.get("call_duration_secs",0) > MIN_DURATION
    ]
    # Оставляем только новые
    new_calls = [c for c in sel if c["start_time_unix_secs"] > last_ts]
    if not new_calls:
        print("Новых звонков нет.")
        return

    # Сортируем: последний звонок первым
    new_calls.sort(key=lambda x:x["start_time_unix_secs"], reverse=True)

    # Формируем текст
    full_text = ""
    for c in new_calls:
        block = format_call(fetch_call_detail(c["conversation_id"]), c["start_time_unix_secs"])
        full_text += block

    # Вставляем в начало документа ровно как на локале
    requests = [{
        "insertText": {
            "location": {"index": 1},
            "text": full_text
        }
    }]
    docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

    print(f"Добавлено {len(new_calls)} новых звонков.")

if __name__ == "__main__":
    main()
