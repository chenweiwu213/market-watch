"""美股盤前盤後看盤頁：指數、期貨、TSM ADR、比特幣、布蘭特原油

資料來源：Yahoo Finance(yfinance)
本基端開啟：python -m streamlit run market_watch.py
"""

from __future__ import annotations
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import altair as alt
import pandas as pd
import streamlit as st
import yfinance as yf

TZ_TW = ZoneInfo("Asia/Taipei")
TZ_NY = ZoneInfo("America/New_York")

STALE_MINUTES = 60  # 最新一筆資料超過此分鐘數，視為休市或來源無新資料
CACHE_TTL_SECONDS = 50  # 快取秒數，需小於最短自動更新間隔(1分鐘)
CHART_HOURS = 36  # 走勢小圖顯示最近幾小時
CHART_HEIGHT = 280  # 走勢圖高度(像素)
GAP_MINUTES = 30  # 相鄰兩筆資料間隔超過此分鐘數，視為休市：x軸拿掉休市時間，用垂直虛線分隔
UP_COLOR, DOWN_COLOR, FLAT_COLOR = "#ff6b6b", "#51cf66", "#74a9ff"  # 紅漲綠跌、無昨收時

# 圖表座標配色(深色主題)
BORDER, MUTED = "#2b3242", "#9aa4b2"

# kind 說明：
#   index     現貨指數，只有正常盤(美東 09:30–16:00)
#   us_future 美股指數期貨，近 24 小時交易，依美東時間標示盤前／盤中／盤後／夜盤
#   us_equity 美股個股或 ETF，盤前 04:00、正常盤 09:30、盤後 16:00–20:00
#   always    近 24 小時交易的商品(比特幣、布蘭特原油)，只標示交易中／休市
# 新增標的：在對應區塊加一行，或新增一個 (標題, [標的...]) 區塊即可
SECTIONS = [
    ("道瓊", [
        {"name": "道瓊指數", "symbol": "^DJI", "kind": "index"},
        {"name": "道瓊期貨 YM", "symbol": "YM=F", "kind": "us_future"},
    ]),
    ("S&P 500", [
        {"name": "S&P 500 指數", "symbol": "^GSPC", "kind": "index"},
        {"name": "S&P 500 期貨 ES", "symbol": "ES=F", "kind": "us_future"},
    ]),
    ("那斯達克", [
        {"name": "那斯達克指數", "symbol": "^IXIC", "kind": "index"},
        {"name": "那斯達克 100 期貨 NQ", "symbol": "NQ=F", "kind": "us_future"},
    ]),
    ("費城半導體", [
        {"name": "費半指數 SOX", "symbol": "^SOX", "kind": "index"},
        # {"name": "SOXX ETF", "symbol": "SOXX", "kind": "us_equity"},
    ]),
    ("台積電 ADR", [
        {"name": "台積電 ADR TSM", "symbol": "TSM", "kind": "us_equity"},
    ]),
    ("比特幣與原油", [
        {"name": "比特幣 BTC-USD", "symbol": "BTC-USD", "kind": "always"},
        {"name": "布蘭特原油 BZ=F", "symbol": "BZ=F", "kind": "always"},
    ]),
]

# ----資料抓取----

def _previous_close(ticker: yf.Ticker, close: pd.Series) -> float | None:
    """取前一交易日收盤價(優先 fast_info，失敗時用日線推算)"""
    try:
        value = ticker.fast_info["previousClose"]
        if value and value > 0:
            return float(value)
    except Exception:
        pass
    try:
        daily = ticker.history(period="10d", interval="1d")["Close"].dropna()
        if len(daily) >= 2:
            last_day = close.index[-1].date()
            return float(daily.iloc[-1] if daily.index[-1].date() < last_day else daily.iloc[-2])
    except Exception:
        pass
    return None


