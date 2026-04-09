"""
鹏华ETF全市场跟踪平台 v2
- 自动识别全部鹏华场内ETF
- 同指数同业竞品对比（规模/费率/溢价率）
- 营销报告生成
"""

from flask import Flask, jsonify, render_template_string, request, session, redirect
import threading, time, os, functools
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "phfund2026etf")
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "etfgogogo")

# ── 全局缓存 ──
cache = {
    "status": "idle",
    "error": "",
    "last_update": None,
    "penghua_etf": [],      # 鹏华全部场内ETF
    "peer_compare": {},     # 同业对比 {etf_code: [竞品列表]}
    "etf_spot": [],         # 全市场ETF行情快照
    "index_perf": {},       # 指数表现 {index_code: {...}}
}
lock = threading.Lock()


# ── 登录 ──
def login_required(f):
    @functools.wraps(f)
    def wrap(*args, **kwargs):
        if not session.get("ok"):
            return redirect("/login") if not request.path.startswith("/api/") else (jsonify({"error": "未登录"}), 401)
        return f(*args, **kwargs)
    return wrap

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("password") == ACCESS_PASSWORD:
            session["ok"] = True
            return redirect("/")
        err = "密码错误"
    return render_template_string(LOGIN_HTML, error=err)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── 数据拉取核心逻辑 ──
