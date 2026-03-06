# ==========================================
# BULL 布林滾雪球選股掃描器
# Streamlit 版本
# ==========================================
import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import datetime
import logging
import plotly.graph_objects as go

# Google Sheets（可選，未設定時自動降級為 session_state）
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

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

import concurrent.futures
import requests
try:
    import twstock
    _ = twstock.twse  # 確認資料庫有載入
except Exception as _twstock_err:
    st.error(f"❌ twstock 載入失敗：{_twstock_err}\n\n請確認 requirements.txt 包含 twstock")
    st.stop()

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
# ==========================================
SHEET_COLS = ["symbol","name","進場日期","進場價","上次加碼價","持倉最高價","加碼次數","加碼紀錄"]
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]

def get_gsheet():
    """取得 Google Sheet worksheet，失敗回傳 None"""
    if not GSPREAD_AVAILABLE:
        return None
    try:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=SCOPES
        )
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(st.secrets["sheets"]["sheet_id"])
        try:
            ws = sh.worksheet("positions")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="positions", rows=1000, cols=20)
            ws.append_row(SHEET_COLS)
        return ws
    except Exception as e:
        return None

def gs_load() -> pd.DataFrame:
    """從 Google Sheets 讀取持倉，失敗回傳空 DataFrame"""
    ws = get_gsheet()
    if ws is None:
        return pd.DataFrame(columns=SHEET_COLS)
    try:
        data = ws.get_all_records()
        if not data:
            return pd.DataFrame(columns=SHEET_COLS)
        df = pd.DataFrame(data)
        df["加碼次數"] = pd.to_numeric(df["加碼次數"], errors="coerce").fillna(0).astype(int)
        df["進場價"]   = pd.to_numeric(df["進場價"],   errors="coerce").fillna(0)
        df["上次加碼價"]= pd.to_numeric(df["上次加碼價"],errors="coerce").fillna(0)
        df["持倉最高價"]= pd.to_numeric(df["持倉最高價"],errors="coerce").fillna(0)
        return df[SHEET_COLS]
    except Exception:
        return pd.DataFrame(columns=SHEET_COLS)

def gs_save(df: pd.DataFrame):
    """將整個持倉 DataFrame 寫回 Google Sheets"""
    ws = get_gsheet()
    if ws is None:
        return
    try:
        ws.clear()
        ws.append_row(SHEET_COLS)
        if len(df) > 0:
            rows = df[SHEET_COLS].fillna("").values.tolist()
            ws.append_rows(rows, value_input_option="USER_ENTERED")
    except Exception:
        pass

def use_gsheet() -> bool:
    """判斷是否啟用 Google Sheets（secrets 不存在時安全回傳 False）"""
    if not GSPREAD_AVAILABLE:
        return False
    try:
        return ("gcp_service_account" in st.secrets and
                "sheets" in st.secrets)
    except Exception:
        return False


