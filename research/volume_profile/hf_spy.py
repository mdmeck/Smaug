"""Pull SPY 1-min bars out of Rrishab/OHLCV-1m monthly parquet files.
Reads only the ticker column to find SPY's row group(s), then reads those."""
import fsspec, pyarrow.parquet as pq, pyarrow.compute as pc, pandas as pd, sys, os, time
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
TICKER = sys.argv[1] if len(sys.argv) > 1 else "SPY"
months = pd.period_range(*(sys.argv[2:4] if len(sys.argv) > 3 else ("2023-04", "2026-03")), freq="M")
out_dir = SP + (sys.argv[4] + "/" if len(sys.argv) > 4 else f"hf_{TICKER}_holdout/" if len(sys.argv) > 3 else f"hf_{TICKER}/")
os.makedirs(out_dir, exist_ok=True)
for m in months:
    dest = out_dir + f"{m}.pkl"
    if os.path.exists(dest):
        continue
    url = f"https://huggingface.co/datasets/Rrishab/OHLCV-1m/resolve/main/data/ohlcv_{m}.parquet"
    for attempt in range(3):
        try:
            t = time.time()
            pf = pq.ParquetFile(fsspec.open(url, block_size=2**22).open())
            parts = []
            for g in range(pf.num_row_groups):
                tk = pf.read_row_group(g, columns=["ticker"]).column(0)
                if pc.any(pc.equal(tk, TICKER)).as_py():
                    tb = pf.read_row_group(g)
                    parts.append(tb.filter(pc.equal(tb["ticker"], TICKER)).to_pandas())
            df = pd.concat(parts) if parts else pd.DataFrame()
            df.to_pickle(dest)
            print(m, len(df), f"{time.time() - t:.0f}s", flush=True)
            break
        except Exception as e:
            print(m, "error", e, flush=True)
            time.sleep(10)
