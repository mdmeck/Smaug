import os, requests, pandas as pd
URL = "https://uslbwanjvritktievkxc.supabase.co/rest/v1/bars"
KEY = "sb_publishable_e3RuFiItE8K2lwt5IXkhIw_vowfx3AS"
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
rows, off = [], 0
while True:
    r = requests.get(URL, headers={**H, "Range": f"{off}-{off+999}"},
                     params={"select": "ts,open,high,low,close,volume", "order": "ts"})
    r.raise_for_status()
    b = r.json()
    rows += b
    if len(b) < 1000:
        break
    off += 1000
df = pd.DataFrame(rows)
df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
df = df.set_index("ts").sort_index()
df.to_pickle(os.path.dirname(os.path.abspath(__file__)) + "/bars.pkl")
print(len(df), df.index.min(), df.index.max(), df.index.normalize().nunique())
