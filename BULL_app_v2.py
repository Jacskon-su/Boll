# ==========================================
# BULL 布林滾雪球選股掃描器
# Streamlit 版本 V2.1 (雲端優化版)
# ==========================================
import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import datetime
import logging
import plotly.graph_objects as go
import time
import concurrent.futures
import requests

# Google Sheets（可選，未設定時自動降級為 session_state）
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

try:
    import twstock
except ImportError:
    twstock = None

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

# ==========================================
# 頁面設定
# ==========================================
st.set_page_config(
    page_title="BULL 布林滾雪球掃描器",
    page_icon="🐂",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stDataFrame {font-size: 1.05rem;}
    [data-testid="stMetricValue"] {font-size: 1.6rem; font-weight: bold;}
    [data-testid="stMetricLabel"] {font-size: 0.9rem; color: #aaa;}
    .signal-exit {background:#3a1a1a; border-left:4px solid #ff4444; padding:8px 12px; border-radius:4px; margin:4px 0;}
    div[data-testid="stTabs"] button {font-size:1.05rem; font-weight:600;}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 參數
# ==========================================
PARAMS = {
    "boll_period"    : 15,
    "boll_std"       : 2.1,
    "squeeze_n"      : 15,
    "squeeze_lookback": 5,
    "vol_ma_days"    : 5,
    "vol_ratio"      : 1.5,
    "sma_trend_days" : 3,
    "vol_ma20_days"  : 20,
    "vol_heavy_days" : 5,
    "vol_shrink_days": 15,
    "addon_b_profit" : 0.10,
    "min_vol_shares" : 1_000_000,
}

# ==========================================
# Google Sheets 整合模組
# ==========================================
SHEET_COLS = ["symbol","name","進場日期","進場價","上次加碼價","持倉最高價","加碼次數","加碼紀錄"]
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]

def get_gsheet():
    if not GSPREAD_AVAILABLE: return None
    try:
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=SCOPES)
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(st.secrets["sheets"]["sheet_id"])
        try:
            ws = sh.worksheet("positions")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="positions", rows=1000, cols=20)
            ws.append_row(SHEET_COLS)
        return ws
    except: return None

