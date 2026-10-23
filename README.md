# 予算ナビ（成長株・投資信託・債券）

三菱UFJ eスマート証券で注文できる **国内株式（成長株）・投資信託・債券** を、入力した予算に合わせて評価・予測して表示する HTML ツールです。
GitHub Pages で公開し、GitHub Actions が平日1日2回、株価・基準価額・金利を自動で更新します。

> 本ツールは公開情報をもとにした参考情報で、投資助言ではありません。三菱UFJ eスマート証券とは関係のない個人用ツールです。

## できること

- 予算を入れると、その金額で買える銘柄・ファンド・債券と、買える数量を表示
- 方針（成長重視／バランス／安定重視）に合わせて、各銘柄を「秀・優・良・可・低」の5段階で相対評価
- 株式：決算・会社予想・アナリスト目標株価・PER/PBR/ROE などの指標、1年の株価、予測レンジ
- 投資信託：信託報酬・リターン・変動率・シャープレシオ・最大下落率、連動指数の各社見通し
- 債券：個人向け国債の利率と税引後利回り、次回利率の目安（市場金利から自動計算）、国債利回り曲線
- 配分案：予算を株式・投信・債券に分け、点数の高い順に機械的に選んだ例

## ファイル

| ファイル | 役割 | 更新 |
|---|---|---|
| `index.html` | 画面 | 手動 |
| `research.json` | 調査データ（決算・会社予想・アナリスト予想・ファンド情報・債券・相場見通し） | 決算シーズンごとに手動 |
| `market.json` / `bundle.js` | 価格データと、画面が読むまとめファイル | 自動（GitHub Actions） |
| `update_market.py` | 価格を取得して `market.json` と `bundle.js` を作るスクリプト | — |
| `test_update_market.py` | 上のスクリプトのテスト（ネット不要） | — |
| `requirements.txt` | yfinance（任意。無くても動きます） | — |
| `.github/workflows/update.yml` | 自動更新と GitHub Pages への公開（下の「貼り付け用」と同じ内容） | — |

## 公開の手順（最初の1回だけ・スマホでも可）

1. **リポジトリを作る**：ブラウザで github.com を開き（GitHubアプリではなく Safari や Chrome）、右上の「＋」→「New repository」。名前は `yosan-navi`、**Public** を選んで「Create repository」。
2. **ZIPを展開**：受け取った ZIP を保存して展開します（iPhone はファイルアプリで ZIP をタップ）。中身は8ファイルです。
3. **8ファイルをアップロード**：リポジトリの画面で「uploading an existing file」（または Add file → Upload files）→「choose your files」で8ファイルをまとめて選び、「Commit changes」。
4. **Pages の設定**：Settings → Pages →「Build and deployment」の Source を **GitHub Actions** に変更（選ぶだけで保存されます）。
5. **自動更新の設定**：Actions タブ →「set up a workflow yourself」→ 最初から入っている文字をすべて消し、下の「貼り付け用」を貼り付け →「Commit changes...」→「Commit changes」。ここで初回の実行が自動で始まります。
6. **待つ**：Actions タブで緑のチェックが付くまで2〜5分。
7. **開く**：Settings → Pages に表示される `https://<ユーザー名>.github.io/yosan-navi/` を開きます。iPhone は共有 →「ホーム画面に追加」でアプリのように使えます。

### うまくいかないとき

- **赤い×（Pages 関連のエラー）**：手順4の Source が GitHub Actions になっているか確認し、Actions のその実行を開いて「Re-run all jobs」。
- **「変更を保存」で 403 / Permission denied**：Settings → Actions → General →「Workflow permissions」を **Read and write permissions** にして Save → Re-run。
- **画面に「データを読み込めませんでした」**：`bundle.js` がアップロードされているか確認。
- スマホでボタンが見つからないときは、Safari の「ぁあ」→「デスクトップ用Webサイトを表示」。

### 貼り付け用（.github/workflows/update.yml と同じ内容）

```yaml
name: 市場データ更新とページ公開

on:
  schedule:
    - cron: "23 7 * * 1-5"    # 平日 16:23（日本時間）
    - cron: "47 22 * * 0-4"   # 平日 07:47（日本時間）
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: market-update
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 準備
        run: pip install -r requirements.txt || echo "yfinance なしで続行します"

      - name: テスト
        continue-on-error: true
        run: if [ -f test_update_market.py ]; then python -m unittest test_update_market; fi

      - name: 市場データを取得
        id: fetch
        continue-on-error: true
        run: python update_market.py

      - name: 取得できなかったときは前回のデータで作る
        if: steps.fetch.outcome == 'failure'
        run: python update_market.py --offline

      - name: 変更を保存
        continue-on-error: true
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add market.json bundle.js
          if git diff --cached --quiet; then echo "変更なし"; exit 0; fi
          git commit -m "data: 市場データ更新 $(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M')"
          git pull --rebase --autostash origin "${GITHUB_REF_NAME}"
          git push

      - name: 公開するファイルをまとめる
        run: |
          mkdir -p _site
          cp index.html bundle.js research.json _site/
          cp market.json _site/ 2>/dev/null || true
          touch _site/.nojekyll

      - uses: actions/configure-pages@v5

      - uses: actions/upload-pages-artifact@v4
        with:
          path: _site

      - name: GitHub Pages に公開
        id: deployment
        uses: actions/deploy-pages@v4

      - name: 取得失敗のお知らせ
        if: steps.fetch.outcome == 'failure'
        run: |
          echo "::error::市場データを取得できませんでした（前回のデータのまま公開しています）"
          exit 1
```