# Session State 初始化
# ==========================================
def init_state():
    defaults = {
        "positions": pd.DataFrame(columns=[
            "symbol","name","進場日期","進場價","上次加碼價",
            "持倉最高價","加碼次數","加碼紀錄"
        ]),
        "scan_result"   : None,
        "last_scan_time": None,
        "gs_loaded"     : False,
        "need_scan"     : False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # 首次啟動從 Google Sheets 載入持倉
    if not st.session_state["gs_loaded"] and use_gsheet():
        df_gs = gs_load()
        if len(df_gs) > 0:
            st.session_state["positions"] = df_gs
        st.session_state["gs_loaded"] = True

init_state()

# ==========================================
# 股票清單
# ==========================================
# ==========================================
# 股票清單 & 資料下載（原封不動來自 ALL_v12）
# ==========================================

def is_trading_hours():
    """判斷目前是否在台股交易時段 (09:00~13:30)"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # 週六日
        return False
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close

def get_cache_ttl():
    return 300 if is_trading_hours() else 3600

@st.cache_data(ttl=3600)
def get_stock_info_map():
    try:
        stock_map = {}
        for code, info in twstock.twse.items():
            if len(code) == 4:
                stock_map[code] = {
                    'name': f"{code} {info.name}",
                    'symbol': f"{code}.TW",
                    'short_name': info.name,
                    'group': getattr(info, 'group', '其他')
                }
        for code, info in twstock.tpex.items():
            if len(code) == 4:
                stock_map[code] = {
                    'name': f"{code} {info.name}",
                    'symbol': f"{code}.TWO",
                    'short_name': info.name,
                    'group': getattr(info, 'group', '其他')
                }
        return stock_map
    except Exception as e:
        logger.error(f"get_stock_info_map 錯誤: {e}")
        return {}

@st.cache_data(ttl=get_cache_ttl(), show_spinner=False)
def fetch_history_data(symbol, start_date=None, end_date=None, period="2y"):
    try:
        ticker = yf.Ticker(symbol)
        if start_date and end_date:
            df = ticker.history(start=start_date, end=end_date)
        else:
            df = ticker.history(period=period)
        if df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    except Exception as e:
        logger.warning(f"fetch_history_data 錯誤 [{symbol}]: {e}")
        return None

def get_stock_data_with_realtime(code, symbol, analysis_date_str):
    df = fetch_history_data(symbol)
    if df is None or df.empty:
        return None

    last_dt = df.index[-1].strftime('%Y-%m-%d')
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')

    if analysis_date_str == today_str and last_dt != today_str:
        try:
            realtime = twstock.realtime.get(code)
            if realtime['success'] and realtime['realtime']['latest_trade_price'] != '-':
                rt = realtime['realtime']
                new_row = pd.Series({
                    'Open': float(rt['open']), 'High': float(rt['high']),
                    'Low': float(rt['low']), 'Close': float(rt['latest_trade_price']),
                    'Volume': float(rt['accumulate_trade_volume']) * 1000
                }, name=pd.Timestamp(today_str))
                df = pd.concat([df, new_row.to_frame().T])
        except Exception as e:
            logger.warning(f"get_stock_data_with_realtime 即時資料錯誤 [{code}]: {e}")
    return df

def fetch_data_batch(stock_map, period="300d", chunk_size=150):
    all_symbols = [info['symbol'] for info in stock_map.values()]
    data_store = {}
    symbol_to_code = {v['symbol']: k for k, v in stock_map.items()}

    total_chunks = (len(all_symbols) // chunk_size) + 1
    progress_text = st.empty()
    bar = st.progress(0)

    for i in range(0, len(all_symbols), chunk_size):
        chunk = all_symbols[i:i + chunk_size]
        if not chunk:
            continue

        chunk_idx = (i // chunk_size) + 1
        progress_text.text(f"📥 正在批量下載歷史資料... (批次 {chunk_idx}/{total_chunks})")
        bar.progress(chunk_idx / total_chunks)

        try:
            tickers_str = " ".join(chunk)
            batch_df = yf.download(tickers_str, period=period, group_by='ticker', threads=True, auto_adjust=True, progress=False)

            if not batch_df.empty:
                if isinstance(batch_df.columns, pd.MultiIndex):
                    for symbol in chunk:
                        try:
                            if symbol in batch_df:
                                stock_df = batch_df[symbol].dropna()
                                if not stock_df.empty:
                                    if stock_df.index.tz is not None:
                                        stock_df.index = stock_df.index.tz_localize(None)
                                    code = symbol_to_code.get(symbol)
                                    if code:
                                        data_store[code] = stock_df
                        except Exception as e:
                            logger.warning(f"批次解析錯誤 [{symbol}]: {e}")
                else:
                    symbol = chunk[0]
                    stock_df = batch_df.dropna()
                    if not stock_df.empty:
                        if stock_df.index.tz is not None:
                            stock_df.index = stock_df.index.tz_localize(None)
                        code = symbol_to_code.get(symbol)
                        if code:
                            data_store[code] = stock_df
            time.sleep(1)
        except Exception as e:
            logger.error(f"批次下載錯誤 (batch {chunk_idx}): {e}")
            st.toast(f"批次下載錯誤: {e}")
            continue

    progress_text.empty()
    bar.empty()
    return data_store

def fetch_realtime_batch(codes_list, chunk_size=50):
    realtime_data = {}
    progress_text = st.empty()

    for i in range(0, len(codes_list), chunk_size):
        chunk = codes_list[i:i + chunk_size]
        progress_text.text(f"⚡ 正在批量更新即時盤... ({i}/{len(codes_list)})")
        try:
            stocks = twstock.realtime.get(chunk)
            if stocks:
                if 'success' in stocks:
                    if stocks['success']:
                        realtime_data[stocks['info']['code']] = stocks['realtime']
                else:
                    for code, data in stocks.items():
                        if data['success']:
                            realtime_data[code] = data['realtime']
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"fetch_realtime_batch 錯誤 (offset {i}): {e}")
    progress_text.empty()
    return realtime_data


# ==========================================
# 單支股票策略運算（供 ThreadPool 使用）
# ==========================================
def analyze_one(sym, df, stock_map, pos_symbols, pos_df):
    """單支股票的指標計算與訊號判斷，回傳結果 dict"""
    result = {"entry": None, "addon_b": None, "addon_a": None, "exit": None,
              "sym": sym, "high_update": None}
    try:
        if len(df) < 40:
            return result
        df   = calc_indicators(df.copy())
        info = stock_map.get(sym, {"code": sym.replace(".TW","").replace(".TWO",""), "name": ""})
        c    = float(df["Close"].iloc[-1])

        # 更新最高價
        if sym in pos_symbols:
            result["high_update"] = (sym, round(c, 2))

        # 進場
        if sym not in pos_symbols and check_entry(df):
            result["entry"] = {
                "代號"   : info["code"], "名稱": info["name"],
                "收盤價" : round(c, 2),
                "上軌"   : round(float(df["Upper"].iloc[-1]), 2),
                "下軌"   : round(float(df["Lower"].iloc[-1]), 2),
                "15MA"   : round(float(df["SMA"].iloc[-1]), 2),
                "成交量" : int(df["Volume"].iloc[-1]),
                "5MA量"  : int(df["Vol_MA5"].iloc[-1]),
                "15MA量" : int(df["Vol_MA15"].iloc[-1]),
                "symbol" : sym,             }

        if sym in pos_symbols and len(pos_df) > 0:
            mask = pos_df["symbol"] == sym
            if not mask.any():
                return result
            pos_row = pos_df[mask].iloc[-1]

            # 出場優先
            ex, c_ex, sma_ex, vr = check_exit(df)
            if ex:
                result["exit"] = {
                    "代號"     : info["code"], "名稱": info["name"],
                    "收盤價"   : round(c_ex, 2), "15MA": round(sma_ex, 2),
                    "量/5MA量" : f"{vr:.1f}x",
                    "進場價"   : float(pos_row["進場價"]),
                    "損益%"    : round((c_ex - float(pos_row["進場價"])) / float(pos_row["進場價"]) * 100, 2),
                    "加碼次數" : int(pos_row["加碼次數"]),
                    "symbol"   : sym,                 }
                return result

            # 加碼B
            tb, c_b, last_p, profit = check_addon_b(df, pos_row)
            if tb:
                result["addon_b"] = {
                    "代號"       : info["code"], "名稱": info["name"],
                    "收盤價"     : round(c_b, 2),
                    "持倉最高價" : round(float(pos_row["持倉最高價"]), 2),
                    "上次加碼價" : round(last_p, 2),
                    "距上次加碼" : f"+{profit*100:.1f}%",
                    "加碼次數"   : int(pos_row["加碼次數"]),
                    "symbol"     : sym,                 }

            # 加碼A
            ta, c_a, sma_a, vma15_a = check_addon_a(df)
            if ta:
                result["addon_a"] = {
                    "代號"    : info["code"], "名稱": info["name"],
                    "收盤價"  : round(c_a, 2), "15MA": round(sma_a, 2),
                    "成交量"  : int(df["Volume"].iloc[-1]),
                    "15MA量"  : int(vma15_a) if vma15_a else 0,
                    "加碼次數": int(pos_row["加碼次數"]),
                    "symbol"  : sym,                 }
    except Exception:
        pass
    return result

# ==========================================
# 主掃描
# ==========================================
def run_scan(stock_map, positions):
    """
    stock_map 格式（來自 get_stock_info_map）：
      {"2330": {"symbol": "2330.TW", "name": "...", "short_name": "...", "group": "..."}}
    fetch_data_batch 回傳：
      {"2330": df, ...}  ← key 是 code
    """
    pos_symbols = positions["symbol"].tolist() if len(positions) > 0 else []
    # 把持倉的 symbol 轉成 code 加入掃描
    pos_codes = [s.replace(".TW","").replace(".TWO","") for s in pos_symbols]
    # 確保持倉股也在 stock_map 裡
    for sym in pos_symbols:
        c = sym.replace(".TW","").replace(".TWO","")
        if c not in stock_map:
            stock_map[c] = {"symbol": sym, "name": c, "short_name": c, "group": "其他"}

    scan_date_str = st.session_state.get("scan_date_str", "")

    # 用 v12 原版 fetch_data_batch 下載（300d 足夠算指標）
    all_data = fetch_data_batch(stock_map, period="300d")

    # 依掃描日期截斷
    if scan_date_str:
        cutoff   = pd.Timestamp(scan_date_str)
        all_data = {c: df[df.index <= cutoff] for c, df in all_data.items()
                    if len(df[df.index <= cutoff]) >= 40}

    pb = st.progress(0, text=f"✅ 下載完成 {len(all_data)} 支，策略運算中...")

    entry_signals   = []
    addon_b_signals = []
    addon_a_signals = []
    exit_signals    = []
    pos_df          = positions.copy()

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(analyze_one,
                            stock_map[c]["symbol"],   # sym = "2330.TW"
                            df,
                            stock_map,
                            pos_symbols,
                            pos_df): c
            for c, df in all_data.items() if c in stock_map
        }
        done  = 0
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if done % 100 == 0 or done == total:
                pb.progress(done / total, text=f"策略運算 {done}/{total}...")
            res = future.result()
            if res["high_update"]:
                sym_, price_ = res["high_update"]
                mask = pos_df["symbol"] == sym_
                if mask.any():
                    idx_ = pos_df[mask].index[-1]
                    if price_ > float(pos_df.loc[idx_, "持倉最高價"]):
                        pos_df.loc[idx_, "持倉最高價"] = price_
            if res["entry"]  : entry_signals.append(res["entry"])
            if res["addon_b"]: addon_b_signals.append(res["addon_b"])
            if res["addon_a"]: addon_a_signals.append(res["addon_a"])
            if res["exit"]   : exit_signals.append(res["exit"])

    st.session_state["positions"] = pos_df
    pb.empty()
    return {"entry": entry_signals, "addon_b": addon_b_signals,
            "addon_a": addon_a_signals, "exit": exit_signals}


# ==========================================
# 持倉操作
# ==========================================
def add_position(sym, name, price, today_str):
    pos = st.session_state["positions"]
    if sym in pos["symbol"].values:
        return
    new_row = pd.DataFrame([{
        "symbol": sym, "name": name, "進場日期": today_str,
        "進場價": round(price, 2), "上次加碼價": round(price, 2),
        "持倉最高價": round(price, 2), "加碼次數": 0, "加碼紀錄": "",
    }])
    updated = pd.concat([pos, new_row], ignore_index=True)
    st.session_state["positions"] = updated
    if use_gsheet():
        gs_save(updated)

def do_addon(sym, addon_type, price, today_str):
    pos  = st.session_state["positions"]
    mask = pos["symbol"] == sym
    if not mask.any():
        return
    idx  = pos[mask].index[-1]
    pos.loc[idx, "上次加碼價"] = round(price, 2)
    pos.loc[idx, "加碼次數"]   = int(pos.loc[idx, "加碼次數"]) + 1
    tag  = f"{today_str}({addon_type})"
    prev = str(pos.loc[idx, "加碼紀錄"])
    pos.loc[idx, "加碼紀錄"] = (prev + " → " + tag).strip(" → ") if prev else tag
    st.session_state["positions"] = pos
    if use_gsheet():
        gs_save(pos)

def remove_position(sym):
    pos     = st.session_state["positions"]
    updated = pos[pos["symbol"] != sym].reset_index(drop=True)
    st.session_state["positions"] = updated
    if use_gsheet():
        gs_save(updated)

# ==========================================
# Sidebar
# ==========================================
with st.sidebar:
    st.title("🐂 BULL 掃描器")
    st.markdown("---")

    # 掃描日期
    scan_date = st.date_input("📅 掃描日期", value=datetime.date.today())
    scan_date_str = scan_date.strftime("%Y-%m-%d")
    st.session_state["scan_date_str"] = scan_date_str

    # 開始掃描按鈕
    if st.button("🔍 開始掃描", use_container_width=True, type="primary"):
        st.session_state["need_scan"] = True
    if st.session_state["last_scan_time"]:
        st.caption(f"上次掃描：{st.session_state['last_scan_time']}")

    st.markdown("---")

    # Google Sheets 狀態
    if use_gsheet():
        st.success("🟢 Google Sheets 已連線")
    elif GSPREAD_AVAILABLE:
        st.warning("⚠️ 未設定 Sheets Secrets，使用暫存模式")
    else:
        st.info("ℹ️ 未安裝 gspread，使用暫存模式")

    st.markdown("---")
    st.markdown("**策略：布林滾雪球**")
    st.caption("📌 進場：布林壓縮突破上軌＋爆量")
    st.caption("🔵 加碼B：突破新高＋距上次≥10%")
    st.caption("🟡 加碼A：量縮回測15MA站回")
    st.caption("🔴 出場：出量跌破15MA / 跌破下軌")
    st.markdown("---")
    with st.expander("⚙️ 參數設定", expanded=False):
        PARAMS["boll_period"]    = st.number_input("布林週期",        value=15, min_value=5,   max_value=60)
        PARAMS["boll_std"]       = st.number_input("布林標準差",       value=2.1,min_value=1.0, max_value=3.0, step=0.1)
        PARAMS["squeeze_n"]      = st.number_input("壓縮判斷天數",     value=15, min_value=5,   max_value=60)
        PARAMS["vol_ratio"]      = st.number_input("爆量倍數",         value=1.5,min_value=1.0, max_value=5.0, step=0.1)
        PARAMS["addon_b_profit"] = st.number_input("加碼B門檻(%)",     value=10, min_value=1,   max_value=50) / 100
        min_k = st.number_input("最低日均量(張)", value=1000, min_value=100, step=100)
        PARAMS["min_vol_shares"] = min_k * 1000

# ==========================================
# 主內容
# ==========================================
today_str = st.session_state.get("scan_date_str", datetime.date.today().strftime("%Y-%m-%d"))
stock_map = get_stock_info_map()
positions = st.session_state["positions"]

if st.session_state["need_scan"]:
    st.session_state["need_scan"] = False
    result = run_scan(stock_map, st.session_state["positions"])
    st.session_state["scan_result"]    = result
    st.session_state["last_scan_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    st.rerun()

result = st.session_state["scan_result"]

# 頂部標題 + 指標卡
st.markdown(f"## 🐂 BULL 布林滾雪球　<span style='font-size:1rem;color:#888;'>{today_str}</span>",
            unsafe_allow_html=True)

c1, c2, c3, c4, c5 = st.columns(5)
with c1: st.metric("📋 持倉數",    len(positions))
with c2: st.metric("🟢 今日進場",  len(result["entry"])   if result else 0)
with c3: st.metric("🔵 創新高加碼",len(result["addon_b"]) if result else 0)
with c4: st.metric("🟡 回測加碼",  len(result["addon_a"]) if result else 0)
with c5: st.metric("🔴 出場訊號",  len(result["exit"])    if result else 0)

st.markdown("---")

if not result:
    st.info("👈 點擊左側「開始掃描」執行掃描")
    st.stop()

entry_n  = len(result["entry"])
addon_b_n= len(result["addon_b"])
addon_a_n= len(result["addon_a"])
exit_n   = len(result["exit"])
pos_n    = len(st.session_state["positions"])

# ==========================================
# Tabs
# ==========================================
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    f"🟢 今日進場 ({entry_n})",
    f"🔵 創新高加碼 ({addon_b_n})",
    f"🟡 回測加碼 ({addon_a_n})",
    f"🔴 出場訊號 ({exit_n})",
    f"📋 持倉管理 ({pos_n})",
])

# ---------- Tab1 進場 ----------
with tab1:
    st.subheader("🟢 今日進場訊號")
    if not result["entry"]:
        st.info("今日無進場訊號")
    else:
        cols_show = ["代號","名稱","收盤價","上軌","下軌","15MA","成交量","5MA量","15MA量"]
        df_show   = pd.DataFrame([{k: r[k] for k in cols_show} for r in result["entry"]])
        st.dataframe(df_show, use_container_width=True, hide_index=True,
                     column_config={
                         "成交量": st.column_config.NumberColumn(format="%d"),
                         "5MA量" : st.column_config.NumberColumn(format="%d"),
                         "15MA量": st.column_config.NumberColumn(format="%d"),
                     })
        st.markdown("**加入持倉：**")
        for r in result["entry"]:
            a, b, cc = st.columns([2, 7, 3])
            already  = r["symbol"] in st.session_state["positions"]["symbol"].values
            with a:
                if already:
                    st.success("✅ 已持倉")
                elif st.button("➕ 加入持倉", key=f"e_{r['symbol']}"):
                    add_position(r["symbol"], r["名稱"], r["收盤價"], today_str)
                    st.rerun()
            with b:
                st.markdown(f"**{r['代號']} {r['名稱']}**　收盤 `{r['收盤價']}`　上軌 `{r['上軌']}`　下軌 `{r['下軌']}`")
            with cc:
                st.caption(f"量/5MA = {r['成交量']/r['5MA量']:.1f}x")


# ---------- Tab2 加碼B ----------
with tab2:
    st.subheader("🔵 創新高加碼訊號（突破新高 且 距上次加碼 ≥ 10%）")
    if not result["addon_b"]:
        st.info("今日無創新高加碼訊號")
    else:
        cols_show = ["代號","名稱","收盤價","持倉最高價","上次加碼價","距上次加碼","加碼次數"]
        st.dataframe(pd.DataFrame([{k: r[k] for k in cols_show} for r in result["addon_b"]]),
                     use_container_width=True, hide_index=True)
        st.markdown("**確認加碼：**")
        for r in result["addon_b"]:
            a, b = st.columns([2, 8])
            with a:
                if st.button("➕ 加碼", key=f"b_{r['symbol']}"):
                    do_addon(r["symbol"], "突破新高", r["收盤價"], today_str)
                    st.success(f"✅ {r['代號']} 加碼記錄完成")
                    st.rerun()
            with b:
                st.markdown(f"**{r['代號']} {r['名稱']}**　"
                            f"收盤 `{r['收盤價']}`　持倉最高 `{r['持倉最高價']}`　"
                            f"上次加碼 `{r['上次加碼價']}`　"
                            f"<span style='color:#00aaff;font-weight:bold;font-size:1.1rem;'>{r['距上次加碼']}</span>",
                            unsafe_allow_html=True)


# ---------- Tab3 加碼A ----------
with tab3:
    st.subheader("🟡 回測加碼訊號（昨日量縮跌破15MA → 今日站回）")
    if not result["addon_a"]:
        st.info("今日無回測加碼訊號")
    else:
        cols_show = ["代號","名稱","收盤價","15MA","成交量","15MA量","加碼次數"]
        st.dataframe(pd.DataFrame([{k: r[k] for k in cols_show} for r in result["addon_a"]]),
                     use_container_width=True, hide_index=True,
                     column_config={
                         "成交量" : st.column_config.NumberColumn(format="%d"),
                         "15MA量": st.column_config.NumberColumn(format="%d"),
                     })
        st.markdown("**確認加碼：**")
        for r in result["addon_a"]:
            a, b = st.columns([2, 8])
            with a:
                if st.button("➕ 加碼", key=f"a_{r['symbol']}"):
                    do_addon(r["symbol"], "回測站回", r["收盤價"], today_str)
                    st.success(f"✅ {r['代號']} 加碼記錄完成")
                    st.rerun()
            with b:
                ratio = r["成交量"] / r["15MA量"] if r["15MA量"] > 0 else 0
                st.markdown(f"**{r['代號']} {r['名稱']}**　"
                            f"收盤 `{r['收盤價']}`　15MA `{r['15MA']}`　量/15MA量 = `{ratio:.2f}x`")


# ---------- Tab4 出場 ----------
with tab4:
    st.subheader("🔴 出場訊號（出量跌破15MA）")
    if not result["exit"]:
        st.info("今日無出場訊號")
    else:
        st.warning("⚠️ 以下標的出現出量跌破15MA，請確認是否出場")
        for r in result["exit"]:
            color = "#ff4444" if r["損益%"] < 0 else "#00cc44"
            st.markdown(f"""
            <div class="signal-exit">
                <b style='font-size:1.05rem;'>{r['代號']} {r['名稱']}</b>　
                收盤 <b>{r['收盤價']}</b>　15MA <b>{r['15MA']}</b>　
                量/5MA = <b>{r['量/5MA量']}</b>　
                進場價 {r['進場價']}　
                損益 <span style='color:{color};font-weight:bold;font-size:1.05rem;'>{r['損益%']:+.2f}%</span>　
                已加碼 {r['加碼次數']} 次
            </div>
            """, unsafe_allow_html=True)

        st.markdown("")
        ex_cols = st.columns(min(len(result["exit"]), 6))
        for i, r in enumerate(result["exit"]):
            with ex_cols[i % 6]:
                if st.button(f"🚪 出場 {r['代號']}", key=f"exit_{r['symbol']}"):
                    remove_position(r["symbol"])
                    st.success(f"✅ {r['代號']} 已移除持倉")
                    st.rerun()


# ---------- Tab5 持倉管理 ----------
with tab5:
    st.subheader("📋 持倉管理")
    positions = st.session_state["positions"]

    if len(positions) == 0:
        st.info("目前無持倉。掃描後在「今日進場」分頁點擊加入持倉。")
    else:
        display = positions[["symbol","name","進場日期","進場價","上次加碼價","持倉最高價","加碼次數","加碼紀錄"]].copy()
        display.columns = ["代號","名稱","進場日期","進場價","上次加碼價","持倉最高價","加碼次數","加碼紀錄"]
        st.dataframe(display, use_container_width=True, hide_index=True,
                     column_config={
                         "進場價"   : st.column_config.NumberColumn(format="%.2f"),
                         "上次加碼價": st.column_config.NumberColumn(format="%.2f"),
                         "持倉最高價": st.column_config.NumberColumn(format="%.2f"),
                     })
        st.markdown("---")
        st.markdown("**手動出場：**")
        btn_cols = st.columns(min(len(positions), 8))
        for i, (_, row) in enumerate(positions.iterrows()):
            with btn_cols[i % 8]:
                label = str(row["symbol"]).replace(".TW","").replace(".TWO","")
                if st.button(f"🚪 {label}", key=f"m_exit_{row['symbol']}"):
                    remove_position(row["symbol"])
                    st.rerun()
        st.markdown("---")
        # 匯出
        csv = positions.to_csv(index=False, encoding="utf-8-sig")
        st.download_button("⬇️ 匯出持倉 CSV", data=csv,
                           file_name=f"BULL_positions_{today_str}.csv", mime="text/csv")
        # 匯入
        st.markdown("**匯入持倉 CSV（恢復上次紀錄）：**")
        uploaded = st.file_uploader("上傳 CSV", type="csv", key="pos_upload")
        if uploaded:
            try:
                df_imp = pd.read_csv(uploaded, encoding="utf-8-sig")
                st.session_state["positions"] = df_imp
                st.success(f"✅ 已匯入 {len(df_imp)} 筆持倉")
                st.rerun()
            except Exception as e:
                st.error(f"匯入失敗：{e}")
