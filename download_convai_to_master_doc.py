#!/usr/bin/env python3
import os
import time
import datetime
import requests
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ----------------- НАСТРОЙКИ -----------------
API_KEY        = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE      = 100
MIN_DURATION   = 60  # секунды
SINCE          = int(datetime.datetime(2025, 4, 1, 0, 0).timestamp())
DOC_ID_FILE    = "doc_id.txt"       # только для локальной отладки
LAST_RUN_FILE  = "last_run.txt"
CREDENTIALS    = "credentials.json"
SCOPES         = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
TZ_OFFSET_HRS  = int(os.getenv("TZ_OFFSET_HOURS", "4"))

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
            creds = flow.run_local_server(port=0, access_type="offline", include_granted_scopes=True)
        pickle.dump(creds, open("token.pickle", "wb"))
    return creds

creds = get_credentials()
docs_service  = build("docs", "v1", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)

# ----------------- ConvAI API -----------------
session = requests.Session()
session.trust_env = False
session.headers.update({
    "xi-api-key": API_KEY,
    "Accept":     "application/json"
})

def fetch_all_calls():
    all_calls = []
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    params = {"page_size": PAGE_SIZE}
    while True:
        r = session.get(url, params=params); r.raise_for_status()
        j = r.json()
        all_calls.extend(j.get("conversations", []))
        if not j.get("has_more", False):
            break
        params["cursor"] = j["next_cursor"]
    return all_calls

def fetch_call_detail(cid):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}")
    r.raise_for_status()
    return r.json()

# ----------------- Хранение состояния -----------------
def load_last_run():
    if os.path.exists(LAST_RUN_FILE):
        return int(open(LAST_RUN_FILE).read().strip())
    return 0

def save_last_run(ts):
    open(LAST_RUN_FILE, "w").write(str(int(ts)))

def load_doc_id():
    # 1) Сначала смотрим Secret
    doc_id = os.getenv("MASTER_DOC_ID")
    if doc_id:
        return doc_id
    # 2) Потом локальный файл (для отладки вне Actions)
    if os.path.exists(DOC_ID_FILE):
        return open(DOC_ID_FILE).read().strip()
    return None

def save_doc_id(did):
    # Не трогаем, если используем секрет
    if os.getenv("MASTER_DOC_ID"):
        return
    open(DOC_ID_FILE, "w").write(did)

def create_master_doc():
    file = drive_service.files().create(
        body={"name":"ConvAI_Master_Log","mimeType":"application/vnd.google-apps.document"},
        fields="id"
    ).execute()
    return file["id"]

# ----------------- Формат звонка -----------------
def format_call(detail, fallback_ts):
    st = detail.get("metadata", {}).get("start_time_unix_secs", fallback_ts)
    dt = datetime.datetime.utcfromtimestamp(st) + datetime.timedelta(hours=TZ_OFFSET_HRS)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    summ = detail.get("analysis", {}).get("transcript_summary", "").strip()
    transcript = detail.get("transcript", [])
    lines, prev = [], None
    for m in transcript:
        role = (m.get("role") or "").upper()
        txt  = (m.get("message") or "").strip()
        if not txt:
            continue
        sec = m.get("time_in_call_secs", 0.0)
        prefix = f"[{sec:06.2f}s] {role}: "
        if role == prev:
            lines[-1] += "\n" + prefix + txt
        else:
            if prev and prev != role:
                lines.append("")
            lines.append(prefix + txt)
        prev = role

    header = f"=== Call at {ts} ===\n"
    if summ:
        header += f"Summary:\n{summ}\n"
    body = "\n".join(lines)
    return header + "\n" + body + "\n\n" + "―" * 40 + "\n\n"

# ----------------- Основной Flow -----------------
def main():
    doc_id = load_doc_id() or create_master_doc()
    save_doc_id(doc_id)

    calls = fetch_all_calls()
    sel   = [
        c for c in calls
        if c.get("start_time_unix_secs", 0) >= SINCE
        and c.get("call_duration_secs", 0) > MIN_DURATION
    ]
    last_ts   = load_last_run()
    new_calls = [c for c in sel if c["start_time_unix_secs"] > last_ts]
    if not new_calls:
        print("Нет новых звонков.")
        return

    new_calls.sort(key=lambda x: x["start_time_unix_secs"], reverse=True)

    full_text, max_ts = "", last_ts
    for c in new_calls:
        detail = fetch_call_detail(c["conversation_id"])
        full_text += format_call(detail, c["start_time_unix_secs"])
        ts_c = detail.get("metadata", {}).get("start_time_unix_secs", 0)
        if ts_c > max_ts:
            max_ts = ts_c

    # Вставка в начало документа:
    doc = docs_service.documents().get(documentId=doc_id).execute()
    end_idx = doc["body"]["content"][-1]["endIndex"]
    insert_at = 1 if end_idx > 1 else end_idx

    requests_body = [{
        "insertText": {
            "location": {"index": insert_at},
            "text": full_text
        }
    }]

    # раскраска, как в локальном скрипте
    offset = insert_at
    pos = 0
    color_map = {
        "AGENT": {"red": 0.0, "green": 0.5, "blue": 0.0},
        "USER":  {"red": 0.0, "green": 0.0, "blue": 0.8},
    }
    for line in full_text.splitlines(True):
        stripped = line.rstrip("\n")
        if stripped.startswith("[") and ":" in stripped:
            cpos = stripped.find(":", stripped.find("]")+1)
            if cpos != -1:
                start = offset + pos
                end   = start + cpos + 1
                role  = "AGENT" if "AGENT" in stripped[:cpos] else "USER"
                requests_body.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": {
                            "foregroundColor": {
                                "color": {"rgbColor": color_map[role]}
                            }
                        },
                        "fields": "foregroundColor"
                    }
                })
        pos += len(line)

    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests_body}
    ).execute()

    save_last_run(max_ts)
    print(f"Добавлено {len(new_calls)} звонков в Google Doc (ID={doc_id}).")

if __name__ == "__main__":
    main()
