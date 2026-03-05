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
from plotly.subplots import make_subplots

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

try:
    import twstock
except ImportError:
    st.error("❌ 缺少 `twstock` 套件，請執行 `pip install twstock`")
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
@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_map():
    stock_map = {}
    for code, info in twstock.twse.items():
        if len(code) == 4 and code.isdigit():
            stock_map[f"{code}.TW"] = {"code": code, "name": info.name}
    for code, info in twstock.tpex.items():
        if len(code) == 4 and code.isdigit():
            stock_map[f"{code}.TWO"] = {"code": code, "name": info.name}
    return stock_map

# ==========================================
# 指標計算
# ==========================================
def calc_indicators(df):
    close  = df["Close"]
    volume = df["Volume"]
    bp     = PARAMS["boll_period"]
    df["SMA"]       = close.rolling(bp).mean()
    df["Std"]       = close.rolling(bp).std()
    df["Upper"]     = df["SMA"] + df["Std"] * PARAMS["boll_std"]
    df["Lower"]     = df["SMA"] - df["Std"] * PARAMS["boll_std"]
    df["Bandwidth"] = df["Upper"] - df["Lower"]
    df["Vol_MA5"]   = volume.rolling(PARAMS["vol_ma_days"]).mean()
    df["Vol_MA15"]  = volume.rolling(PARAMS["vol_shrink_days"]).mean()
    df["Vol_MA20"]  = volume.rolling(PARAMS["vol_ma20_days"]).mean()
    sq_n = PARAMS["squeeze_n"]
    df["BW_Min"]         = df["Bandwidth"].rolling(sq_n).min()
    df["Is_Squeeze"]     = df["Bandwidth"] == df["BW_Min"]
    lb = PARAMS["squeeze_lookback"]
    df["Squeeze_Recent"] = df["Is_Squeeze"].shift(1).rolling(lb).max() == 1
    df["SMA_Up"]         = df["SMA"] > df["SMA"].shift(PARAMS["sma_trend_days"])
    return df

# ==========================================
# 訊號判斷
# ==========================================
def check_entry(df):
    if len(df) < PARAMS["boll_period"] + PARAMS["squeeze_n"]:
        return False
    row = df.iloc[-1]
    return (
        bool(row["Squeeze_Recent"]) and
        float(row["Close"]) > float(row["Upper"]) and
        float(row["Volume"]) > float(row["Vol_MA5"]) * PARAMS["vol_ratio"] and
        bool(row["SMA_Up"]) and
        float(row["Vol_MA20"]) > PARAMS["min_vol_shares"]
    )

def check_addon_b(df, pos_row):
    c            = float(df["Close"].iloc[-1])
    pos_high     = float(pos_row["持倉最高價"])
    last_p       = float(pos_row["上次加碼價"])
    profit       = (c - last_p) / last_p
    return (c > pos_high) and (profit >= PARAMS["addon_b_profit"]), c, last_p, profit

def check_addon_a(df):
    if len(df) < 3:
        return False, None, None, None
    c_today    = float(df["Close"].iloc[-1])
    c_yest     = float(df["Close"].iloc[-2])
    sma_today  = float(df["SMA"].iloc[-1])
    sma_yest   = float(df["SMA"].iloc[-2])
    vol_yest   = float(df["Volume"].iloc[-2])
    vma15_yest = float(df["Vol_MA15"].iloc[-2])
    triggered  = (c_yest < sma_yest) and (vol_yest < vma15_yest) and (c_today >= sma_today)
    return triggered, c_today, sma_today, float(df["Vol_MA15"].iloc[-1])

def check_exit(df):
    if len(df) < 2:
        return False, None, None, None
    c    = float(df["Close"].iloc[-1])
    sma  = float(df["SMA"].iloc[-1])
    vol  = float(df["Volume"].iloc[-1])
    vma5 = float(df["Vol_MA5"].iloc[-1])
    return (c < sma) and (vol >= vma5), c, sma, vol / vma5 if vma5 > 0 else 0

