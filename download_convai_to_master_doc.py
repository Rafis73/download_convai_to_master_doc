#!/usr/bin/env python3
import os
import time
import datetime
import sys
import socket
import requests
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ————— Глобальный таймаут —————
socket.setdefaulttimeout(60)

# ————— Конфиг —————
API_KEY       = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE     = 100
MIN_DURATION  = 60  # сек
SINCE_EPOCH   = int(datetime.datetime(2025,4,1).timestamp())
CREDENTIALS   = "credentials.json"
SCOPES        = ["https://www.googleapis.com/auth/documents"]
TZ_OFFSET_H   = int(os.getenv("TZ_OFFSET_HOURS","0"))

# ————— Google OAuth —————
def get_creds():
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
    return creds

creds = get_creds()
docs_svc = build("docs","v1",credentials=creds)

# ————— ConvAI API —————
sess = requests.Session()
sess.trust_env = False
sess.headers.update({"xi-api-key":API_KEY,"Accept":"application/json"})

def fetch_all_calls():
    url="https://api.elevenlabs.io/v1/convai/conversations"
    params={"page_size":PAGE_SIZE}
    out=[]
    while True:
        r=sess.get(url,params=params,timeout=30); r.raise_for_status()
        j=r.json()
        out+=j.get("conversations",[])
        if not j.get("has_more",False): break
        params["cursor"]=j["next_cursor"]
    return out

def fetch_detail(cid):
    url=f"https://api.elevenlabs.io/v1/convai/conversations/{cid}"
    r=sess.get(url,timeout=30); r.raise_for_status()
    return r.json()

# ————— Парсим последний заголовок из документа —————
def get_last_run_from_doc(doc_id):
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    content = doc.get("body",{}).get("content",[])
    last_ts = SINCE_EPOCH
    for elem in content:
        para = elem.get("paragraph",{})
        text=""
        for el in para.get("elements",[]):
            tr = el.get("textRun",{})
            text += tr.get("content","")
        if text.startswith("=== Call at "):
            # формат: === Call at YYYY-MM-DD HH:MM:SS ===\n
            ts_str = text[len("=== Call at "):].split(" ===")[0]
            try:
                dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                # получить UTC-epoch: убрать смещение часов
                epoch = int(time.mktime(dt.timetuple())) - TZ_OFFSET_H*3600
                if epoch>last_ts: last_ts=epoch
            except:
                pass
    return last_ts

# ————— Формат одного звонка —————
def format_call(d, fallback):
    st = d.get("metadata",{}).get("start_time_unix_secs",fallback)
    dt = time.gmtime(st + TZ_OFFSET_H*3600)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", dt)
    summ = d.get("analysis",{}).get("transcript_summary","").strip()
    lines=[]
    prev=None
    for m in d.get("transcript",[]):
        role=(m.get("role") or "").upper()
        txt=(m.get("message") or "").strip()
        if not txt: continue
        sec=m.get("time_in_call_secs",0.0)
        line=f"[{sec:06.2f}s] {role}: {txt}"
        if prev and prev!=role: lines.append("")
        if prev==role:
            lines[-1]+="\n"+line
        else:
            lines.append(line)
        prev=role
    hdr=f"=== Call at {ts} ===\n"
    if summ: hdr+=f"Summary:\n{summ}\n"
    return hdr+"\n"+"\n".join(lines)+"\n\n"+"―"*40+"\n\n"

# ————— Main —————
def main():
    doc_id=os.getenv("MASTER_DOC_ID")
    if not doc_id:
        print("❌ Переменная MASTER_DOC_ID не установлена!")
        sys.exit(1)

    last_ts = get_last_run_from_doc(doc_id)

    calls = fetch_all_calls()
    sel   = [c for c in calls
             if c.get("start_time_unix_secs",0)>=SINCE_EPOCH
             and c.get("call_duration_secs",0)>MIN_DURATION]
    new   = [c for c in sel if c["start_time_unix_secs"]>last_ts]
    if not new:
        print("🔍 Нет новых звонков.")
        return

    new.sort(key=lambda x:x["start_time_unix_secs"])
    full=""
    max_ts=last_ts
    for c in new:
        d=fetch_detail(c["conversation_id"])
        full+=format_call(d,c["start_time_unix_secs"])
        st=d.get("metadata",{}).get("start_time_unix_secs",0)
        if st>max_ts: max_ts=st

    reqs=[{"insertText":{"endOfSegmentLocation":{},"text":full}}]
    try:
        docs_svc.documents().batchUpdate(documentId=doc_id,body={"requests":reqs}).execute()
    except HttpError as e:
        print("❌ Ошибка batchUpdate:",e)
        sys.exit(1)

    print(f"✅ Добавлено {len(new)} звонков. Последний ts={max_ts}")

if __name__=="__main__":
    main()
