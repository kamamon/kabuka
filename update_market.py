#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市場データ自動更新スクリプト
============================

GitHub Actions から毎日実行され、research.json に載っている銘柄・ファンドの
最新データを取得して market.json と bundle.js を作り直します。

取得元（上から順に試し、失敗したら次へ）
  国内株式・ETF・指数・為替 : yfinance（インストール済みなら） → Yahoo Finance チャートAPI → Stooq
  投資信託の基準価額        : 投資信託協会「投信総合検索ライブラリー」CSV（分配金込みで再計算）
  日本国債の利回り          : 財務省「国債金利情報」CSV
  米国10年債利回り          : FRED (DGS10)
  アナリスト予想（任意）    : yfinance の Ticker.info（目標株価・推奨度）

取得に失敗した項目は前回の値を残し "stale": true を付けます。
全部失敗したときはファイルを書き換えずに終了コード 1 で終わります。
標準ライブラリだけで動きます（yfinance は任意）。

使い方
  python update_market.py                 ネットから取得して更新
  python update_market.py --offline       取得せず bundle.js だけ作り直す
  python update_market.py --fixtures DIR  テスト用（DIR/manifest.json の URL→ファイル対応を使う）
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATA_DIR = os.path.dirname(os.path.abspath(__file__))  # research.json などと同じフォルダ

JST = dt.timezone(dt.timedelta(hours=9), "JST")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
MOF_CURRENT = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
ERA_BASE = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}


# ---------------------------------------------------------------- 小道具
def log(msg: str) -> None:
    print(msg, flush=True)


def warn(title: str, msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title={title}::{msg}", flush=True)
    else:
        print(f"[warning] {title}: {msg}", flush=True)


def short(e: Exception) -> str:
    s = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {e}"
    return s if len(s) < 160 else s[:157] + "..."


def rnd(x, n=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, n)


def sig(x: float, n: int = 4) -> float:
    """有効数字 n 桁に丸める（スパークライン用）"""
    if x == 0 or x is None or math.isnan(x):
        return 0
    return round(x, -int(math.floor(math.log10(abs(x)))) + (n - 1))


def load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- HTTP
class Fetcher:
    def __init__(self, offline: bool = False, fixtures_dir: str | None = None):
        self.offline = offline
        self.fixtures = None
        if fixtures_dir:
            with open(os.path.join(fixtures_dir, "manifest.json"), encoding="utf-8") as f:
                self.fixtures = {k: os.path.join(fixtures_dir, v) for k, v in json.load(f).items()}

    def get(self, url: str, timeout: int = 25, retries: int = 2) -> bytes:
        if self.offline:
            raise FetchError("offline mode")
        if self.fixtures is not None:
            path = self.fixtures.get(url)
            if not path or not os.path.exists(path):
                raise FetchError(f"no fixture for {url}")
            with open(path, "rb") as f:
                return f.read()
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA, "Accept": "*/*", "Accept-Language": "ja,en;q=0.8"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (400, 401, 403, 404):
                    break
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(2 * (attempt + 1))
        raise FetchError(short(last) if last else "unknown error")


# ---------------------------------------------------------------- 価格系列（株・ETF・指数・為替）
def yahoo_chart_url(symbol: str, host: str = YAHOO_HOSTS[0], rng: str = "2y") -> str:
    return (f"https://{host}/v8/finance/chart/{urllib.parse.quote(symbol, safe='')}"
            f"?range={rng}&interval=1d&includeAdjustedClose=true&events=div%2Csplits")


def stooq_url(symbol: str) -> str:
    return f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol, safe='^')}&i=d"