# ==========================================
# 批量下載
# ==========================================
def fetch_all(symbols, period="6mo", pb=None):
    all_data   = {}
    chunk_size = 100
    chunks     = [symbols[i:i+chunk_size] for i in range(0, len(symbols), chunk_size)]
    for idx, batch in enumerate(chunks, 1):
        if pb:
            pb.progress(idx / len(chunks), text=f"下載中 {idx}/{len(chunks)} 批...")
        try:
            raw = yf.download(batch, period=period, progress=False,
                              group_by="ticker", threads=True, auto_adjust=True)
            if len(batch) == 1:
                sym = batch[0]
                if not raw.empty:
                    df = raw.copy()
                    if isinstance(df.columns, pd.MultiIndex):
                        df = df.droplevel(1, axis=1)
                    df.index = pd.to_datetime(df.index)
                    if df.index.tz is not None:
                        df.index = df.index.tz_localize(None)
                    all_data[sym] = df
            else:
                for sym in batch:
                    try:
                        df = raw[sym].dropna(how="all")
                        if df.empty:
                            continue
                        df.index = pd.to_datetime(df.index)
                        if df.index.tz is not None:
                            df.index = df.index.tz_localize(None)
                        all_data[sym] = df
                    except Exception:
                        continue
        except Exception:
            continue
    return all_data

# ==========================================
# 主掃描
# ==========================================
def run_scan(stock_map, positions):
    all_symbols = list(stock_map.keys())
    pos_symbols = positions["symbol"].tolist() if len(positions) > 0 else []
    dl_symbols  = list(set(all_symbols + pos_symbols))

    pb       = st.progress(0, text="準備下載...")
    all_data = fetch_all(dl_symbols, period="6mo", pb=pb)
    pb.progress(1.0, text=f"✅ 下載完成 {len(all_data)} 支")

    entry_signals  = []
    addon_b_signals = []
    addon_a_signals = []
    exit_signals   = []
    pos_df         = positions.copy()

    for sym, df in all_data.items():
        if len(df) < 40:
            continue
        try:
            df   = calc_indicators(df.copy())
            info = stock_map.get(sym, {"code": sym.replace(".TW","").replace(".TWO",""), "name": ""})
            c    = float(df["Close"].iloc[-1])

            # 更新持倉最高價
            if sym in pos_symbols and len(pos_df) > 0:
                mask = pos_df["symbol"] == sym
                if mask.any():
                    idx_ = pos_df[mask].index[-1]
                    if c > float(pos_df.loc[idx_, "持倉最高價"]):
                        pos_df.loc[idx_, "持倉最高價"] = round(c, 2)

            # 進場
            if sym not in pos_symbols and check_entry(df):
                entry_signals.append({
                    "代號"   : info["code"], "名稱": info["name"],
                    "收盤價" : round(c, 2),
                    "上軌"   : round(float(df["Upper"].iloc[-1]), 2),
                    "下軌"   : round(float(df["Lower"].iloc[-1]), 2),
                    "15MA"   : round(float(df["SMA"].iloc[-1]), 2),
                    "成交量" : int(df["Volume"].iloc[-1]),
                    "5MA量"  : int(df["Vol_MA5"].iloc[-1]),
                    "15MA量" : int(df["Vol_MA15"].iloc[-1]),
                    "symbol" : sym, "_df": df,
                })

            if sym in pos_symbols and len(pos_df) > 0:
                mask    = pos_df["symbol"] == sym
                if not mask.any():
                    continue
                pos_row = pos_df[mask].iloc[-1]

                # 出場優先
                ex, c_ex, sma_ex, vr = check_exit(df)
                if ex:
                    exit_signals.append({
                        "代號"     : info["code"], "名稱": info["name"],
                        "收盤價"   : round(c_ex, 2), "15MA": round(sma_ex, 2),
                        "量/5MA量" : f"{vr:.1f}x",
                        "進場價"   : float(pos_row["進場價"]),
                        "損益%"    : round((c_ex - float(pos_row["進場價"])) / float(pos_row["進場價"]) * 100, 2),
                        "加碼次數" : int(pos_row["加碼次數"]),
                        "symbol"   : sym, "_df": df,
                    })
                    continue

                # 加碼B
                tb, c_b, last_p, profit = check_addon_b(df, pos_row)
                if tb:
                    addon_b_signals.append({
                        "代號"       : info["code"], "名稱": info["name"],
                        "收盤價"     : round(c_b, 2),
                        "持倉最高價" : round(float(pos_row["持倉最高價"]), 2),
                        "上次加碼價" : round(last_p, 2),
                        "距上次加碼" : f"+{profit*100:.1f}%",
                        "加碼次數"   : int(pos_row["加碼次數"]),
                        "symbol"     : sym, "_df": df,
                    })

                # 加碼A
                ta, c_a, sma_a, vma15_a = check_addon_a(df)
                if ta:
                    addon_a_signals.append({
                        "代號"    : info["code"], "名稱": info["name"],
                        "收盤價"  : round(c_a, 2), "15MA": round(sma_a, 2),
                        "成交量"  : int(df["Volume"].iloc[-1]),
                        "15MA量"  : int(vma15_a) if vma15_a else 0,
                        "加碼次數": int(pos_row["加碼次數"]),
                        "symbol"  : sym, "_df": df,
                    })

        except Exception:
            continue

    st.session_state["positions"] = pos_df
    pb.empty()
    return {"entry": entry_signals, "addon_b": addon_b_signals,
            "addon_a": addon_a_signals, "exit": exit_signals}