def fetch_one(symbol: str, kind: str = "") -> dict | None:
    """抓單一標的 5 分鐘線(含盤前盤後)與前收，失敗回傳 None"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="5m", prepost=True)
        if kind == "index":  # 現貨指數只保留正常盤(美東 09:30–16:00)，Yahoo 在收盤後補的水平線資料不要
            hist = hist.tz_convert(TZ_NY).between_time("09:30", "16:00", inclusive="left")
        if hist.empty:
            return None
        close = hist["Close"].dropna()
        if close.empty:
            return None
        last_ts = close.index[-1]
        series = close[close.index >= last_ts - pd.Timedelta(hours=CHART_HOURS)].copy()
        series.index = series.index.tz_convert(TZ_TW).tz_localize(None)
        return {
            "price": float(close.iloc[-1]),
            "ts": last_ts.tz_convert("UTC"),
            "prev": _previous_close(ticker, close),
            "series": series,
        }
    except Exception:
        return None


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="更新報價中")
def fetch_all(targets: tuple[tuple[str, str], ...]) -> dict[str, dict | None]:
    """並行抓所有標的；僅存在記憶體快取，不寫入任何資料庫"""
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda t: fetch_one(*t), targets)
        return {symbol: result for (symbol, _), result in zip(targets, results)}


# ----時段判斷----

def session_label(kind: str, last_ts: pd.Timestamp, now_utc: pd.Timestamp) -> str:
    """依美東／台北時間與資料新舊程度，回傳時段標籤(假日以資料是否過舊判斷)"""
    if (now_utc - last_ts).total_seconds() / 60 > STALE_MINUTES:
        return "休市／無新資料"
    if kind == "always":
        return "交易中"

    ny = now_utc.tz_convert(TZ_NY)
    t, wd = ny.time(), ny.weekday()
    pre, open_, post, end = dt.time(4, 0), dt.time(9, 30), dt.time(16, 0), dt.time(20, 0)

    if kind == "index":
        return "盤中" if (wd < 5 and open_ <= t < post) else "休市"
    if kind == "us_equity":
        if wd >= 5 or not (pre <= t < end):
            return "休市"
    else:  # us_future：週五 17:00 美東到週日 18:00 休市
        if wd == 5 or (wd == 4 and t >= dt.time(17, 0)) or (wd == 6 and t < dt.time(18, 0)):
            return "休市"
    if pre <= t < open_:
        return "盤前"
    if open_ <= t < post:
        return "盤中"
    if post <= t < end:
        return "盤後"
    return "夜盤"


# ----圖表----

def _prepare(series: pd.Series) -> pd.DataFrame:
    """x 軸改用「第幾筆資料」的序號，休市時段不佔空間；「斷」標記該筆是否接在休市之後"""
    gap = series.index.to_series().diff() > pd.Timedelta(minutes=GAP_MINUTES)
    return pd.DataFrame({
        "序": range(len(series)),
        "價格": series.values,
        "時間文字": [t.strftime("%m/%d %H:%M") for t in series.index],
        "斷": gap.values,
    })


def _split_by_prev(df: pd.DataFrame, prev: float | None) -> pd.DataFrame:
    """依「價格相對昨收」把走勢切成漲／跌區段；穿越昨收處插入內插點讓紅綠線相接；休市處斷線"""
    if not prev:
        return df.assign(區段=df["斷"].cumsum(), 方向="無")[["序", "價格", "區段", "方向"]]
    rows: list[tuple] = []
    seg = 0
    last: tuple | None = None
    for x, p, brk in zip(df["序"], df["價格"], df["斷"]):
        side = "漲" if p >= prev else "跌"
        if last is not None:
            if brk:
                seg += 1
            elif side != last[2]:
                x0, p0, _ = last
                xc = x0 + (x - x0) * ((prev - p0) / (p - p0))
                rows.append((xc, prev, seg, last[2]))
                seg += 1
                rows.append((xc, prev, seg, side))
        rows.append((x, p, seg, side))
        last = (x, p, side)
    return pd.DataFrame(rows, columns=["序", "價格", "區段", "方向"])


def _axis_ticks(df: pd.DataFrame) -> list[int]:
    """x 軸標籤位置：每段開盤的第一筆一定標，再補幾個均分點(距離太近就略過)"""
    n = len(df)
    ticks = sorted({0, *[int(i) for i in df.index[df["斷"]]]})
    for i in (round((n - 1) * k / 3) for k in (1, 2, 3)):
        if all(abs(i - t) >= 0.2 * n for t in ticks):
            ticks.append(i)
    return sorted(ticks)


def build_chart(series: pd.Series, prev: float | None) -> alt.LayerChart:
    """走勢圖：休市時段從 x 軸移除並以垂直虛線分隔、y 軸不從 0 起算、虛線標示昨收、依相對昨收上色、整張圖可觸發提示"""
    df = _prepare(series)
    n = len(df)
    lo, hi = float(series.min()), float(series.max())
    if prev:
        lo, hi = min(lo, prev), max(hi, prev)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.001 or 1.0
    y_enc = alt.Y(
        "價格:Q",
        title=None,
        scale=alt.Scale(domain=[lo - pad, hi + pad], zero=False, nice=False),
        axis=alt.Axis(format=",.0f" if hi - lo >= 100 else ",.2f"),
    )

    ticks = _axis_ticks(df)
    labels = {str(i): df["時間文字"][i] for i in ticks}
    x_scale = alt.Scale(domain=[-0.5, n - 0.5], nice=False)
    x_enc = alt.X(
        "序:Q", title=None, scale=x_scale,
        axis=alt.Axis(values=ticks, labelExpr=f"{json.dumps(labels)}[datum.value]",
                      labelAngle=0, labelFlush=True, labelOverlap=True, grid=False),
    )
    x_plain = alt.X("序:Q", scale=x_scale)  # 其他圖層共用同一個 x 比例尺，不重複畫軸

    line = alt.Chart(_split_by_prev(df, prev)).mark_line().encode(
        x=x_enc,
        y=y_enc,
        detail="區段:N",
        color=alt.Color(
            "方向:N",
            scale=alt.Scale(domain=["漲", "跌", "無"], range=[UP_COLOR, DOWN_COLOR, FLAT_COLOR]),
            legend=None,
        ),
    )

    # 互動：每筆資料各有一個「半格寬、整張圖高」的透明長條，游標在圖上任何位置都會落在其中一條，
    # 顯示該筆的時間與價格；游標移出圖表時，提示框、游標線與圓點一起消失，全頁同時最多一條游標線
    hover = alt.selection_point(on="mouseover", clear="mouseout", fields=["序"], empty=False)
    tip = [alt.Tooltip("時間文字:N", title="時間(台北)"), alt.Tooltip("價格:Q", format=",.2f")]
    base = alt.Chart(df)
    guide = base.mark_rule(color="#adb5bd").encode(x=x_plain).transform_filter(hover)
    dot = base.mark_point(size=70, filled=True, color="#dee2e6").encode(x=x_plain, y=y_enc).transform_filter(hover)
    cells = df.assign(左=df["序"] - 0.5, 右=df["序"] + 0.5)
    hit_area = (  # 必須放在最上層，才接得到游標事件
        alt.Chart(cells)
        .mark_rect(opacity=0.01, color="#000000")
        .encode(x=alt.X("左:Q", scale=x_scale), x2=alt.X2("右:Q"), tooltip=tip)
        .add_params(hover)
    )

    layers = [line, guide, dot, hit_area]
    if prev:
        layers.insert(1, alt.Chart(pd.DataFrame({"昨收": [prev]})).mark_rule(color="#868e96", strokeDash=[4, 4]).encode(y="昨收:Q"))
    breaks = [int(i) - 0.5 for i in df.index[df["斷"]]]
    if breaks:  # 休市分隔線：收盤與下次開盤之間
        layers.insert(1, alt.Chart(pd.DataFrame({"x": breaks})).mark_rule(color="#adb5bd", strokeDash=[3, 3], opacity=0.8).encode(x=alt.X("x:Q", scale=x_scale)))
    return (
        alt.layer(*layers)
        .properties(height=CHART_HEIGHT, padding={"left": 4, "right": 24, "top": 8, "bottom": 4})
        .configure(background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(labelColor=MUTED, domainColor=BORDER, tickColor=BORDER, gridColor=BORDER)
    )


# ----畫面----

def render_card(item: dict, data: dict | None, now_utc: pd.Timestamp) -> None:
    """單一標的卡片：現價、漲跌、時段標籤、資料時間、走勢圖"""
    with st.container(border=True):
        if data is None:
            st.markdown(f"**{item['name']}**")
            st.warning("暫時無法取得")
            return
        label = session_label(item["kind"], data["ts"], now_utc)
        price, prev = data["price"], data["prev"]
        delta = None
        if prev:
            chg = price - prev
            delta = f"{chg:+,.2f} ({chg / prev * 100:+.2f}%)"
        st.metric(
            f"{item['name']}・{label}",
            f"{price:,.2f}",
            delta,
            delta_color="inverse", 
        )
        note = f"資料時間(台北){data['ts'].tz_convert(TZ_TW):%m/%d %H:%M}"
        if prev:
            note += f"・昨收 {prev:,.2f}"
        st.caption(note)
        if len(data["series"]) > 1:
            st.altair_chart(build_chart(data["series"], prev), theme=None)


# RWD：st.columns 在寬度 < 640px 時會自動直向堆疊，這裡再補手機的邊距與字級
CUSTOM_CSS = """
<style>
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1600px; }
@media (max-width: 768px) {
  .block-container { padding: 1rem 0.75rem 2rem 0.75rem; }
  h1 { font-size: 1.6rem !important; }
  h3 { font-size: 1.15rem !important; }
  [data-testid="stMetricValue"] { font-size: 1.6rem; }
  [data-testid="stMetricLabel"] p { font-size: 0.9rem; }
}
</style>
"""

st.set_page_config(page_title="國際股市盤前盤中盤後資訊", layout="wide", initial_sidebar_state="collapsed")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
st.title("國際股市盤前盤中盤後資訊")

interval_col, _ = st.columns([1, 4])
with interval_col:
    interval_min = st.selectbox("自動更新間隔", [1, 3, 5], index=1, format_func=lambda m: f"每 {m} 分鐘")


@st.fragment(run_every=interval_min * 60)
def board() -> None:
    """看盤區塊：依設定間隔自動重跑，只更新此區塊"""
    btn_col, time_col = st.columns([1, 4], vertical_alignment="center")
    with btn_col:
        if st.button("手動刷新"):
            fetch_all.clear()
    targets = tuple((item["symbol"], item["kind"]) for _, items in SECTIONS for item in items)
    data = fetch_all(targets)
    now_utc = pd.Timestamp.now(tz="UTC")
    with time_col:
        st.caption(f"頁面更新時間(台北){now_utc.tz_convert(TZ_TW):%m/%d %H:%M:%S}")
    for title, items in SECTIONS:
        st.subheader(title)
        cols = st.columns(2)
        for col, item in zip(cols, items):
            with col:
                render_card(item, data.get(item["symbol"]), now_utc)


board()
st.caption(
    "資料來源：Yahoo Finance(延遲報價)，報價可能延遲或偶爾失效。"
    "休市時段在走勢圖上以垂直虛線分隔，休市日以資料是否過舊判斷。"
)