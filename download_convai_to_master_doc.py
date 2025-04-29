#!/usr/bin/env python3
# download_convai_to_master_doc.py

import os
import time
import datetime
import requests
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ----------------- НАСТРОЙКИ -----------------
API_KEY       = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE     = 100
MIN_DURATION  = 60      # секунды
# С какого момента брать звонки (1 апреля 2025)
SINCE         = int(datetime.datetime(2025, 4, 1, 0, 0).timestamp())
DOC_ID_FILE   = "doc_id.txt"
LAST_RUN_FILE = "last_run.txt"
CREDENTIALS   = "credentials.json"
SCOPES        = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

# ----------------- Google OAuth -----------------
def get_credentials():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS, SCOPES
            )
            creds = flow.run_local_server(
                port=0,
                access_type="offline",
                include_granted_scopes=True,
            )
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)
    return creds

creds = get_credentials()
docs_service  = build("docs", "v1", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)

# ----------------- ConvAI API -----------------
session = requests.Session()
session.headers.update({
    "xi-api-key": API_KEY,
    "Accept":     "application/json"
})

def fetch_all_calls():
    url, params = (
        "https://api.elevenlabs.io/v1/convai/conversations",
        {"page_size": PAGE_SIZE},
    )
    all_ = []
    while True:
        r = session.get(url, params=params); r.raise_for_status()
        j = r.json()
        all_.extend(j.get("conversations", []))
        if not j.get("has_more", False):
            break
        params["cursor"] = j["next_cursor"]
    return all_

def fetch_call_detail(cid):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}")
    r.raise_for_status()
    return r.json()

# ----------------- Вспомогалки -----------------
def load_last_run():
    return int(open(LAST_RUN_FILE).read().strip()) if os.path.exists(LAST_RUN_FILE) else 0

def save_last_run(ts):
    with open(LAST_RUN_FILE, "w") as f:
        f.write(str(int(ts)))

def load_doc_id():
    return open(DOC_ID_FILE).read().strip() if os.path.exists(DOC_ID_FILE) else None

def save_doc_id(did):
    with open(DOC_ID_FILE, "w") as f:
        f.write(did)

def create_master_doc():
    meta = {
        "name":     "ConvAI_Master_Log",
        "mimeType": "application/vnd.google-apps.document"
    }
    file = drive_service.files().create(body=meta, fields="id").execute()
    return file["id"]

# ----------------- Формат звонка -----------------
def format_call(detail, fallback_ts):
    st = detail.get("metadata", {}).get("start_time_unix_secs", fallback_ts)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st))
    summ = detail.get("analysis", {}).get("transcript_summary", "").strip()
    transcript = detail.get("transcript", [])
    lines = []
    prev = None
    for m in transcript:
        role = (m.get("role") or "").upper()
        txt  = (m.get("message") or "").strip()
        if not txt:
            continue
        tsec = m.get("time_in_call_secs", 0.0)
        line = f"[{tsec:06.2f}s] {role}: {txt}"
        if prev and prev != role:
            lines.append("")  # разделитель
        if prev == role:
            lines[-1] += "\n" + line
        else:
            lines.append(line)
        prev = role
    body = "\n".join(lines)
    header = f"=== Call at {ts} ===\n"
    if summ:
        header += f"Summary:\n{summ}\n"
    return header + "\n" + body + "\n\n" + "―"*40 + "\n\n"

# ----------------- Основной Flow -----------------
def main():
    # 1) master-doc
    doc_id = load_doc_id() or create_master_doc()
    save_doc_id(doc_id)

    # 2) загрузка всех звонков
    calls = fetch_all_calls()

    # 3) фильтрация: с 1 апреля 2025 и дольше 1 минуты
    sel = [
        c for c in calls
        if c.get("start_time_unix_secs", 0) >= SINCE
        and c.get("call_duration_secs", 0) > MIN_DURATION
    ]

    # 4) только новые после last_run
    last_ts = load_last_run()
    new_calls = [
        c for c in sel
        if c.get("start_time_unix_secs", 0) > last_ts
    ]
    if not new_calls:
        print("Нет новых звонков для добавления.")
        return

    # 5) сортировка (последний звонок первым)
    new_calls.sort(
        key=lambda x: x["start_time_unix_secs"], reverse=True
    )

    # 6) формируем full_text, отслеживаем max_ts
    full_text = ""
    max_ts = last_ts
    for c in new_calls:
        cid      = c["conversation_id"]
        fallback = c.get("start_time_unix_secs", 0)
        detail   = fetch_call_detail(cid)
        block    = format_call(detail, fallback)
        full_text += block
        st_call = detail.get("metadata", {}).get("start_time_unix_secs", fallback)
        if st_call > max_ts:
            max_ts = st_call

    # 7) вставка и раскраска
    requests_body = []
    requests_body.append({
        "insertText": {
            "location": {"index": 1},
            "text": full_text
        }
    })
    offset = 1
    pos = 0
    color_map = {
        "AGENT": {"red": 0.0, "green": 0.5, "blue": 0.0},
        "USER":  {"red": 0.0, "green": 0.0, "blue": 0.8},
    }
    for line in full_text.splitlines(True):
        stripped = line.rstrip("\n")
        if stripped.startswith("[") and ":" in stripped:
            colon = stripped.find(":", stripped.find("]")+1)
            if colon != -1:
                start_idx = offset + pos
                end_idx   = start_idx + colon + 1
                role = "AGENT" if "AGENT" in stripped[:colon] else "USER"
                requests_body.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start_idx, "endIndex": end_idx},
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

    # 8) сохранение last_run
    save_last_run(max_ts)
    print(f"Добавлено {len(new_calls)} звонков в Google Doc (ID={doc_id}).")

if __name__ == "__main__":
    main()