# ==========================================
# K線圖
# ==========================================
def plot_kline(df, sym, title=""):
    df  = df.tail(60).copy()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"],
        increasing_line_color="red", decreasing_line_color="green", name="K線"
    ), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["Upper"],
        line=dict(color="rgba(100,180,255,0.6)", width=1), name="上軌"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["SMA"],
        line=dict(color="rgba(255,200,0,0.9)", width=1.5), name="15MA"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["Lower"],
        line=dict(color="rgba(100,180,255,0.6)", width=1),
        fill="tonexty", fillcolor="rgba(100,180,255,0.05)", name="下軌"), row=1, col=1)
    colors = ["red" if float(df["Close"].iloc[i]) >= float(df["Open"].iloc[i]) else "green"
              for i in range(len(df))]
    fig.add_trace(go.Bar(x=df.index, y=df["Volume"],
        marker_color=colors, name="成交量", opacity=0.7), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["Vol_MA5"],
        line=dict(color="yellow", width=1), name="5MA量"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["Vol_MA15"],
        line=dict(color="cyan", width=1), name="15MA量"), row=2, col=1)
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, color="white")),
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        font=dict(color="white"), xaxis_rangeslider_visible=False,
        height=420, margin=dict(l=10, r=10, t=36, b=10),
        legend=dict(orientation="h", y=1.02, font=dict(size=10)),
    )
    fig.update_xaxes(gridcolor="#2a2a2a")
    fig.update_yaxes(gridcolor="#2a2a2a")
    return fig

# ==========================================
# Google Sheets 連線
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

    # 開始掃描按鈕
    scan_btn = st.button("🔍 開始掃描", use_container_width=True, type="primary")
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
today_str = scan_date_str if "scan_date_str" in dir() else datetime.date.today().strftime("%Y-%m-%d")
stock_map = get_stock_map()
positions = st.session_state["positions"]

if scan_btn:
    result = run_scan(stock_map, st.session_state["positions"])
    st.session_state["scan_result"]    = result
    st.session_state["last_scan_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    st.rerun()

result = st.session_state["scan_result"]

# 頂部標題 + 指標卡
st.markdown(f"## 🐂 BULL 布林滾雪球掃描器　<span style='font-size:1rem;color:#888;'>{today_str}</span>",
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
        st.markdown("---")
        for r in result["entry"]:
            with st.expander(f"📈 {r['代號']} {r['名稱']} K線"):
                st.plotly_chart(plot_kline(r["_df"], r["symbol"], f"{r['代號']} {r['名稱']}"),
                                use_container_width=True)

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
        st.markdown("---")
        for r in result["addon_b"]:
            with st.expander(f"📈 {r['代號']} {r['名稱']} K線"):
                st.plotly_chart(plot_kline(r["_df"], r["symbol"], f"{r['代號']} {r['名稱']}"),
                                use_container_width=True)

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
        st.markdown("---")
        for r in result["addon_a"]:
            with st.expander(f"📈 {r['代號']} {r['名稱']} K線"):
                st.plotly_chart(plot_kline(r["_df"], r["symbol"], f"{r['代號']} {r['名稱']}"),
                                use_container_width=True)

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
        st.markdown("---")
        for r in result["exit"]:
            with st.expander(f"📈 {r['代號']} {r['名稱']} K線"):
                st.plotly_chart(plot_kline(r["_df"], r["symbol"], f"{r['代號']} {r['名稱']}"),
                                use_container_width=True)

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