## 自動更新のしくみ

- 平日の **16:23** と **07:47**（日本時間）に実行します（GitHub が混んでいると数十分遅れることがあります）。`main` に push したときも実行されます。
- 取得元
  - 株価・ETF・指数・為替：Yahoo Finance（yfinance → チャートAPI の順）、だめなら Stooq
  - 投資信託の基準価額：投資信託協会「投信総合検索ライブラリー」の CSV（分配金を再投資したとみなしてリターンを計算）
  - 日本国債の利回り：財務省「国債金利情報」CSV
  - 米国10年国債利回り：FRED（DGS10）
  - アナリスト目標株価（任意）：yfinance で取れた銘柄だけ、調査時点の値より優先して使用
- 取れなかった項目は前回の値のまま表示し、画面上部にお知らせが出ます。
- 1件も取れなかった場合は Actions が失敗扱いになり、GitHub からメールが届きます（データは前回のまま）。

## 調査データ（research.json）の更新

株価は自動で更新されますが、**決算・会社予想・アナリスト予想・ファンドの説明は research.json に書かれた調査時点の値**です。決算発表（5月・8月・11月・2月）のあとに更新するのがおすすめです。

- Claude に research.json を渡して「最新の決算と予想で更新して」と頼むのがいちばん簡単です。
- 手で編集して commit すると、Actions が自動で `bundle.js` を作り直してページに反映します。
- 銘柄を増やすときは `stocks` に同じ形の項目を足します（`code` だけでも株価は自動取得されます）。
- 投資信託は `isin`（ISINコード）と `assoc_code`（投資信託協会のファンドコード）がそろっている7本（eMAXIS Slim の全世界株式・米国株式・先進国株式・国内株式TOPIX・国内株式日経平均・国内債券・先進国債券）の基準価額を自動取得します。残りの11本は調査時点（2026年10月22日）の値を表示しているので、`isin` を書き足すと自動更新の対象になります。

### 収録している銘柄・ファンド（2026年10月23日時点）

- 国内株式（成長株30銘柄）：アドバンテスト、東京エレクトロン、ディスコ、レーザーテック、イビデン、キオクシアHD、フジクラ、三菱重工業、川崎重工業、ソフトバンクグループ、日立製作所、ソニーグループ、キーエンス、富士通、NEC、野村総合研究所、ベイカレント、リクルートHD、メルカリ、freee、マネーフォワード、Sansan、任天堂、カプコン、バンダイナムコHD、ANYCOLOR、第一三共、中外製薬、ファーストリテイリング、パン・パシフィック・インターナショナルHD
- 投資信託（18本）：eMAXIS Slim シリーズ（全世界株式・米国株式・先進国株式・国内株式TOPIX/日経平均・新興国株式・バランス8資産・国内債券・先進国債券・国内/先進国リート）、日経平均高配当利回り株ファンドⅡ、SBI・V・米国高配当株式、たわらノーロード先進国債券（ヘッジあり）、三菱UFJ純金ファンド、SBI・iシェアーズ・ゴールド、iFreeNEXT FANG+、ひふみプラス
- 債券：個人向け国債（変動10年・固定5年・固定3年の10月募集分）、新発の円建て社債、米国債（既発）と、債券ETF・投信

## 評価と予測の考え方

- 各項目（成長性・収益性・割安度・財務・アナリスト評価・株価の勢い・値動きの安定・配当）を、**この一覧の中での位置（0〜100点）** に直し、方針ごとの重みで平均して総合点にしています。順位で上位15%が「秀」、40%までが「優」、70%までが「良」、90%までが「可」、残りが「低」です。
- 予測は次の4種類を分けて表示しています。どれも将来を保証するものではありません。
  - アナリスト目標株価（平均・最高・最安）
  - 会社予想（決算短信の今期見通し）
  - 値動きの目安：過去1年の変動率から計算した「1年でこのくらい動きうる」幅（±1σ、約68%）
  - 各社の見通し：証券会社・調査機関が公表した株価指数・為替の予想

## ローカルで試す

```bash
python -m unittest test_update_market   # テスト（ネット不要）
python update_market.py --offline       # research.json だけ直したとき bundle.js を作り直す
python update_market.py                 # ネットから価格を取得
```

`index.html` をブラウザで開けば、同じフォルダの `bundle.js` を読んで表示します。

## 注意

- 掲載情報の正確性・完全性は保証されません。注文の前に、価格・手数料・目論見書・契約締結前交付書面を必ず証券会社の画面で確認してください。
- 取得元サイトの利用規約の範囲で、個人利用にとどめてください。
