# -*- coding: utf-8 -*-
"""update_market.py のテスト（ネット不要。実データと同じ形式のフィクスチャを生成して検証）

  python -m unittest test_update_market -v
"""
import datetime as dt
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update_market as um  # noqa: E402

TODAY = um.today_jst()


def business_days(n_days_back: int):
    d = TODAY - dt.timedelta(days=n_days_back)
    out = []
    while d <= TODAY - dt.timedelta(days=1):
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def yahoo_json(dates, closes, adjs=None, gmtoffset=32400):
    ts = [int(dt.datetime(d.year, d.month, d.day, 0, 0, tzinfo=dt.timezone.utc).timestamp()) for d in dates]
    return json.dumps({"chart": {"result": [{
        "meta": {"currency": "JPY", "symbol": "X", "gmtoffset": gmtoffset},
        "timestamp": ts,
        "indicators": {"quote": [{"close": closes}], "adjclose": [{"adjclose": adjs or closes}]}}],
        "error": None}}).encode()


def stooq_csv(dates, closes):
    lines = ["Date,Open,High,Low,Close,Volume"]
    lines += [f"{d.isoformat()},{c},{c},{c},{c},1000" for d, c in zip(dates, closes)]
    return ("\n".join(lines) + "\n").encode()


def toushin_csv(dates, navs, dists=None):
    lines = ["年月日,基準価額(円),純資産総額（百万円）,分配金,決算期"]
    for i, (d, v) in enumerate(zip(dates, navs)):
        dist = (dists or {}).get(i)
        lines.append(f"{d.year}年{d.month:02d}月{d.day:02d}日,{v},{1000000 + i},{'' if dist is None else dist},{'' if dist is None else 1}")
    return ("\n".join(lines) + "\n").encode("cp932")


def mof_csv(rows):
    head = "国債金利情報 (令和8年10月),,,,,,,,,,,,,,,,(単位 : %)"
    hdr = "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年,20年,25年,30年,40年"
    body = [f"{r[0]}," + ",".join(str(x) for x in r[1]) for r in rows]
    tail = [",,,,,,,,,,,,,,,", "※最新のcsvデータがダウンロードできない場合、キャッシュを削除してください。,,,,,,,,,,,,,,,"]
    return ("\r\n".join([head, hdr] + body + tail) + "\r\n").encode("cp932")


class UpdateMarketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.fix = os.path.join(self.tmp, "fix")
        os.makedirs(self.data)
        os.makedirs(self.fix)
        self.research = {
            "indices": [
                {"id": "nikkei", "name": "日経平均", "yahoo": "^N225", "stooq": "^nkx"},
                {"id": "usdjpy", "name": "ドル円", "yahoo": "JPY=X", "stooq": "usdjpy", "fred": "DEXJPUS"},
            ],
            "stocks": [
                {"code": "6857", "name": "A社", "price": 9999, "price_date": "2026-10-22"},
                {"code": "285A", "name": "B社", "price": 5000, "price_date": "2026-10-22"},
                {"code": "9999", "name": "C社（取得不能）", "price": 1234, "price_date": "2026-10-22"},
            ],
            "bond_alternatives": [{"code": "2621", "name": "債券ETF", "type": "ETF"}],
            "funds": [
                {"id": "f1", "name": "F1", "isin": "JP0000000001", "assoc_code": "0000001A"},
                {"id": "f2", "name": "F2（分配あり）", "isin": "JP0000000002", "assoc_code": "0000002A"},
                {"id": "f3", "name": "F3（コードなし）", "nav": 12345, "nav_date": "2026-10-21"},
            ],
        }
        with open(os.path.join(self.data, "research.json"), "w", encoding="utf-8") as f:
            json.dump(self.research, f, ensure_ascii=False)

        self.manifest = {}
        self.d2y = business_days(800)
        self.p_a = [1000 * (1.001 ** i) for i in range(len(self.d2y))]
        self.add(um.yahoo_chart_url("6857.T"), "a.json", yahoo_json(self.d2y, self.p_a))
        # 285A は Yahoo が失敗 → Stooq
        self.p_b = [500 + i for i in range(len(self.d2y))]
        self.add(um.stooq_url("285a.jp"), "b.csv", stooq_csv(self.d2y, self.p_b))
        self.add(um.yahoo_chart_url("^N225"), "n.json", yahoo_json(self.d2y, [40000 + i for i in range(len(self.d2y))]))
        self.add(um.yahoo_chart_url("2621.T"), "e.json", yahoo_json(self.d2y, [1200.0] * len(self.d2y)))
        # 投信: 6年分
        self.d6y = business_days(6 * 366)
        self.nav1 = [10000 * (1.0004 ** i) for i in range(len(self.d6y))]
        self.add(um.toushin_url("JP0000000001", "0000001A"), "f1.csv", toushin_csv(self.d6y, [round(x) for x in self.nav1]))
        # F2: 基準価額は横ばい、毎年100円分配（分配落ちで基準価額が下がる）
        nav2, dists, v = [], {}, 10000.0
        for i, d in enumerate(self.d6y):
            if i > 0 and i % 245 == 0:
                v -= 100
                dists[i] = 100
            nav2.append(v)
        self.nav2, self.dists2 = nav2, dists
        self.add(um.toushin_url("JP0000000002", "0000002A"), "f2.csv", toushin_csv(self.d6y, nav2, dists))
        # 金利
        self.add(um.MOF_CURRENT, "mof.csv", mof_csv([
            ("R8.10.1", [1.668, 1.939, 2.077, 2.274, 2.407, 2.534, 2.657, 2.82, 2.952, 3.092, 3.62, 3.91, 4.154, 4.122, 4.125]),
            ("R8.10.21", [1.657, 1.914, 2.032, 2.188, 2.309, 2.407, 2.518, 2.667, 2.792, 2.923, 3.451, 3.726, 4.008, 3.98, 3.994]),
        ]))
        start = TODAY - dt.timedelta(days=400)
        fred_rows = ["observation_date,DGS10"]
        dd = start
        k = 0
        while dd < TODAY:
            fred_rows.append(f"{dd.isoformat()},{'.' if k % 7 == 3 else 4.0 + k * 0.001:.3f}" if k % 7 != 3 else f"{dd.isoformat()},.")
            dd += dt.timedelta(days=1)
            k += 1
        self.add(um.fred_url("DGS10", start), "dgs10.csv", ("\n".join(fred_rows) + "\n").encode())
        fx = ["observation_date,DEXJPUS"] + [f"{d.isoformat()},{150 + i * 0.01:.2f}" for i, d in enumerate(business_days(800))]
        self.add(um.fred_url("DEXJPUS", TODAY - dt.timedelta(days=800)), "fx.csv", ("\n".join(fx) + "\n").encode())
        self.write_manifest()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def add(self, url, name, content):
        with open(os.path.join(self.fix, name), "wb") as f:
            f.write(content)
        self.manifest[url] = name

    def write_manifest(self):
        with open(os.path.join(self.fix, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.manifest, f)

    def run_main(self, *extra):
        return um.main(["--fixtures", self.fix, "--data-dir", self.data, *extra])

    def market(self):
        with open(os.path.join(self.data, "market.json"), encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------
    def test_full_update(self):
        self.assertEqual(self.run_main(), 0)
        m = self.market()
        a = m["stocks"]["6857"]
        self.assertEqual(a["source"], "yahoo")
        self.assertFalse(a["stale"])
        self.assertAlmostEqual(a["price"], round(self.p_a[-1], 2), places=2)
        # 1年リターン = 1年前（以前で最も近い営業日）からの上昇率
        dates = self.d2y
        target = dates[-1] - dt.timedelta(days=365)
        i = max(k for k, d in enumerate(dates) if d <= target)
        expect = (self.p_a[-1] / self.p_a[i] - 1) * 100
        self.assertAlmostEqual(a["ret_1y_pct"], round(expect, 2), places=2)
        self.assertLess(a["vol_1y_pct"], 0.01)  # 一定成長なのでボラはほぼ0
        self.assertEqual(a["high_52w"], a["price"])
        self.assertEqual(len(a["spark"]), len(a["spark_d"]))
        self.assertEqual(a["spark_d"][-1], a["date"])
        self.assertLessEqual(len(a["spark"]), 62)

        b = m["stocks"]["285A"]
        self.assertEqual(b["source"], "stooq")
        self.assertEqual(b["price"], self.p_b[-1])

        c = m["stocks"]["9999"]
        self.assertTrue(c["stale"])
        self.assertEqual(c["price"], 1234)
        self.assertEqual(c["source"], "research")

        self.assertEqual(m["indices"]["usdjpy"]["source"], "fred")
        self.assertEqual(m["indices"]["nikkei"]["source"], "yahoo")
        self.assertEqual(m["etfs"]["2621"]["price"], 1200.0)

        # 金利・個人向け国債の目安
        jgb = m["rates"]["jgb"]
        self.assertEqual(jgb["date"], "2026-10-21")
        self.assertEqual(jgb["curve"]["10"], 2.923)
        est = m["rates"]["kojinmuke_estimate"]
        self.assertEqual(est["hendo10"], math.floor(2.923 * 0.66 * 100) / 100)  # 1.92
        self.assertEqual(est["kotei5"], 2.25)   # 2.309-0.05=2.259 → 2.25
        self.assertEqual(est["kotei3"], 2.0)    # 2.032-0.03=2.002 → 2.00
        self.assertEqual(jgb["chg_10y_since"]["bp"], round((2.923 - 3.092) * 100, 1))
        self.assertIn("ust10y", m["rates"])
        self.assertGreater(m["rates"]["ust10y"]["value"], 4.0)

        # 投信
        f1 = m["funds"]["f1"]
        self.assertEqual(f1["source"], "toushin-lib")
        self.assertIsNotNone(f1["ret_5y_ann_pct"])
        self.assertAlmostEqual(f1["ret_5y_ann_pct"], round(((1.0004 ** 245) - 1) * 100, 0), delta=1.0)
        self.assertEqual(f1["sharpe_3y"] is None or isinstance(f1["sharpe_3y"], float), True)
        f2 = m["funds"]["f2"]
        # 分配金を再投資すると、価格が下がってもトータルリターンはほぼ0（下がらない）
        self.assertGreater(f2["ret_1y_pct"], -0.5)
        self.assertLess(f2["nav"], 10000)
        self.assertEqual(f2["dist_12m"], 100)
        f3 = m["funds"]["f3"]
        self.assertTrue(f3["stale"])
        self.assertEqual(f3["nav"], 12345)

        st = m["status"]
        self.assertEqual(st["stocks_ok"], 2)
        self.assertEqual(st["stocks_failed"], 1)
        self.assertEqual(st["funds_failed"], 0)
        self.assertEqual(st["funds_skipped"], 1)

        # bundle.js が JS として読める形か
        with open(os.path.join(self.data, "bundle.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("window.RESEARCH=", js)
        self.assertIn("window.MARKET=", js)
        payload = js.split("window.MARKET=", 1)[1].rsplit(";", 1)[0]
        self.assertEqual(json.loads(payload)["stocks"]["6857"]["price"], a["price"])

    def test_failure_keeps_previous_values(self):
        self.assertEqual(self.run_main(), 0)
        first = self.market()
        # Yahoo の 6857 が落ちたことにする（Stooq も無い）
        del self.manifest[um.yahoo_chart_url("6857.T")]
        self.write_manifest()
        self.assertEqual(self.run_main(), 0)
        m = self.market()
        self.assertTrue(m["stocks"]["6857"]["stale"])
        self.assertEqual(m["stocks"]["6857"]["price"], first["stocks"]["6857"]["price"])

    def test_all_fail_does_not_overwrite(self):
        self.assertEqual(self.run_main(), 0)
        with open(os.path.join(self.data, "market.json"), encoding="utf-8") as f:
            before = f.read()
        self.manifest = {}
        self.write_manifest()
        self.assertEqual(self.run_main(), 1)
        with open(os.path.join(self.data, "market.json"), encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_offline_build(self):
        self.assertEqual(um.main(["--offline", "--data-dir", self.data]), 0)
        m = self.market()
        self.assertEqual(m["stocks"]["6857"]["price"], 9999)
        self.assertTrue(m["stocks"]["6857"]["stale"])
        self.assertEqual(m["funds"]["f3"]["nav"], 12345)

    def test_era_dates(self):
        self.assertEqual(um.parse_era_date("R8.10.21"), dt.date(2026, 10, 21))
        self.assertEqual(um.parse_era_date("H31.4.26"), dt.date(2019, 4, 26))
        self.assertIsNone(um.parse_era_date(""))


if __name__ == "__main__":
    unittest.main()