def fetch_all():
    with lock:
        cache["status"] = "loading"
        cache["error"] = ""

    try:
        import akshare as ak
        import pandas as pd

        # ── 1. 全市场ETF实时行情 ──
        print("[1/4] 拉取全市场ETF行情...")
        spot_df = ak.fund_etf_spot_em()
        spot_records = spot_df.fillna("").to_dict(orient="records")

        # ── 2. 识别鹏华ETF ──
        print("[2/4] 识别鹏华ETF...")
        # 从天天基金ETF列表拉管理人信息
        try:
            info_df = ak.fund_etf_fund_info_em()
            # 筛选鹏华
            ph_info = info_df[info_df.apply(
                lambda r: "鹏华" in str(r.values), axis=1
            )].copy()
        except Exception:
            ph_info = pd.DataFrame()

        # 从行情数据名称识别（兜底）
        ph_spot = spot_df[spot_df["名称"].str.contains("鹏华", na=False)].copy()

        # 合并：以行情数据为主，补充info
        ph_codes = set(ph_spot["代码"].tolist())
        if not ph_info.empty and "基金代码" in ph_info.columns:
            ph_codes |= set(ph_info["基金代码"].tolist())

        # 构建鹏华ETF列表
        penghua_list = []
        for _, row in ph_spot.iterrows():
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            # 尝试从info_df获取跟踪指数
            index_code, index_name, fee = "", "", ""
            if not ph_info.empty:
                match = ph_info[ph_info.get("基金代码", pd.Series(dtype=str)) == code]
                if not match.empty:
                    r2 = match.iloc[0]
                    index_code = str(r2.get("跟踪标的", r2.get("基准指数代码", "")))
                    index_name = str(r2.get("跟踪标的名称", r2.get("基准指数名称", "")))
                    fee = str(r2.get("管理费率", ""))

            penghua_list.append({
                "code": code,
                "name": name,
                "price": _safe_float(row.get("最新价")),
                "pct_chg": _safe_float(row.get("涨跌幅")),
                "premium": _safe_float(row.get("折溢价率")),
                "turnover": row.get("成交额", ""),
                "aum": _safe_float(row.get("规模(亿)", row.get("规模", ""))),
                "index_code": index_code,
                "index_name": index_name,
                "fee": fee,
            })

        # ── 3. 同业对比（按跟踪指数分组） ──
        print("[3/4] 生成同业对比...")
        peer_compare = {}
        # 按指数分组全市场ETF
        index_groups = {}
        for rec in spot_records:
            # 尝试从名称推断指数（简单匹配）
            rec_name = str(rec.get("名称", ""))
            rec_code = str(rec.get("代码", ""))
            index_groups.setdefault(rec_code, rec)

        # 对每只鹏华ETF，找同类竞品
        # 方法：从info_df按跟踪指数代码分组
        if not ph_info.empty and "跟踪标的" in ph_info.columns:
            for ph in penghua_list:
                idx = ph["index_code"]
                if not idx:
                    continue
                # 找同指数其他ETF
                peers_info = info_df[
                    info_df.get("跟踪标的", pd.Series(dtype=str)).astype(str).str.contains(idx, na=False)
                ] if not info_df.empty else pd.DataFrame()

                peers = []
                for _, pr in peers_info.iterrows():
                    pc = str(pr.get("基金代码", ""))
                    # 从spot找行情
                    spot_match = spot_df[spot_df["代码"] == pc]
                    aum = _safe_float(pr.get("规模(亿)", ""))
                    prem = 0.0
                    price = 0.0
                    if not spot_match.empty:
                        aum = aum or _safe_float(spot_match.iloc[0].get("规模(亿)", spot_match.iloc[0].get("规模", "")))
                        prem = _safe_float(spot_match.iloc[0].get("折溢价率", 0))
                        price = _safe_float(spot_match.iloc[0].get("最新价", 0))

                    peers.append({
                        "code": pc,
                        "name": str(pr.get("基金名称", pr.get("名称", ""))),
                        "manager": str(pr.get("基金公司", pr.get("管理人", ""))),
                        "aum": aum,
                        "fee": str(pr.get("管理费率", "")),
                        "premium": prem,
                        "price": price,
                        "is_penghua": "鹏华" in str(pr.get("基金公司", pr.get("管理人", ""))),
                    })
                # 按规模排序
                peers.sort(key=lambda x: x["aum"] or 0, reverse=True)
                peer_compare[ph["code"]] = peers

        # 如果info_df没有跟踪指数字段，用名称关键词匹配做简单同业
        if not peer_compare:
            for ph in penghua_list:
                # 取ETF名称中"ETF鹏华"前的关键词
                kw = ph["name"].replace("ETF鹏华", "").replace("鹏华", "").strip()
                kw = kw[:4] if len(kw) > 4 else kw
                if not kw:
                    continue
                peers = []
                for rec in spot_records:
                    if kw in str(rec.get("名称", "")):
                        peers.append({
                            "code": str(rec.get("代码", "")),
                            "name": str(rec.get("名称", "")),
                            "manager": "",
                            "aum": _safe_float(rec.get("规模(亿)", rec.get("规模", ""))),
                            "fee": "",
                            "premium": _safe_float(rec.get("折溢价率", 0)),
                            "price": _safe_float(rec.get("最新价", 0)),
                            "is_penghua": "鹏华" in str(rec.get("名称", "")),
                        })
                peers.sort(key=lambda x: x["aum"] or 0, reverse=True)
                peer_compare[ph["code"]] = peers[:10]

        # ── 4. 指数行情（鹏华ETF对应的指数） ──
        print("[4/4] 拉取指数表现...")
        index_perf = {}
        done_indices = set()
        end = date.today()

        for ph in penghua_list[:20]:  # 限制前20个避免超时
            idx = ph["index_code"]
            if not idx or idx in done_indices:
                continue
            try:
                df = ak.index_zh_a_hist(
                    symbol=idx, period="daily",
                    start_date=end.replace(year=end.year - 1).strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
                if df.empty:
                    continue
                df = df.sort_values("日期")
                c = df["收盘"].values

                def r(n, c=c):
                    return round((c[-1]/c[-n]-1)*100, 2) if len(c) >= n else None

                roll = pd.Series(c).cummax()
                dd = round(((pd.Series(c)-roll)/roll).min()*100, 2)

                index_perf[idx] = {
                    "name": ph["index_name"] or idx,
                    "ret_1m": r(22), "ret_3m": r(63),
                    "ret_6m": r(126), "ret_1y": r(252),
                    "max_dd": dd, "price": round(float(c[-1]), 2),
                }
                done_indices.add(idx)
                time.sleep(0.3)
            except Exception as e:
                pass

        with lock:
            cache["penghua_etf"] = penghua_list
            cache["peer_compare"] = peer_compare
            cache["etf_spot"] = spot_records[:300]
            cache["index_perf"] = index_perf
            cache["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cache["status"] = "ok"
        print(f"完成！共识别鹏华ETF {len(penghua_list)} 只")

    except Exception as e:
        with lock:
            cache["status"] = "error"
            cache["error"] = str(e)
        print(f"错误: {e}")


def _safe_float(v):
    try:
        return round(float(str(v).replace("%", "").replace(",", "").strip()), 4)
    except Exception:
        return None


# ── API ──
@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    if cache["status"] == "loading":
        return jsonify({"ok": False, "msg": "加载中"})
    threading.Thread(target=fetch_all, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/status")
@login_required
def api_status():
    with lock:
        return jsonify({
            "status": cache["status"],
            "last_update": cache["last_update"],
            "error": cache["error"],
            "count": len(cache["penghua_etf"]),
        })

@app.route("/api/data")
@login_required
def api_data():
    with lock:
        return jsonify({
            "penghua_etf": cache["penghua_etf"],
            "peer_compare": cache["peer_compare"],
            "index_perf": cache["index_perf"],
            "last_update": cache["last_update"],
        })

@app.route("/api/etf_spot")
@login_required
def api_etf_spot():
    q = request.args.get("q", "").lower()
    with lock:
        data = cache["etf_spot"]
    if q:
        data = [r for r in data if q in str(r.get("名称","")).lower() or q in str(r.get("代码","")).lower()]
    return jsonify(data[:200])


# ── 页面 ──
@app.route("/")
@login_required
def index():
    return render_template_string(MAIN_HTML)


# ════════════════════════════════════════════
# 登录页
# ════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF平台</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f5f4f0;font-family:'Noto Sans SC',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;border:1px solid #e2e0d8;border-radius:10px;padding:52px 44px;width:380px}
.brand{font-family:'DM Mono',monospace;font-size:11px;color:#aaa;letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}
.title{font-size:20px;font-weight:500;margin-bottom:36px}
label{font-size:11px;color:#999;font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:6px}
input{width:100%;border:1px solid #e2e0d8;border-radius:5px;padding:11px 13px;font-size:14px;font-family:'Noto Sans SC',sans-serif;margin-bottom:22px;color:#1a1916;outline:none}
input:focus{border-color:#3d2b8a}
button{width:100%;background:#1a1916;color:#f0ede6;border:none;border-radius:5px;padding:12px;font-size:14px;font-family:'Noto Sans SC',sans-serif;cursor:pointer}
button:hover{background:#333}
.err{color:#c0392b;font-size:13px;margin-bottom:16px}
</style></head><body>
<div class="box">
  <div class="brand">鹏华基金管理有限公司</div>
  <div class="title">ETF全市场跟踪平台</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>团队访问密码</label>
    <input type="password" name="password" placeholder="请输入密码" autofocus>
    <button type="submit">进入</button>
  </form>
</div></body></html>"""


# ════════════════════════════════════════════
# 主页面
# ════════════════════════════════════════════
MAIN_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF · 全市场跟踪</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f4f0;--s:#fff;--b:#e2e0d8;--t:#1a1916;--mu:#7a7870;
  --ph:#3d2b8a;--phl:#eeebf8;--phb:#534AB7;
  --red:#c0392b;--redl:#fdf0ef;
  --grn:#2d5a3d;--grnl:#e8f0eb;
  --amb:#b7790f;--ambl:#fdf6e3;
  --mono:'DM Mono',monospace;--sans:'Noto Sans SC',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:var(--sans);color:var(--t);font-size:14px;line-height:1.6}
.shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}
.sb{background:var(--t);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto}
.main{padding:28px 32px;min-width:0}
.logo{padding:26px 24px 20px;border-bottom:1px solid #2a2826}
.logo-sub{font-family:var(--mono);font-size:10px;color:#555;letter-spacing:.12em;text-transform:uppercase;margin-bottom:5px}
.logo-title{font-size:15px;font-weight:500;color:#f0ede6;line-height:1.35}
.nav{padding:14px 0;flex:1}
.nl{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:#444;padding:10px 24px 5px}
.ni{display:flex;align-items:center;gap:9px;padding:9px 24px;font-size:13px;color:#888;cursor:pointer;border-left:2px solid transparent;transition:all .12s}
.ni:hover{color:#f0ede6;background:#1e1d1a}
.ni.on{color:#f0ede6;border-left-color:#8fbc9c;background:#1a1916}
.nd{width:5px;height:5px;border-radius:50%;background:#333;flex-shrink:0}
.ni.on .nd{background:#8fbc9c}
.sb-foot{padding:18px 24px;border-top:1px solid #2a2826}
.sdot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#444;margin-right:6px}
.sdot.ok{background:#5ab97a}.sdot.loading{background:#f0b429;animation:blink 1s infinite}.sdot.error{background:#e05a4e}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.panel{display:none}.panel.on{display:block}
.ph{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:22px;gap:12px}
.pt{font-size:20px;font-weight:500;letter-spacing:-.02em}
.ps{font-size:12px;color:var(--mu);margin-top:3px;font-family:var(--mono)}
.btns{display:flex;gap:7px;flex-shrink:0}
.btn{padding:7px 15px;border-radius:4px;border:1px solid var(--b);background:var(--s);cursor:pointer;font-size:13px;font-family:var(--sans);color:var(--t);transition:all .12s;white-space:nowrap}
.btn:hover{border-color:#888}.btn.pri{background:var(--t);color:#f0ede6;border-color:var(--t)}.btn.pri:hover{background:#333}
.btn.sm{padding:5px 11px;font-size:12px}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:20px}
.mc{background:var(--s);border:1px solid var(--b);border-radius:7px;padding:14px 16px}
.ml{font-size:11px;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.mv{font-size:22px;font-weight:300;letter-spacing:-.025em}
.mn{font-size:11px;color:var(--mu);margin-top:2px}
.up{color:var(--red)}.dn{color:#1a4a7a}.warn{color:var(--amb)}
.card{background:var(--s);border:1px solid var(--b);border-radius:7px;overflow:hidden;margin-bottom:16px}
.ch{padding:13px 18px;border-bottom:1px solid var(--b);display:flex;align-items:center;justify-content:space-between;gap:8px}
.ct{font-size:13px;font-weight:500;flex-shrink:0}
.cm{font-size:11px;color:var(--mu);font-family:var(--mono)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:9px 14px;font-size:11px;font-weight:400;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--b);background:#faf9f7;white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid #f0ede8;vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:#faf9f7}
.ph-row td{background:var(--phl)}.ph-row:hover td{background:#e5e1f5}
.badge{display:inline-block;padding:2px 7px;border-radius:3px;font-size:11px;font-family:var(--mono)}
.b-ph{background:var(--phl);color:var(--ph)}.b-g{background:var(--grnl);color:var(--grn)}
.b-r{background:var(--redl);color:var(--red)}.b-a{background:var(--ambl);color:var(--amb)}
.b-gray{background:#f0ede8;color:#555}
input,select{border:1px solid var(--b);border-radius:4px;padding:7px 10px;font-size:13px;font-family:var(--sans);background:var(--s);color:var(--t)}
input:focus,select:focus{outline:none;border-color:var(--ph)}
.mkt-card{border-left:3px solid var(--ph);background:var(--s);border-radius:0 7px 7px 0;border:1px solid var(--b);padding:16px 18px;margin-bottom:10px}
.mkt-n{font-family:var(--mono);font-size:10px;color:var(--mu);margin-bottom:4px}
.mkt-t{font-weight:500;font-size:14px;margin-bottom:7px}
.mkt-b{font-size:13px;color:#444;line-height:1.8}
#toast{position:fixed;bottom:20px;right:20px;background:var(--t);color:#f0ede6;padding:9px 16px;border-radius:4px;font-size:12px;font-family:var(--mono);opacity:0;transition:opacity .2s;pointer-events:none;z-index:999}
#toast.show{opacity:1}
.tag{font-size:10px;font-family:var(--mono);padding:2px 6px;border-radius:3px;white-space:nowrap}
.tag-ph{background:var(--phl);color:var(--ph)}
.tag-1st{background:#e8f0eb;color:#2d5a3d}
.empty{text-align:center;padding:48px;color:var(--mu);font-size:13px}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:#ccc;border-radius:2px}
</style></head>
<body>
<div class="shell">
<aside class="sb">
  <div class="logo">
    <div class="logo-sub">鹏华基金管理有限公司</div>
    <div class="logo-title">ETF全市场<br>跟踪平台</div>
  </div>
  <nav class="nav">
    <div class="nl">核心</div>
    <div class="ni on" onclick="nav(this,'overview')"><span class="nd"></span>鹏华ETF总览</div>
    <div class="ni" onclick="nav(this,'peer')"><span class="nd"></span>同业竞品对比</div>
    <div class="ni" onclick="nav(this,'index')"><span class="nd"></span>指数表现</div>
    <div class="nl">工具</div>
    <div class="ni" onclick="nav(this,'market')"><span class="nd"></span>全市场ETF</div>
    <div class="ni" onclick="nav(this,'report')"><span class="nd"></span>营销报告</div>
  </nav>
  <div class="sb-foot">
    <div style="font-size:12px;color:#666;font-family:var(--mono)">
      <span class="sdot" id="sdot"></span><span id="stxt">未加载</span>
    </div>
    <div style="font-size:11px;color:#444;margin-top:4px;font-family:var(--mono)" id="upd"></div>
    <div style="margin-top:10px"><a href="/logout" style="font-size:11px;color:#555;font-family:var(--mono)">退出</a></div>
  </div>
</aside>

<main class="main">

<!-- 总览 -->
<div class="panel on" id="p-overview">
  <div class="ph">
    <div><div class="pt">鹏华ETF总览</div><div class="ps">全部场内产品 · 自动识别</div></div>
    <div class="btns">
      <button class="btn sm" onclick="exportCsv()">导出CSV</button>
      <button class="btn sm pri" onclick="refresh()">刷新数据</button>
    </div>
  </div>
  <div class="metrics">
    <div class="mc"><div class="ml">鹏华ETF总数</div><div class="mv" id="m-count">—</div><div class="mn">场内产品</div></div>
    <div class="mc"><div class="ml">今日平均涨跌</div><div class="mv" id="m-avg">—</div><div class="mn">等权平均</div></div>
    <div class="mc"><div class="ml">规模最大产品</div><div class="mv" id="m-top" style="font-size:14px;margin-top:4px">—</div><div class="mn" id="m-top-aum"></div></div>
    <div class="mc"><div class="ml">溢价率异常(>1%)</div><div class="mv warn" id="m-prem">—</div><div class="mn">只需关注</div></div>
  </div>
  <div class="card">
    <div class="ch">
      <span class="ct">全部鹏华场内ETF</span>
      <input type="text" id="ph-q" placeholder="搜索..." style="width:160px" oninput="filterPh()">
    </div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>最新价</th><th>今日涨跌</th>
        <th>折溢价率</th><th>规模(亿)</th><th>跟踪指数</th><th>同业排名</th>
      </tr></thead>
      <tbody id="ph-body"><tr><td colspan="8" class="empty">点击「刷新数据」加载（约需1-2分钟）</td></tr></tbody>
    </table>
    </div>
  </div>
</div>

<!-- 同业对比 -->
<div class="panel" id="p-peer">
  <div class="ph">
    <div><div class="pt">同业竞品对比</div><div class="ps">按指数分组 · 规模排名</div></div>
  </div>
  <div style="margin-bottom:14px;display:flex;align-items:center;gap:10px">
    <label style="font-size:12px;color:var(--mu)">选择产品：</label>
    <select id="peer-sel" onchange="renderPeer()" style="width:280px"></select>
  </div>
  <div id="peer-content"><div class="card"><div class="empty">请先刷新数据，再选择产品</div></div></div>
</div>

<!-- 指数表现 -->
<div class="panel" id="p-index">
  <div class="ph">
    <div><div class="pt">指数表现</div><div class="ps">鹏华ETF对应指数 · 历史收益</div></div>
  </div>
  <div class="card">
    <div class="ch"><span class="ct">指数收益汇总</span><span class="cm" id="idx-time"></span></div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>指数代码</th><th>指数名称</th><th>近1月</th><th>近3月</th><th>近6月</th><th>近1年</th><th>最大回撤</th></tr></thead>
      <tbody id="idx-body"><tr><td colspan="7" class="empty">暂无数据</td></tr></tbody>
    </table>
    </div>
  </div>
</div>

<!-- 全市场ETF -->
<div class="panel" id="p-market">
  <div class="ph">
    <div><div class="pt">全市场ETF</div><div class="ps">实时行情 · 全量数据</div></div>
    <input type="text" id="mkt-q" placeholder="搜索名称/代码..." style="width:180px" oninput="filterMkt()">
  </div>
  <div class="card">
    <div class="ch"><span class="ct">全市场场内ETF</span><span class="cm" id="mkt-cnt"></span></div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th><th>成交额</th><th>折溢价率</th><th>规模(亿)</th></tr></thead>
      <tbody id="mkt-body"><tr><td colspan="7" class="empty">点击「刷新数据」加载</td></tr></tbody>
    </table>
    </div>
  </div>
</div>

<!-- 营销报告 -->
<div class="panel" id="p-report">
  <div class="ph">
    <div><div class="pt">营销报告</div><div class="ps">自动生成 · 可直接发送</div></div>
    <div class="btns">
      <button class="btn sm" onclick="copyReport()">复制全文</button>
      <button class="btn sm pri" onclick="exportReport()">导出TXT</button>
    </div>
  </div>
  <div id="report-content"></div>
</div>

</main>
</div>
<div id="toast"></div>

<script>
let D = {penghua_etf:[], peer_compare:{}, index_perf:{}, last_update:null};
let mktAll = [];

function nav(el, id) {
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  el.classList.add('on');
  document.getElementById('p-'+id).classList.add('on');
  if(id==='report') renderReport();
  if(id==='market') loadMkt();
}

function toast(msg, dur=2500) {
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), dur);
}

function setStatus(s, txt) {
  document.getElementById('sdot').className='sdot '+s;
  document.getElementById('stxt').textContent=txt;
}

// ── 刷新 ──
async function refresh() {
  setStatus('loading','加载中...');
  toast('正在拉取全市场数据，约需1-2分钟...',5000);
  await fetch('/api/refresh',{method:'POST'});
  const iv=setInterval(async()=>{
    const r=await fetch('/api/status').then(r=>r.json());
    document.getElementById('upd').textContent=r.last_update||'';
    if(r.status==='ok'){
      clearInterval(iv); setStatus('ok','数据正常');
      await loadData(); toast(`已加载 ${r.count} 只鹏华ETF ✓`);
    } else if(r.status==='error'){
      clearInterval(iv); setStatus('error','加载失败');
      toast('错误: '+r.error, 5000);
    }
  },2000);
}

async function loadData() {
  D=await fetch('/api/data').then(r=>r.json());
  renderOverview();
  renderPeerSel();
  renderIndex();
  if(D.last_update) document.getElementById('upd').textContent=D.last_update;
}

async function loadMkt() {
  const q=document.getElementById('mkt-q').value;
  const data=await fetch('/api/etf_spot?q='+encodeURIComponent(q)).then(r=>r.json());
  mktAll=data;
  renderMkt(data);
}

// ── 格式化 ──
function fp(v,dec=2){
  if(v==null||v==='')return'<span style="color:var(--mu)">—</span>';
  const n=parseFloat(v);
  if(isNaN(n))return'<span style="color:var(--mu)">—</span>';
  const s=n>=0?'+':'';
  return`<span class="${n>=0?'up':'dn'}">${s}${n.toFixed(dec)}%</span>`;
}
function fn(v,dec=2){
  if(v==null||v===''||isNaN(parseFloat(v)))return'—';
  return parseFloat(v).toFixed(dec);
}

// ── 总览 ──
function renderOverview() {
  const data=D.penghua_etf||[];
  if(!data.length){
    document.getElementById('ph-body').innerHTML='<tr><td colspan="8" class="empty">暂无数据，请刷新</td></tr>';
    return;
  }

  // 指标卡
  document.getElementById('m-count').textContent=data.length+'只';
  const chgs=data.filter(d=>d.pct_chg!=null).map(d=>d.pct_chg);
  const avg=chgs.length?chgs.reduce((a,b)=>a+b,0)/chgs.length:null;
  document.getElementById('m-avg').innerHTML=avg!=null?fp(avg):'—';
  const byAum=[...data].sort((a,b)=>(b.aum||0)-(a.aum||0));
  if(byAum[0]){
    document.getElementById('m-top').textContent=byAum[0].name;
    document.getElementById('m-top-aum').textContent=byAum[0].aum?byAum[0].aum.toFixed(1)+'亿':'—';
  }
  const premAbove=data.filter(d=>d.premium!=null&&Math.abs(d.premium)>1).length;
  document.getElementById('m-prem').textContent=premAbove+'只';

  renderPhTable(data);
}

function renderPhTable(data) {
  const peer=D.peer_compare||{};
  document.getElementById('ph-body').innerHTML=data.map(d=>{
    // 计算同业排名
    const peers=peer[d.code]||[];
    const rank=peers.findIndex(p=>p.code===d.code)+1;
    const total=peers.length;
    const rankStr=rank>0?`${rank}/${total}`:'—';
    const rankBadge=rank===1?'<span class="tag tag-1st">规模第1</span>':
                    rank>0&&rank<=3?`<span class="tag b-a">第${rank}/${total}</span>`:
                    rank>0?`<span class="tag b-gray">第${rank}/${total}</span>`:'—';
    return`<tr class="ph-row">
      <td style="font-family:var(--mono);font-size:12px">${d.code}</td>
      <td><b>${d.name}</b></td>
      <td style="font-family:var(--mono)">${fn(d.price)}</td>
      <td>${fp(d.pct_chg)}</td>
      <td>${d.premium!=null?fp(d.premium):'—'}</td>
      <td style="font-family:var(--mono)">${d.aum?d.aum.toFixed(2):'—'}</td>
      <td style="font-size:12px;color:var(--mu)">${d.index_name||d.index_code||'—'}</td>
      <td>${rankBadge}</td>
    </tr>`;
  }).join('');
}

function filterPh() {
  const q=document.getElementById('ph-q').value.toLowerCase();
  const filtered=(D.penghua_etf||[]).filter(d=>
    d.name.toLowerCase().includes(q)||d.code.toLowerCase().includes(q)||
    (d.index_name||'').toLowerCase().includes(q)
  );
  renderPhTable(filtered);
}

// ── 同业对比 ──
function renderPeerSel() {
  const sel=document.getElementById('peer-sel');
  const data=D.penghua_etf||[];
  sel.innerHTML=data.length?
    data.map(d=>`<option value="${d.code}">${d.name}（${d.code}）</option>`).join(''):
    '<option>暂无数据</option>';
  if(data.length) renderPeer();
}

function renderPeer() {
  const code=document.getElementById('peer-sel').value;
  const phEtf=(D.penghua_etf||[]).find(d=>d.code===code);
  const peers=(D.peer_compare||{})[code]||[];
  const el=document.getElementById('peer-content');

  if(!phEtf){el.innerHTML='<div class="card"><div class="empty">请先刷新数据</div></div>';return}

  const rows=peers.length?peers.map((p,i)=>`
    <tr class="${p.is_penghua?'ph-row':''}">
      <td style="font-family:var(--mono);color:var(--mu);font-size:12px">${i+1}</td>
      <td><b>${p.name}</b>${p.is_penghua?' <span class="badge b-ph">鹏华</span>':''}</td>
      <td style="font-family:var(--mono);font-size:12px">${p.code}</td>
      <td>${p.manager||'—'}</td>
      <td style="font-family:var(--mono);font-weight:${p.is_penghua?'500':'400'}">${p.aum?p.aum.toFixed(2)+'亿':'—'}</td>
      <td style="font-size:12px">${p.fee||'—'}</td>
      <td>${p.premium!=null?fp(p.premium):'—'}</td>
    </tr>`).join(''):
    '<tr><td colspan="7" class="empty">暂无同业数据（指数代码未识别）</td></tr>';

  el.innerHTML=`
    <div class="metrics" style="grid-template-columns:repeat(3,1fr)">
      <div class="mc"><div class="ml">产品名称</div><div class="mv" style="font-size:15px;margin-top:4px">${phEtf.name}</div></div>
      <div class="mc"><div class="ml">跟踪指数</div><div class="mv" style="font-size:15px;margin-top:4px">${phEtf.index_name||phEtf.index_code||'—'}</div></div>
      <div class="mc"><div class="ml">同业产品数</div><div class="mv">${peers.length}</div><div class="mn">含本产品</div></div>
    </div>
    <div class="card">
      <div class="ch"><span class="ct">同指数ETF对比（按规模排序）</span></div>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>#</th><th>产品名称</th><th>代码</th><th>管理人</th><th>规模(亿)</th><th>管理费率</th><th>折溢价率</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      </div>
    </div>`;
}

// ── 指数表现 ──
function renderIndex() {
  const data=D.index_perf||{};
  const keys=Object.keys(data);
  if(!keys.length){
    document.getElementById('idx-body').innerHTML='<tr><td colspan="7" class="empty">暂无数据</td></tr>';
    return;
  }
  if(D.last_update) document.getElementById('idx-time').textContent=D.last_update;
  document.getElementById('idx-body').innerHTML=keys.map(k=>{
    const d=data[k];
    return`<tr>
      <td style="font-family:var(--mono);font-size:12px;color:var(--mu)">${k}</td>
      <td><b>${d.name||k}</b></td>
      <td>${fp(d.ret_1m)}</td><td>${fp(d.ret_3m)}</td>
      <td>${fp(d.ret_6m)}</td><td style="font-weight:500">${fp(d.ret_1y)}</td>
      <td><span class="dn">${d.max_dd!=null?d.max_dd.toFixed(2)+'%':'—'}</span></td>
    </tr>`;
  }).join('');
}

// ── 全市场ETF ──
function renderMkt(data) {
  document.getElementById('mkt-cnt').textContent=data.length+'只';
  if(!data.length){
    document.getElementById('mkt-body').innerHTML='<tr><td colspan="7" class="empty">点击「刷新数据」加载</td></tr>';
    return;
  }
  document.getElementById('mkt-body').innerHTML=data.slice(0,200).map(d=>{
    const chg=parseFloat(d['涨跌幅']||0);
    const prem=parseFloat(d['折溢价率']||0);
    return`<tr>
      <td style="font-family:var(--mono);font-size:12px">${d['代码']||'—'}</td>
      <td>${d['名称']||'—'}</td>
      <td style="font-family:var(--mono)">${fn(d['最新价'])}</td>
      <td>${fp(chg)}</td>
      <td style="font-size:12px;color:var(--mu)">${d['成交额']||'—'}</td>
      <td>${fp(prem)}</td>
      <td style="font-family:var(--mono)">${d['规模']||d['规模(亿)']||'—'}</td>
    </tr>`;
  }).join('');
}

async function filterMkt() {
  const q=document.getElementById('mkt-q').value;
  const data=await fetch('/api/etf_spot?q='+encodeURIComponent(q)).then(r=>r.json());
  renderMkt(data);
}

// ── 营销报告 ──
function renderReport() {
  const data=D.penghua_etf||[];
  const peer=D.peer_compare||{};
  const idx=D.index_perf||{};

  if(!data.length){
    document.getElementById('report-content').innerHTML=
      '<div class="card"><div class="empty">请先刷新数据，再生成报告</div></div>';
    return;
  }

  // 按规模排序
  const sorted=[...data].sort((a,b)=>(b.aum||0)-(a.aum||0));
  const date=D.last_update||new Date().toLocaleDateString('zh');

  // 生成每个产品的简报
  const cards=sorted.slice(0,10).map(d=>{
    const peers=peer[d.code]||[];
    const rank=peers.findIndex(p=>p.code===d.code)+1;
    const total=peers.length;
    const idxData=idx[d.index_code]||null;
    const rankStr=rank===1?'规模行业第一':rank>0?`规模行业第${rank}/${total}`:'';
    const ret1y=idxData?.ret_1y;
    return`
    <div class="mkt-card">
      <div class="mkt-n">${d.code} · ${d.index_name||'—'}</div>
      <div class="mkt-t">${d.name}${rankStr?' <span class="badge b-g">'+rankStr+'</span>':''}</div>
      <div class="mkt-b">
        ${d.aum?`当前规模 <b>${d.aum.toFixed(2)}亿元</b>，`:''}
        ${d.pct_chg!=null?`今日${d.pct_chg>=0?'上涨':'下跌'} <b>${Math.abs(d.pct_chg).toFixed(2)}%</b>，`:''}
        ${d.premium!=null?`折溢价率 <b>${d.premium>=0?'+':''}${d.premium.toFixed(2)}%</b>`:''}
        ${idxData&&ret1y!=null?`<br>跟踪指数近一年表现：<b>${ret1y>=0?'+':''}${ret1y.toFixed(2)}%</b>`:''}
        ${peers.length>1?`<br>同指数共 ${total} 只ETF竞品，${rank===1?'本产品规模领先':'规模排名第'+rank}`:''}
      </div>
    </div>`;
  }).join('');

  document.getElementById('report-content').innerHTML=`
    <div style="background:var(--s);border:1px solid var(--b);border-radius:7px;padding:16px 20px;margin-bottom:14px;font-size:13px;color:var(--mu)">
      数据时间：${date} &nbsp;·&nbsp; 共识别鹏华场内ETF ${data.length} 只 &nbsp;·&nbsp;
      以下展示规模前10只产品简报
    </div>
    ${cards}`;
}

function copyReport() {
  const txt=document.getElementById('report-content').innerText;
  navigator.clipboard.writeText(txt).then(()=>toast('已复制 ✓'));
}

function exportReport() {
  const txt=document.getElementById('report-content').innerText;
  if(!txt.trim()){toast('请先生成报告');return}
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([txt],{type:'text/plain;charset=utf-8'}));
  a.download=`鹏华ETF报告_${new Date().toLocaleDateString('zh')}.txt`;
  a.click(); toast('已导出 ✓');
}

function exportCsv() {
  const data=D.penghua_etf||[];
  if(!data.length){toast('暂无数据');return}
  let csv='\ufeff代码,名称,最新价,涨跌幅(%),折溢价率(%),规模(亿),跟踪指数代码,跟踪指数名称\n';
  data.forEach(d=>csv+=`${d.code},${d.name},${d.price||''},${d.pct_chg||''},${d.premium||''},${d.aum||''},${d.index_code||''},${d.index_name||''}\n`);
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download=`鹏华ETF_${new Date().toLocaleDateString('zh')}.csv`;
  a.click(); toast('已导出 ✓');
}

window.onload=async()=>{
  const s=await fetch('/api/status').then(r=>r.json());
  if(s.status==='ok'){setStatus('ok','数据正常');await loadData()}
  else setStatus('idle','待加载');
};
</script>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"  鹏华ETF平台 v2 → http://localhost:{port}")
    print(f"  密码: {ACCESS_PASSWORD}")
    app.run(host="0.0.0.0", port=port, debug=False)