def gs_load() -> pd.DataFrame:
    ws = get_gsheet()
    if ws is None: return pd.DataFrame(columns=SHEET_COLS)
    try:
        data = ws.get_all_records()
        if not data: return pd.DataFrame(columns=SHEET_COLS)
        df = pd.DataFrame(data)
        for col in ["加碼次數", "進場價", "上次加碼價", "持倉最高價"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df[SHEET_COLS]
    except: return pd.DataFrame(columns=SHEET_COLS)

def gs_save(df: pd.DataFrame):
    ws = get_gsheet()
    if ws is None: return
    try:
        ws.clear()
        ws.append_row(SHEET_COLS)
        if len(df) > 0:
            ws.append_rows(df[SHEET_COLS].fillna("").values.tolist(), value_input_option="USER_ENTERED")
    except: pass

def use_gsheet() -> bool:
    if not GSPREAD_AVAILABLE: return False
    try: return ("gcp_service_account" in st.secrets and "sheets" in st.secrets)
    except: return False

def init_state():
    defaults = {
        "positions": pd.DataFrame(columns=SHEET_COLS),
        "scan_result": None,
        "last_scan_time": None,
        "gs_loaded": False,
        "need_scan": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state: st.session_state[k] = v
    if not st.session_state["gs_loaded"] and use_gsheet():
        df_gs = gs_load()
        if len(df_gs) > 0: st.session_state["positions"] = df_gs
        st.session_state["gs_loaded"] = True

init_state()

# ==========================================
# 股票清單與歷史資料下載
# ==========================================
def get_stock_map():
    """優化版：強制同步 twstock 代碼資料"""
    try:
        if twstock:
            # 強制執行內部更新函數，確保雲端 Linux 環境載入完整的 codes JSON
            twstock.__update_codes() 
            
        stock_map = {}
        # 上市股票
        for code, info in twstock.twse.items():
            if len(code) == 4 and code.isdigit():
                stock_map[f"{code}.TW"] = {"code": code, "name": info.name, "market": "上市"}
        # 上櫃股票
        for code, info in twstock.tpex.items():
            if len(code) == 4 and code.isdigit():
                stock_map[f"{code}.TWO"] = {"code": code, "name": info.name, "market": "上櫃"}
        
        # 排除 9 字頭權證或特別股
        stock_map = {k: v for k, v in stock_map.items() if int(v['code']) < 9000}
        return stock_map
    except Exception as e:
        st.error(f"股票清單獲取失敗: {e}")
        return {}

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history_data(symbol, period="6mo"):
    try:
        df = yf.Ticker(symbol).history(period=period)
        if df.empty: return None
        if df.index.tz is not None: df.index = df.index.tz_localize(None)
        return df
    except: return None

def fetch_all_cached(symbols, period="6mo", pb=None, scan_date=""):
    """優化版：縮小 chunk_size 以應對 Streamlit Cloud Rate Limit"""
    all_data = {}
    chunk_size = 50  # 雲端環境建議縮小批次
    chunks = [symbols[i:i+chunk_size] for i in range(0, len(symbols), chunk_size)]

    for idx, batch in enumerate(chunks, 1):
        if pb:
            pb.progress(idx / len(chunks) * 0.85, text=f"📥 批量下載中 {idx}/{len(chunks)} 批 (每批{chunk_size}支)...")
        try:
            # 增加 threads 提高並行度，但縮小 batch size 降低被封機率
            raw = yf.download(batch, period=period, progress=False, group_by="ticker", threads=True, auto_adjust=True)
            
            if len(batch) == 1:
                sym = batch[0]
                if not raw.empty:
                    df = raw.copy()
                    if isinstance(df.columns, pd.MultiIndex): df = df.droplevel(1, axis=1)
                    df.index = pd.to_datetime(df.index)
                    if df.index.tz is not None: df.index = df.index.tz_localize(None)
                    all_data[sym] = df
            else:
                for sym in batch:
                    try:
                        df = raw[sym].dropna(how="all")
                        if df.empty: continue
                        df.index = pd.to_datetime(df.index)
                        if df.index.tz is not None: df.index = df.index.tz_localize(None)
                        all_data[sym] = df
                    except: continue
            # 雲端環境建議在批次間留一點緩衝
            time.sleep(0.5)
        except: continue

    missing = [s for s in symbols if s not in all_data]
    if missing and pb:
        pb.progress(0.9, text=f"🔄 補下載 {len(missing)} 支失敗項目...")
    for sym in missing:
        df = fetch_history_data(sym, period)
        if df is not None: all_data[sym] = df

    if scan_date:
        cutoff = pd.Timestamp(scan_date)
        all_data = {s: d[d.index <= cutoff] for s, d in all_data.items() if len(d[d.index <= cutoff]) >= 40}

    return all_data

# ==========================================
# 指標與策略邏輯
# ==========================================
def calc_indicators(df):
    close, volume = df["Close"], df["Volume"]
    bp = PARAMS["boll_period"]
    df["SMA"] = close.rolling(bp).mean()
    df["Std"] = close.rolling(bp).std()
    df["Upper"] = df["SMA"] + df["Std"] * PARAMS["boll_std"]
    df["Lower"] = df["SMA"] - df["Std"] * PARAMS["boll_std"]
    df["Bandwidth"] = df["Upper"] - df["Lower"]
    df["Vol_MA5"] = volume.rolling(PARAMS["vol_ma_days"]).mean()
    df["Vol_MA15"] = volume.rolling(PARAMS["vol_shrink_days"]).mean()
    df["Vol_MA20"] = volume.rolling(PARAMS["vol_ma20_days"]).mean()
    df["BW_Min"] = df["Bandwidth"].rolling(PARAMS["squeeze_n"]).min()
    df["Squeeze_Recent"] = (df["Bandwidth"] == df["BW_Min"]).shift(1).rolling(PARAMS["squeeze_lookback"]).max() == 1
    df["SMA_Up"] = df["SMA"] > df["SMA"].shift(PARAMS["sma_trend_days"])
    return df

def check_entry(df):
    if len(df) < 40: return False
    row = df.iloc[-1]
    return (row["Squeeze_Recent"] and float(row["Close"]) > float(row["Upper"]) and 
            float(row["Volume"]) > float(row["Vol_MA5"]) * PARAMS["vol_ratio"] and 
            row["SMA_Up"] and float(row["Vol_MA20"]) > PARAMS["min_vol_shares"])

def check_addon_b(df, pos_row):
    c = float(df["Close"].iloc[-1])
    last_p = float(pos_row["上次加碼價"])
    return (c > float(pos_row["持倉最高價"])) and ((c - last_p) / last_p >= PARAMS["addon_b_profit"]), c

def check_addon_a(df):
    if len(df) < 3: return False, None, None, None
    c_today, c_yest, sma_today, sma_yest = df["Close"].iloc[-1], df["Close"].iloc[-2], df["SMA"].iloc[-1], df["SMA"].iloc[-2]
    v_yest, vma15_yest = df["Volume"].iloc[-2], df["Vol_MA15"].iloc[-2]
    return (c_yest < sma_yest) and (v_yest < vma15_yest) and (c_today >= sma_today), c_today, sma_today, df["Vol_MA15"].iloc[-1]

def check_exit(df):
    if len(df) < 2: return False, None, None, None
    c, sma, vol, vma5 = df["Close"].iloc[-1], df["SMA"].iloc[-1], df["Volume"].iloc[-1], df["Vol_MA5"].iloc[-1]
    return (c < sma) and (vol >= vma5), c, sma, vol / vma5 if vma5 > 0 else 0

def analyze_one(sym, df, stock_map, pos_symbols, pos_df):
    res = {"entry": None, "addon_b": None, "addon_a": None, "exit": None, "sym": sym, "high_update": None}
    try:
        if len(df) < 40: return res
        df = calc_indicators(df.copy())
        info = stock_map.get(sym, {"code": sym.split('.')[0], "name": ""})
        c = float(df["Close"].iloc[-1])
        if sym in pos_symbols: res["high_update"] = (sym, round(c, 2))
        
        if sym not in pos_symbols and check_entry(df):
            res["entry"] = {"代號": info["code"], "名稱": info["name"], "收盤價": round(c, 2), "上軌": round(df["Upper"].iloc[-1], 2), "下軌": round(df["Lower"].iloc[-1], 2), "15MA": round(df["SMA"].iloc[-1], 2), "成交量": int(df["Volume"].iloc[-1]), "5MA量": int(df["Vol_MA5"].iloc[-1]), "symbol": sym}
        
        if sym in pos_symbols:
            mask = pos_df["symbol"] == sym
            if mask.any():
                pos_row = pos_df[mask].iloc[-1]
                ex, c_ex, sma_ex, vr = check_exit(df)
                if ex:
                    res["exit"] = {"代號": info["code"], "名稱": info["name"], "收盤價": round(c_ex, 2), "15MA": round(sma_ex, 2), "量/5MA量": f"{vr:.1f}x", "進場價": float(pos_row["進場價"]), "損益%": round((c_ex - float(pos_row["進場價"])) / float(pos_row["進場價"]) * 100, 2), "加碼次數": int(pos_row["加碼次數"]), "symbol": sym}
                    return res
                tb, c_b = check_addon_b(df, pos_row)
                if tb:
                    res["addon_b"] = {"代號": info["code"], "名稱": info["name"], "收盤價": round(c_b, 2), "持倉最高價": round(pos_row["持倉最高價"], 2), "上次加碼價": round(pos_row["上次加碼價"], 2), "距上次加碼": f"+{((c_b-pos_row['上次加碼價'])/pos_row['上次加碼價'])*100:.1f}%", "加碼次數": int(pos_row["加碼次數"]), "symbol": sym}
                ta, c_a, sma_a, vma15_a = check_addon_a(df)
                if ta:
                    res["addon_a"] = {"代號": info["code"], "名稱": info["name"], "收盤價": round(c_a, 2), "15MA": round(sma_a, 2), "成交量": int(df["Volume"].iloc[-1]), "15MA量": int(vma15_a), "加碼次數": int(pos_row["加碼次數"]), "symbol": sym}
    except: pass
    return res

# ==========================================
# 主掃描流程
# ==========================================
def run_scan(stock_map, positions):
    all_symbols = list(stock_map.keys())
    pos_symbols = positions["symbol"].tolist() if len(positions) > 0 else []
    dl_symbols  = list(set(all_symbols + pos_symbols))
    pb = st.progress(0, text="準備下載歷史資料...")
    scan_date_str = st.session_state.get("scan_date_str", "")
    all_data = fetch_all_cached(dl_symbols, period="6mo", pb=pb, scan_date=scan_date_str)
    
    entry_signals, addon_b_signals, addon_a_signals, exit_signals = [], [], [], []
    pos_df = positions.copy()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(analyze_one, sym, df, stock_map, pos_symbols, pos_df): sym for sym, df in all_data.items()}
        done, total = 0, len(futures)
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if done % 100 == 0 or done == total: pb.progress(0.9 + 0.1 * done / total, text=f"正在運算策略 {done}/{total}...")
            res = future.result()
            if res["high_update"]:
                sym_, price_ = res["high_update"]
                mask = pos_df["symbol"] == sym_
                if mask.any():
                    idx_ = pos_df[mask].index[-1]
                    if price_ > float(pos_df.loc[idx_, "持倉最高價"]): pos_df.loc[idx_, "持倉最高價"] = price_
            if res["entry"]: entry_signals.append(res["entry"])
            if res["addon_b"]: addon_b_signals.append(res["addon_b"])
            if res["addon_a"]: addon_a_signals.append(res["addon_a"])
            if res["exit"]: exit_signals.append(res["exit"])
    st.session_state["positions"] = pos_df
    pb.empty()
    return {"entry": entry_signals, "addon_b": addon_b_signals, "addon_a": addon_a_signals, "exit": exit_signals}

# ==========================================
# 介面主程式
# ==========================================
with st.sidebar:
    st.title("🐂 BULL 掃描器")
    st.caption("雲端優化版 V2.1")
    st.divider()
    scan_date = st.date_input("📅 掃描日期", value=datetime.date.today())
    st.session_state["scan_date_str"] = scan_date.strftime("%Y-%m-%d")
    
    # 在側邊欄顯示股票偵測數量，方便除錯
    stock_map = get_stock_map()
    twse_count = sum(1 for v in stock_map.values() if v['market'] == '上市')
    tpex_count = sum(1 for v in stock_map.values() if v['market'] == '上櫃')
    st.info(f"偵測到台股: {len(stock_map)} 支\n(上市: {twse_count} / 上櫃: {tpex_count})")

    if st.button("🔍 開始全域掃描", use_container_width=True, type="primary"):
        st.session_state["need_scan"] = True
    
    st.divider()
    with st.expander("⚙️ 策略參數"):
        PARAMS["boll_period"] = st.number_input("布林週期", value=15)
        PARAMS["boll_std"] = st.number_input("標準差", value=2.1, step=0.1)
        PARAMS["vol_ratio"] = st.number_input("爆量倍數", value=1.5, step=0.1)
        min_k = st.number_input("最低日均量(張)", value=1000, step=100)
        PARAMS["min_vol_shares"] = min_k * 1000

today_str = st.session_state.get("scan_date_str", datetime.date.today().strftime("%Y-%m-%d"))
positions = st.session_state["positions"]

if st.session_state.get("need_scan"):
    st.session_state["need_scan"] = False
    result = run_scan(stock_map, st.session_state["positions"])
    st.session_state["scan_result"] = result
    st.session_state["last_scan_time"] = datetime.datetime.now().strftime("%m-%d %H:%M")
    st.rerun()

result = st.session_state.get("scan_result")
st.markdown(f"## 🐂 BULL 布林滾雪球　<span style='font-size:1rem;color:#888;'>{today_str}</span>", unsafe_allow_html=True)

if not result:
    st.info("👈 點擊左側「開始掃描」執行運算。")
    st.stop()

# 顯示分頁標籤與結果
t1, t2, t3, t4, t5 = st.tabs([f"🟢 進場 ({len(result['entry'])})", f"🔵 新高加碼 ({len(result['addon_b'])})", f"🟡 回測加碼 ({len(result['addon_a'])})", f"🔴 出場 ({len(result['exit'])})", f"📋 持倉 ({len(positions)})"])

with t1:
    if not result["entry"]: st.info("無訊號")
    else:
        st.dataframe(pd.DataFrame(result["entry"]).drop(columns=['symbol']), use_container_width=True, hide_index=True)
        for r in result["entry"]:
            if st.button(f"➕ 加入 {r['代號']} {r['名稱']}", key=f"e_{r['symbol']}"):
                # 簡單實現加入邏輯
                new_row = pd.DataFrame([{"symbol": r["symbol"], "name": r["名稱"], "進場日期": today_str, "進場價": r["收盤價"], "上次加碼價": r["收盤價"], "持倉最高價": r["收盤價"], "加碼次數": 0, "加碼紀錄": ""}])
                st.session_state["positions"] = pd.concat([st.session_state["positions"], new_row], ignore_index=True)
                if use_gsheet(): gs_save(st.session_state["positions"])
                st.rerun()

with t2:
    if not result["addon_b"]: st.info("無訊號")
    else: st.dataframe(pd.DataFrame(result["addon_b"]).drop(columns=['symbol']), use_container_width=True, hide_index=True)

with t3:
    if not result["addon_a"]: st.info("無訊號")
    else: st.dataframe(pd.DataFrame(result["addon_a"]).drop(columns=['symbol']), use_container_width=True, hide_index=True)

with t4:
    if not result["exit"]: st.info("無訊號")
    else:
        for r in result["exit"]:
            st.markdown(f"<div class='signal-exit'><b>{r['代號']} {r['名稱']}</b> 損益: {r['損益%']}% | 建議出場</div>", unsafe_allow_html=True)
            if st.button(f"🚪 移除 {r['代號']}", key=f"ex_{r['symbol']}"):
                st.session_state["positions"] = st.session_state["positions"][st.session_state["positions"]["symbol"] != r["symbol"]]
                if use_gsheet(): gs_save(st.session_state["positions"])
                st.rerun()

with t5:
    st.dataframe(st.session_state["positions"], use_container_width=True, hide_index=True)
    if st.button("🗑️ 清空所有持倉"):
        st.session_state["positions"] = pd.DataFrame(columns=SHEET_COLS)
        if use_gsheet(): gs_save(st.session_state["positions"])
        st.rerun()