def parse_yahoo_chart(raw: bytes):
    j = json.loads(raw.decode("utf-8"))
    chart = j.get("chart") or {}
    if chart.get("error"):
        raise FetchError(f"yahoo error {chart['error']}")
    res = (chart.get("result") or [None])[0]
    if not res:
        raise FetchError("yahoo: empty result")
    meta = res.get("meta") or {}
    off = int(meta.get("gmtoffset") or 0)
    ts = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    quote = (ind.get("quote") or [{}])[0] or {}
    closes = quote.get("close") or []
    adj_list = (ind.get("adjclose") or [{}])[0] or {}
    adjs = adj_list.get("adjclose") or closes
    rows: dict[str, tuple[float, float]] = {}
    for t, c, a in zip(ts, closes, adjs):
        if c is None or (isinstance(c, float) and math.isnan(c)):
            continue
        d = (dt.datetime.fromtimestamp(t, dt.timezone.utc) + dt.timedelta(seconds=off)).date().isoformat()
        a = c if a is None or (isinstance(a, float) and math.isnan(a)) else a
        rows[d] = (float(c), float(a))
    out = sorted((d, c, a) for d, (c, a) in rows.items())
    if len(out) < 2:
        raise FetchError("yahoo: too few rows")
    return out


def parse_stooq_csv(raw: bytes):
    text = raw.decode("utf-8", "replace").strip()
    if not text.lower().startswith("date"):
        raise FetchError("stooq: no data")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            c = float(r.get("Close") or r.get("close"))
        except (TypeError, ValueError):
            continue
        out.append((r.get("Date") or r.get("date"), c, c))
    out.sort()
    if len(out) < 2:
        raise FetchError("stooq: too few rows")
    cutoff = (dt.date.fromisoformat(out[-1][0]) - dt.timedelta(days=800)).isoformat()
    return [r for r in out if r[0] >= cutoff]


def yf_available() -> bool:
    try:
        import yfinance  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def yf_history(symbol: str):
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=False, actions=False)
    if h is None or len(h) < 2:
        raise FetchError("yfinance: empty")
    out = []
    for idx, row in h.iterrows():
        c = row.get("Close")
        if c is None or c != c:
            continue
        a = row.get("Adj Close", c)
        if a is None or a != a:
            a = c
        out.append((idx.date().isoformat(), float(c), float(a)))
    out.sort()
    if len(out) < 2:
        raise FetchError("yfinance: too few rows")
    return out


def yf_analyst(symbol: str) -> dict:
    import yfinance as yf
    t = yf.Ticker(symbol)
    info = t.get_info() if hasattr(t, "get_info") else t.info
    keys = {
        "targetMeanPrice": "target_mean", "targetHighPrice": "target_high",
        "targetLowPrice": "target_low", "targetMedianPrice": "target_median",
        "numberOfAnalystOpinions": "n", "recommendationKey": "rec_key",
        "recommendationMean": "rec_mean", "forwardEps": "forward_eps",
        "trailingEps": "trailing_eps", "returnOnEquity": "roe",
        "revenueGrowth": "revenue_growth", "earningsGrowth": "earnings_growth",
    }
    out = {}
    for k, v in keys.items():
        val = (info or {}).get(k)
        if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
            continue
        out[v] = round(val, 4) if isinstance(val, float) else val
    if not out.get("target_mean"):
        raise FetchError("no analyst target")
    out["date"] = today_jst().isoformat()
    out["source"] = "Yahoo Finance (yfinance)"
    return out


def fetch_price_series(fetcher: Fetcher, yahoo: str | None, stooq: str | None, use_yf: bool):
    errors = []
    if use_yf and yahoo:
        try:
            return yf_history(yahoo), "yfinance"
        except Exception as e:  # noqa: BLE001
            errors.append(f"yfinance: {short(e)}")
    if yahoo:
        for host in YAHOO_HOSTS:
            try:
                return parse_yahoo_chart(fetcher.get(yahoo_chart_url(yahoo, host))), "yahoo"
            except Exception as e:  # noqa: BLE001
                errors.append(f"yahoo({host.split('.')[0]}): {short(e)}")
    if stooq:
        try:
            return parse_stooq_csv(fetcher.get(stooq_url(stooq))), "stooq"
        except Exception as e:  # noqa: BLE001
            errors.append(f"stooq: {short(e)}")
    raise FetchError(" / ".join(errors) or "no symbol")


