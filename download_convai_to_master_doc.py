#!/usr/bin/env python3
import os, time, datetime, sys, socket, requests, pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ————— Глобальный таймаут —————
socket.setdefaulttimeout(60)

# ————— Настройки —————
API_KEY       = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE     = 100
MIN_DURATION  = 60      # сек
SINCE_EPOCH   = int(datetime.datetime(2025,4,1).timestamp())
DOC_ID        = os.getenv("MASTER_DOC_ID")
LAST_RUN_FILE = "last_run.txt"

if not DOC_ID:
    print("ERROR: MASTER_DOC_ID не задан!")
    sys.exit(1)

# ————— Google OAuth —————
SCOPES = ["https://www.googleapis.com/auth/documents"]
def get_creds():
    creds = None
    if os.path.exists("token.pickle"):
        creds = pickle.load(open("token.pickle","rb"))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline")
        pickle.dump(creds, open("token.pickle","wb"))
    return creds

creds = get_creds()
docs = build("docs","v1",credentials=creds)

# ————— ConvAI API —————
sess = requests.Session()
sess.headers.update({"xi-api-key":API_KEY,"Accept":"application/json"})
sess.trust_env = False

def fetch_all_calls():
    out, url, params = [], "https://api.elevenlabs.io/v1/convai/conversations", {"page_size":PAGE_SIZE}
    while True:
        r = sess.get(url, params=params, timeout=30); r.raise_for_status()
        j = r.json()
        out += j.get("conversations",[])
        if not j.get("has_more",False): break
        params["cursor"] = j["next_cursor"]
    return out

def fetch_detail(cid):
    r = sess.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}", timeout=30)
    r.raise_for_status()
    return r.json()

# ————— Работа с last_run —————
def load_last():
    if not os.path.exists(LAST_RUN_FILE):
        return 0
    return int(open(LAST_RUN_FILE).read().strip())

def save_last(ts):
    open(LAST_RUN_FILE,"w").write(str(int(ts)))

# ————— Формат звонка —————
def format_call(det, fallback):
    st = det.get("metadata",{}).get("start_time_unix_secs",fallback)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st))
    summ = det.get("analysis",{}).get("transcript_summary","").strip()
    lines, prev = [], None
    for m in det.get("transcript",[]):
        role = (m.get("role") or "").upper()
        msg  = (m.get("message") or "").strip()
        if not msg: continue
        sec  = m.get("time_in_call_secs",0.0)
        line = f"[{sec:06.2f}s] {role}: {msg}"
        if prev and prev!=role: lines.append("")
        if prev==role:
            lines[-1] += "\n"+line
        else:
            lines.append(line)
        prev = role
    header = f"=== Call at {ts} ===\n"
    if summ: header += f"Summary:\n{summ}\n"
    return header + "\n" + "\n".join(lines) + "\n\n" + "―"*40 + "\n\n"

# ————— Main —————
def main():
    last_ts = load_last()
    calls = fetch_all_calls()
    sel   = [c for c in calls if c.get("start_time_unix_secs",0)>=SINCE_EPOCH and c.get("call_duration_secs",0)>MIN_DURATION]
    new   = [c for c in sel if c["start_time_unix_secs"]>last_ts]
    if not new:
        print("🔍 Нет новых звонков.")
        return

    new.sort(key=lambda x:x["start_time_unix_secs"])
    full, max_ts = "", last_ts
    for c in new:
        det = fetch_detail(c["conversation_id"])
        full += format_call(det, c["start_time_unix_secs"])
        st = det.get("metadata",{}).get("start_time_unix_secs",0)
        if st>max_ts: max_ts=st

    # Append-only через endOfSegmentLocation
    reqs = [{"insertText":{"endOfSegmentLocation":{},"text":full}}]
    try:
        docs.documents().batchUpdate(documentId=DOC_ID,body={"requests":reqs}).execute()
    except HttpError as e:
        print("❌ Ошибка при batchUpdate:", e)
        sys.exit(1)

    save_last(max_ts)
    print(f"✅ Добавлено {len(new)} звонков, last_run={max_ts}")

if __name__=="__main__":
    main()