# ---------------------------------------------------------------- 指標計算
def series_metrics(series, spark_points: int = 60) -> dict:
    """series: [(YYYY-MM-DD, 終値, 調整後終値)] 昇順"""
    dates = [dt.date.fromisoformat(r[0]) for r in series]
    closes = [r[1] for r in series]
    adjs = [r[2] for r in series]
    last = dates[-1]

    def start_index(days: int) -> int:
        return bisect.bisect_left(dates, last - dt.timedelta(days=days))

    def ret(days: int, annualize: bool = False):
        target = last - dt.timedelta(days=days)
        i = bisect.bisect_right(dates, target) - 1
        if i < 0 or (target - dates[i]).days > 14 or adjs[i] <= 0:
            return None
        r = adjs[-1] / adjs[i]
        if annualize:
            yrs = (last - dates[i]).days / 365.25
            return (r ** (1 / yrs) - 1) * 100 if yrs > 0 else None
        return (r - 1) * 100

    def vol(days: int):
        i = start_index(days)
        if i == 0 and (last - dates[0]).days < days * 0.9:
            return None  # 期間が足りない
        seg = adjs[i:]
        lr = [math.log(seg[k] / seg[k - 1]) for k in range(1, len(seg)) if seg[k - 1] > 0 and seg[k] > 0]
        if len(lr) < 20:
            return None
        mean = sum(lr) / len(lr)
        var = sum((x - mean) ** 2 for x in lr) / (len(lr) - 1)
        span_years = max((last - dates[i]).days, 1) / 365.25
        per_year = len(lr) / span_years
        return math.sqrt(var * per_year) * 100

    def mdd(days: int):
        i = start_index(days)
        if i == 0 and (last - dates[0]).days < days * 0.9:
            return None
        seg = adjs[i:]
        if len(seg) < 20:
            return None
        peak, worst = seg[0], 0.0
        for v in seg:
            peak = max(peak, v)
            worst = min(worst, v / peak - 1)
        return worst * 100

    i52 = start_index(365)
    window = closes[i52:] or closes[-1:]
    wdates = [d.isoformat() for d in dates[i52:]] or [last.isoformat()]
    step = max(1, -(-len(window) // spark_points))  # 切り上げ
    idx = list(range(0, len(window), step))
    if idx[-1] != len(window) - 1:
        idx.append(len(window) - 1)
    prev = closes[-2] if len(closes) > 1 else None
    return {
        "price": rnd(closes[-1], 2),
        "date": last.isoformat(),
        "prev_close": rnd(prev, 2),
        "chg_1d_pct": rnd((closes[-1] / prev - 1) * 100 if prev else None),
        "ret_1m_pct": rnd(ret(30)),
        "ret_3m_pct": rnd(ret(91)),
        "ret_6m_pct": rnd(ret(182)),
        "ret_1y_pct": rnd(ret(365)),
        "ret_3y_ann_pct": rnd(ret(1096, True)),
        "ret_5y_ann_pct": rnd(ret(1826, True)),
        "vol_1y_pct": rnd(vol(365)),
        "vol_3y_pct": rnd(vol(1096)),
        "mdd_1y_pct": rnd(mdd(365)),
        "mdd_3y_pct": rnd(mdd(1096)),
        "high_52w": rnd(max(window), 2),
        "low_52w": rnd(min(window), 2),
        "spark": [sig(window[i]) for i in idx],
        "spark_d": [wdates[i] for i in idx],
    }


# ---------------------------------------------------------------- 投資信託（投信協会CSV）
def toushin_url(isin: str, assoc: str) -> str:
    return ("https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
            f"?isinCd={isin}&associFundCd={assoc}")


def parse_toushin_csv(raw: bytes):
    """列: 年月日, 基準価額(円), 純資産総額(百万円), 分配金, 決算期（Shift_JIS）"""
    text = raw.decode("cp932", "replace")
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if not r:
            continue
        m = re.match(r"\s*(\d{4})年(\d{1,2})月(\d{1,2})日", r[0])
        if not m:
            continue
        try:
            nav = float(r[1].replace(",", ""))
        except (IndexError, ValueError):
            continue
        aum = None
        if len(r) > 2 and r[2].strip():
            try:
                aum = float(r[2].replace(",", ""))
            except ValueError:
                pass
        dist = 0.0
        if len(r) > 3 and r[3].strip():
            try:
                dist = float(r[3].replace(",", ""))
            except ValueError:
                pass
        rows.append((dt.date(int(m[1]), int(m[2]), int(m[3])), nav, aum, dist))
    rows.sort(key=lambda x: x[0])
    if len(rows) < 2:
        raise FetchError("toushin: too few rows")
    series, tri = [], rows[0][1]
    series.append((rows[0][0].isoformat(), rows[0][1], tri))
    for k in range(1, len(rows)):
        d, nav, _aum, dist = rows[k]
        prev_nav = rows[k - 1][1]
        if prev_nav > 0:
            tri *= (nav + dist) / prev_nav  # 分配金を再投資したとみなす
        series.append((d.isoformat(), nav, tri))
    last_d = rows[-1][0]
    dist_12m = sum(r[3] for r in rows if r[0] > last_d - dt.timedelta(days=365))
    return series, {"aum_million": rows[-1][2], "dist_12m": dist_12m}


# ---------------------------------------------------------------- 金利
def parse_era_date(s: str):
    m = re.match(r"\s*([MTSHR])(\d+)\.(\d+)\.(\d+)", s or "")
    if not m:
        return None
    return dt.date(ERA_BASE[m[1]] + int(m[2]), int(m[3]), int(m[4]))


def parse_mof_jgb(raw: bytes):
    """財務省 国債金利情報CSV（Shift_JIS, 1行目タイトル, 基準日は和暦 R8.10.21 形式）"""
    text = raw.decode("cp932", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    hi = next((i for i, r in enumerate(rows) if r and r[0].strip() == "基準日"), None)
    if hi is None:
        raise FetchError("mof: header not found")
    header = [h.strip() for h in rows[hi]]
    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        d = parse_era_date(r[0])
        if not d:
            continue
        vals = {}
        for j, h in enumerate(header):
            m = re.match(r"(\d+)年$", h)
            if not m or j >= len(r):
                continue
            try:
                vals[int(m[1])] = float(r[j])
            except ValueError:
                pass
        if vals:
            out.append((d, vals))
    if not out:
        raise FetchError("mof: no data rows")
    out.sort(key=lambda x: x[0])
    return out


def kojinmuke_estimates(y: dict) -> dict:
    """個人向け国債の次回利率の目安（財務省の算式を市場利回りで近似）"""
    def floor2(x):
        return math.floor(x * 100 + 1e-9) / 100
    est = {}
    if 10 in y:
        est["hendo10"] = max(0.05, floor2(y[10] * 0.66))
    if 5 in y:
        est["kotei5"] = max(0.05, floor2(y[5] - 0.05))
    if 3 in y:
        est["kotei3"] = max(0.05, floor2(y[3] - 0.03))
    return est


def fred_url(series_id: str, start: dt.date) -> str:
    return f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start.isoformat()}"


def parse_fred(raw: bytes):
    text = raw.decode("utf-8", "replace")
    rd = csv.reader(io.StringIO(text))
    next(rd, None)
    pts = []
    for r in rd:
        if len(r) < 2:
            continue
        try:
            pts.append((r[0], float(r[1])))
        except ValueError:
            continue
    if not pts:
        raise FetchError("fred: no data")
    return pts


def value_change(pts, days: int):
    """[(date, value)] から最新値と days 日前との差"""
    last_d = dt.date.fromisoformat(pts[-1][0])
    target = last_d - dt.timedelta(days=days)
    older = [p for p in pts if dt.date.fromisoformat(p[0]) <= target]
    return (pts[-1][1] - older[-1][1]) if older else None


# ---------------------------------------------------------------- 失敗時のフォールバック
def fallback_entry(prev: dict | None, research_item: dict | None, price_key: str = "price") -> dict:
    if prev:
        e = dict(prev)
        e["stale"] = True
        return e
    e = {"stale": True, "source": "research"}
    if research_item:
        if research_item.get("price") is not None:
            e[price_key] = research_item.get("price")
            e["date"] = research_item.get("price_date")
        elif research_item.get("nav") is not None:
            e[price_key] = research_item.get("nav")
            e["date"] = research_item.get("nav_date")
    return e


# ---------------------------------------------------------------- メイン処理
def symbols_for(code: str, item: dict) -> tuple[str, str]:
    sy = item.get("symbols") or {}
    return sy.get("yahoo") or f"{code}.T", sy.get("stooq") or f"{code.lower()}.jp"


def update(research: dict, prev: dict, fetcher: Fetcher, use_yf: bool, with_analyst: bool) -> dict:
    now = dt.datetime.now(JST)
    market = {"updated_at": now.isoformat(timespec="minutes"), "stocks": {}, "etfs": {},
              "indices": {}, "funds": {}, "rates": {}}
    st = {"stocks_ok": 0, "stocks_failed": 0, "funds_ok": 0, "funds_failed": 0, "funds_skipped": 0,
          "etfs_ok": 0, "etfs_failed": 0, "indices_ok": 0, "indices_failed": 0,
          "rates_ok": 0, "rates_failed": 0, "analyst_ok": 0, "errors": []}

    def fail(kind: str, key: str, e: Exception):
        st[f"{kind}_failed"] += 1
        msg = f"{kind}:{key}: {short(e)}"
        st["errors"].append(msg)
        warn(f"取得失敗 {kind}", msg)

    # --- 金利（ファンドのシャープレシオで使うので先に）
    rates = {}
    rf = 0.0
    try:
        try:
            rows = parse_mof_jgb(fetcher.get(MOF_CURRENT))
        except Exception:  # noqa: BLE001  月初などで当月分が空のとき
            rows = parse_mof_jgb(fetcher.get(MOF_ALL))
        d, ylds = rows[-1]
        rates["jgb"] = {"date": d.isoformat(),
                        "curve": {str(k): v for k, v in sorted(ylds.items())},
                        "source": "財務省 国債金利情報"}
        first = rows[0]
        if first[0] < d and 10 in first[1] and 10 in ylds:
            rates["jgb"]["chg_10y_since"] = {"date": first[0].isoformat(), "bp": round((ylds[10] - first[1][10]) * 100, 1)}
        rates["kojinmuke_estimate"] = {**kojinmuke_estimates(ylds), "basis_date": d.isoformat()}
        rf = ylds.get(1, 0.0) or 0.0
        st["rates_ok"] += 1
    except Exception as e:  # noqa: BLE001
        fail("rates", "jgb", e)
        for k in ("jgb", "kojinmuke_estimate"):
            if k in (prev.get("rates") or {}):
                rates[k] = {**prev["rates"][k], "stale": True}
        rf = ((((prev.get("rates") or {}).get("jgb") or {}).get("curve") or {}).get("1")) or 0.0
    try:
        pts = parse_fred(fetcher.get(fred_url("DGS10", today_jst() - dt.timedelta(days=400))))
        rates["ust10y"] = {"value": pts[-1][1], "date": pts[-1][0],
                           "chg_1m_bp": rnd((value_change(pts, 30) or 0) * 100, 1),
                           "source": "FRED DGS10"}
        st["rates_ok"] += 1
    except Exception as e:  # noqa: BLE001
        fail("rates", "ust10y", e)
        if "ust10y" in (prev.get("rates") or {}):
            rates["ust10y"] = {**prev["rates"]["ust10y"], "stale": True}
    market["rates"] = rates

    # --- 指数・為替
    for ix in research.get("indices", []):
        key = ix["id"]
        try:
            series, src = fetch_price_series(fetcher, ix.get("yahoo"), ix.get("stooq"), use_yf)
            m = series_metrics(series)
            m.update(source=src, stale=False)
            market["indices"][key] = m
            st["indices_ok"] += 1
        except Exception as e:  # noqa: BLE001
            if ix.get("fred"):
                try:
                    pts = parse_fred(fetcher.get(fred_url(ix["fred"], today_jst() - dt.timedelta(days=800))))
                    series = [(d, v, v) for d, v in pts]
                    m = series_metrics(series)
                    m.update(source="fred", stale=False)
                    market["indices"][key] = m
                    st["indices_ok"] += 1
                    continue
                except Exception as e2:  # noqa: BLE001
                    e = FetchError(f"{short(e)} / fred: {short(e2)}")
            fail("indices", key, e)
            market["indices"][key] = fallback_entry((prev.get("indices") or {}).get(key), None)

    # --- 国内株式
    analyst_fail_streak = 0
    for s in research.get("stocks", []):
        code = s["code"]
        ysym, ssym = symbols_for(code, s)
        try:
            series, src = fetch_price_series(fetcher, ysym, ssym, use_yf)
            m = series_metrics(series)
            m.update(source=src, stale=False)
            st["stocks_ok"] += 1
        except Exception as e:  # noqa: BLE001
            fail("stocks", code, e)
            m = fallback_entry((prev.get("stocks") or {}).get(code), s)
        if with_analyst and use_yf and analyst_fail_streak < 4:
            try:
                m["analyst_auto"] = yf_analyst(ysym)
                st["analyst_ok"] += 1
                analyst_fail_streak = 0
                time.sleep(0.6)
            except Exception:  # noqa: BLE001
                analyst_fail_streak += 1
                old = ((prev.get("stocks") or {}).get(code) or {}).get("analyst_auto")
                if old:
                    m["analyst_auto"] = old
        else:
            old = ((prev.get("stocks") or {}).get(code) or {}).get("analyst_auto")
            if old and "analyst_auto" not in m:
                m["analyst_auto"] = old
        market["stocks"][code] = m

    # --- ETF（債券ETFなど）
    for e_item in research.get("bond_alternatives", []):
        code = (e_item.get("code") or "").strip()
        if not code or e_item.get("type") != "ETF":
            continue
        ysym, ssym = symbols_for(code, e_item)
        try:
            series, src = fetch_price_series(fetcher, ysym, ssym, use_yf)
            m = series_metrics(series)
            m.update(source=src, stale=False)
            market["etfs"][code] = m
            st["etfs_ok"] += 1
        except Exception as e:  # noqa: BLE001
            fail("etfs", code, e)
            market["etfs"][code] = fallback_entry((prev.get("etfs") or {}).get(code), e_item)

    # --- 投資信託
    for f in research.get("funds", []):
        fid = f["id"]
        isin, assoc = f.get("isin"), f.get("assoc_code")
        if not isin or not assoc:  # 自動取得の対象外（調査時点の値を表示）
            st["funds_skipped"] += 1
            market["funds"][fid] = fallback_entry((prev.get("funds") or {}).get(fid), f, price_key="nav")
            continue
        try:
            series, meta = parse_toushin_csv(fetcher.get(toushin_url(isin, assoc)))
            m = series_metrics(series)
            m["nav"] = m.pop("price")
            m["prev_nav"] = m.pop("prev_close")
            if meta.get("aum_million") is not None:
                m["aum_oku"] = rnd(meta["aum_million"] / 100, 1)
            m["dist_12m"] = rnd(meta.get("dist_12m"), 0)
            v3, r3 = m.get("vol_3y_pct"), m.get("ret_3y_ann_pct")
            m["sharpe_3y"] = rnd((r3 - rf) / v3, 2) if (v3 and r3 is not None) else None
            m["rf_pct"] = rf
            m.update(source="toushin-lib", stale=False)
            market["funds"][fid] = m
            st["funds_ok"] += 1
        except Exception as e:  # noqa: BLE001
            fail("funds", fid, e)
            market["funds"][fid] = fallback_entry((prev.get("funds") or {}).get(fid), f, price_key="nav")

    st["errors"] = st["errors"][:60]
    market["status"] = st
    return market


def offline_market(research: dict, prev: dict) -> dict:
    """ネットに出ずに作る（初回や research.json だけ直したとき）。前回値→調査時点の値の順で使う"""
    market = {"updated_at": prev.get("updated_at"),  # 取得していないので時刻は前回のまま（初回は空）
              "stocks": {}, "etfs": {}, "indices": dict(prev.get("indices") or {}),
              "funds": {}, "rates": dict(prev.get("rates") or {}),
              "status": prev.get("status") or {"note": "offline build"}}
    for s in research.get("stocks", []):
        market["stocks"][s["code"]] = (prev.get("stocks") or {}).get(s["code"]) or fallback_entry(None, s)
    for e in research.get("bond_alternatives", []):
        code = (e.get("code") or "").strip()
        if code and e.get("type") == "ETF":
            market["etfs"][code] = (prev.get("etfs") or {}).get(code) or fallback_entry(None, e)
    for f in research.get("funds", []):
        market["funds"][f["id"]] = (prev.get("funds") or {}).get(f["id"]) or fallback_entry(None, f, "nav")
    return market


def to_js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def write_outputs(research: dict, market: dict, data_dir: str = DATA_DIR) -> None:
    with open(os.path.join(data_dir, "market.json"), "w", encoding="utf-8") as f:
        json.dump(market, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    with open(os.path.join(data_dir, "bundle.js"), "w", encoding="utf-8") as f:
        f.write("/* 自動生成ファイル（update_market.py）。手で編集しないでください。 */\n")
        f.write(f"window.RESEARCH={to_js(research)};\n")
        f.write(f"window.MARKET={to_js(market)};\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="市場データを取得して market.json と bundle.js を更新")
    ap.add_argument("--offline", action="store_true", help="ネットに出ずに bundle.js だけ作り直す")
    ap.add_argument("--fixtures", help="テスト用フィクスチャのフォルダ")
    ap.add_argument("--no-yfinance", action="store_true", help="yfinance を使わない")
    ap.add_argument("--no-analyst", action="store_true", help="アナリスト予想の自動取得をしない")
    ap.add_argument("--data-dir", default=DATA_DIR, help="research.json のあるフォルダ（テスト用）")
    args = ap.parse_args(argv)

    research = load_json(os.path.join(args.data_dir, "research.json"))
    if not research:
        log("research.json が見つかりません")
        return 1
    prev = load_json(os.path.join(args.data_dir, "market.json")) or {}

    if args.offline:
        market = offline_market(research, prev)
        write_outputs(research, market, args.data_dir)
        log("offline: bundle.js を作り直しました")
        return 0

    fetcher = Fetcher(fixtures_dir=args.fixtures)
    use_yf = (not args.no_yfinance) and (args.fixtures is None) and yf_available()
    log(f"yfinance: {'使用' if use_yf else '不使用'}")
    market = update(research, prev, fetcher, use_yf, with_analyst=not args.no_analyst)
    st = market["status"]
    log("結果: " + ", ".join(f"{k}={v}" for k, v in st.items() if k != "errors"))
    if st["stocks_ok"] == 0 and st["funds_ok"] == 0 and st["indices_ok"] == 0:
        log("すべての取得に失敗したため、ファイルは更新しません")
        return 1
    write_outputs(research, market, args.data_dir)
    log(f"更新しました: {market['updated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
