"""
鹏华ETF指数跟踪平台
支持：AKShare实时拉取 + 手动录入 + 密码保护
部署：Railway / 阿里云 / 腾讯云
"""

from flask import Flask, jsonify, render_template_string, request, session, redirect
import threading, time, json, os
from datetime import datetime, date, timedelta
import functools

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "phfund2024etf")

# ── 访问密码（环境变量设置，默认 penghua2024） ──
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "penghua2024")

# ── 全局缓存 ──
cache = {
    "last_update": None,
    "index_perf": [],
    "etf_spot": [],
    "status": "idle",
    "error": "",
}
cache_lock = threading.Lock()

FOCUS_INDICES = {
    "932368": "800现金流",
    "932365": "中证现金流",
    "980092": "国证自由现金流",
    "000922": "中证红利",
    "H30269": "红利低波",
    "000015": "上证红利",
}
PENGHUA_NAMES = {"800现金流", "中证现金流"}
CF_NAMES = {"800现金流", "中证现金流", "国证自由现金流", "富时现金流"}
DIV_NAMES = {"中证红利", "上证红利", "红利低波"}


# ── 登录保护 ──
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ACCESS_PASSWORD:
            session["logged_in"] = True
            return redirect("/")
        error = "密码错误"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── 数据拉取 ──
def fetch_all():
    with cache_lock:
        cache["status"] = "loading"
        cache["error"] = ""

    results, etf_spot = [], []
    try:
        import akshare as ak

        try:
            spot_df = ak.fund_etf_spot_em()
            etf_spot = spot_df.head(500).fillna("").to_dict(orient="records")
        except Exception as e:
            pass

        end = date.today()
        import pandas as pd

        for code, name in FOCUS_INDICES.items():
            try:
                df = ak.index_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=end.replace(year=end.year - 1).strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
                if df.empty:
                    continue
                df = df.sort_values("日期")
                close = df["收盘"].values

                def ret(n, c=close):
                    return round((c[-1] / c[-n] - 1) * 100, 2) if len(c) >= n else None

                roll_max = pd.Series(close).cummax()
                drawdown = round(((pd.Series(close) - roll_max) / roll_max).min() * 100, 2)

                results.append({
                    "code": code, "name": name,
                    "is_penghua": name in PENGHUA_NAMES,
                    "is_cf": name in CF_NAMES,
                    "ret_1m": ret(22), "ret_3m": ret(63),
                    "ret_6m": ret(126), "ret_1y": ret(252),
                    "max_dd": drawdown,
                    "price": round(float(close[-1]), 2),
                    "source": "akshare",
                })
                time.sleep(0.35)
            except Exception as e:
                results.append({"code": code, "name": name,
                                 "is_penghua": name in PENGHUA_NAMES,
                                 "error": str(e), "source": "error"})

        with cache_lock:
            # 保留手动录入的条目（manual=True），用实时数据覆盖同名条目
            manual = [r for r in cache["index_perf"] if r.get("manual")]
            manual_names = {r["name"] for r in manual}
            merged = results + [r for r in manual if r["name"] not in {x["name"] for x in results}]
            cache["index_perf"] = merged
            cache["etf_spot"] = etf_spot
            cache["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cache["status"] = "ok"

    except Exception as e:
        with cache_lock:
            cache["status"] = "error"
            cache["error"] = str(e)


# ── API ──
@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    if cache["status"] == "loading":
        return jsonify({"ok": False, "msg": "正在加载中"})
    threading.Thread(target=fetch_all, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/status")
@login_required
def api_status():
    with cache_lock:
        return jsonify({
            "status": cache["status"],
            "last_update": cache["last_update"],
            "error": cache["error"],
            "count": len(cache["index_perf"]),
        })


@app.route("/api/data")
@login_required
def api_data():
    with cache_lock:
        return jsonify({
            "index_perf": cache["index_perf"],
            "etf_spot": cache["etf_spot"][:200],
            "last_update": cache["last_update"],
        })


@app.route("/api/manual", methods=["POST"])
@login_required
def api_manual():
    row = request.json
    if not row or "name" not in row:
        return jsonify({"ok": False, "msg": "缺少name"})
    row["is_penghua"] = row.get("name") in PENGHUA_NAMES
    row["is_cf"] = row.get("name") in CF_NAMES
    row["manual"] = True
    row["source"] = "manual"
    with cache_lock:
        existing = [r for r in cache["index_perf"] if r["name"] != row["name"]]
        existing.append(row)
        cache["index_perf"] = existing
        cache["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"ok": True})


@app.route("/api/delete", methods=["POST"])
@login_required
def api_delete():
    name = request.json.get("name")
    with cache_lock:
        cache["index_perf"] = [r for r in cache["index_perf"] if r["name"] != name]
    return jsonify({"ok": True})


# ── 页面 ──
@app.route("/")
@login_required
def index():
    return render_template_string(MAIN_HTML)


# ────────────────────────────────────────────
# 登录页 HTML
# ────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF平台 · 登录</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f5f4f0;font-family:'Noto Sans SC',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;border:1px solid #e2e0d8;border-radius:8px;padding:48px 40px;width:360px}
.brand{font-family:'DM Mono',monospace;font-size:11px;color:#999;letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}
.title{font-size:20px;font-weight:500;margin-bottom:32px;color:#1a1916}
label{font-size:11px;color:#888;font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:6px}
input{width:100%;border:1px solid #e2e0d8;border-radius:4px;padding:10px 12px;font-size:14px;font-family:'Noto Sans SC',sans-serif;margin-bottom:20px;color:#1a1916}
input:focus{outline:none;border-color:#3d2b8a}
button{width:100%;background:#1a1916;color:#f0ede6;border:none;border-radius:4px;padding:11px;font-size:14px;font-family:'Noto Sans SC',sans-serif;cursor:pointer}
button:hover{background:#333}
.error{color:#c0392b;font-size:13px;margin-bottom:16px}
</style></head><body>
<div class="box">
  <div class="brand">鹏华基金</div>
  <div class="title">场内ETF跟踪平台</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>访问密码</label>
    <input type="password" name="password" placeholder="请输入团队密码" autofocus>
    <button type="submit">进入平台</button>
  </form>
</div>
</body></html>"""


# ────────────────────────────────────────────
# 主页面 HTML
# ────────────────────────────────────────────
MAIN_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF · 指数跟踪平台</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#f5f4f0;--s:#fff;--b:#e2e0d8;--t:#1a1916;--mu:#7a7870;--ac:#2d5a3d;--acl:#e8f0eb;--red:#c0392b;--redl:#fdf0ef;--amb:#b7790f;--ambl:#fdf6e3;--blu:#1a4a7a;--blul:#eaf0f8;--ph:#3d2b8a;--phl:#eeebf8;--mono:'DM Mono',monospace;--sans:'Noto Sans SC',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:var(--sans);color:var(--t);font-size:14px;line-height:1.6}
.shell{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
.sidebar{background:var(--t);color:#e8e6e0;position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column}
.main{padding:28px 32px;overflow-x:hidden}
.logo{padding:24px 24px 18px;border-bottom:1px solid #2e2c28}
.logo-mark{font-family:var(--mono);font-size:10px;color:#555;letter-spacing:.12em;text-transform:uppercase;margin-bottom:5px}
.logo-name{font-size:15px;font-weight:500;color:#f0ede6;line-height:1.3}
.nav{padding:12px 0}
.nav-label{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:#444;padding:10px 24px 5px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 24px;font-size:13px;color:#999;cursor:pointer;border-left:2px solid transparent;transition:all .15s}
.nav-item:hover{color:#f0ede6;background:#1e1d1a}
.nav-item.active{color:#f0ede6;border-left-color:#8fbc9c;background:#1e1d1a}
.nav-dot{width:5px;height:5px;border-radius:50%;background:#444;flex-shrink:0}
.nav-item.active .nav-dot{background:#8fbc9c}
.sidebar-foot{padding:18px 24px;border-top:1px solid #2e2c28;margin-top:auto}
.spill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-family:var(--mono);color:#666}
.sdot{width:6px;height:6px;border-radius:50%;background:#444}
.sdot.ok{background:#5ab97a}.sdot.loading{background:#f0b429;animation:pulse 1s infinite}.sdot.error{background:#e05a4e}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.panel{display:none}.panel.active{display:block}
.ph{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:24px}
.pt{font-size:21px;font-weight:500;letter-spacing:-.02em}
.ps{font-size:12px;color:var(--mu);margin-top:3px;font-family:var(--mono)}
.btns{display:flex;gap:8px}
.btn{padding:7px 16px;border-radius:4px;border:1px solid var(--b);background:var(--s);cursor:pointer;font-size:13px;font-family:var(--sans);color:var(--t);transition:all .15s}
.btn:hover{border-color:var(--t)}.btn.pri{background:var(--t);color:#f0ede6;border-color:var(--t)}.btn.pri:hover{background:#333}
.btn.sm{padding:5px 12px;font-size:12px}.btn.danger{border-color:#e05a4e;color:#c0392b}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}
.mc{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:14px 16px}
.ml{font-size:11px;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}
.mv{font-size:24px;font-weight:300;letter-spacing:-.03em}
.mn{font-size:11px;color:var(--mu);margin-top:3px}
.up{color:var(--red)}.dn{color:var(--blu)}.warn{color:var(--amb)}
.card{background:var(--s);border:1px solid var(--b);border-radius:6px;overflow:hidden;margin-bottom:18px}
.ch{padding:13px 18px;border-bottom:1px solid var(--b);display:flex;align-items:center;justify-content:space-between}
.ct{font-size:13px;font-weight:500}.cm{font-size:11px;color:var(--mu);font-family:var(--mono)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:9px 14px;font-size:11px;font-weight:400;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--b);background:#faf9f7}
td{padding:10px 14px;border-bottom:1px solid #f0ede8;font-size:13px}
tr:last-child td{border-bottom:none}tr:hover td{background:#faf9f7}
.pr-row td{background:var(--phl)}.pr-row:hover td{background:#e5e1f5}
.badge{display:inline-block;padding:2px 7px;border-radius:3px;font-size:11px;font-family:var(--mono)}
.b-ph{background:var(--phl);color:var(--ph)}.b-g{background:var(--acl);color:var(--ac)}
.b-r{background:var(--redl);color:var(--red)}.b-a{background:var(--ambl);color:var(--amb)}
.b-b{background:var(--blul);color:var(--blu)}.b-m{background:#f0ede8;color:#555}
.fg{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:18px;border-bottom:1px solid var(--b)}
.fgg{display:flex;flex-direction:column;gap:4px}
.fl{font-size:11px;color:var(--mu);font-family:var(--mono);text-transform:uppercase}
input,select{border:1px solid var(--b);border-radius:4px;padding:7px 10px;font-size:13px;font-family:var(--sans);background:var(--s);color:var(--t);width:100%}
input:focus,select:focus{outline:none;border-color:var(--ph)}
.qbtns{display:flex;flex-wrap:wrap;gap:6px;padding:14px 18px}
.mkt-card{border-left:3px solid var(--ph);background:var(--s);border-radius:0 6px 6px 0;border:1px solid var(--b);border-left:3px solid var(--ph);padding:16px 18px;margin-bottom:10px}
.mkt-n{font-family:var(--mono);font-size:11px;color:var(--mu);margin-bottom:5px}
.mkt-t{font-weight:500;font-size:14px;margin-bottom:8px}
.mkt-b{font-size:13px;color:#444;line-height:1.8}
#toast{position:fixed;bottom:22px;right:22px;background:var(--t);color:#f0ede6;padding:9px 16px;border-radius:4px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none;font-family:var(--mono);z-index:999}
#toast.show{opacity:1}
.tag-src{font-size:10px;font-family:var(--mono);padding:1px 5px;border-radius:2px}
.src-ak{background:#e8f4ec;color:#2a6b3a}.src-m{background:#f0ede8;color:#666}.src-e{background:#fdf0ef;color:#c0392b}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:#ccc;border-radius:2px}
</style></head>
<body>
<div class="shell">
<aside class="sidebar">
  <div class="logo">
    <div class="logo-mark">鹏华基金</div>
    <div class="logo-name">场内ETF<br>指数跟踪平台</div>
  </div>
  <nav class="nav">
    <div class="nav-label">看板</div>
    <div class="nav-item active" onclick="nav(this,'overview')"><span class="nav-dot"></span>总览</div>
    <div class="nav-item" onclick="nav(this,'table')"><span class="nav-dot"></span>指数详表</div>
    <div class="nav-item" onclick="nav(this,'etf')"><span class="nav-dot"></span>ETF行情</div>
    <div class="nav-label">数据</div>
    <div class="nav-item" onclick="nav(this,'manual')"><span class="nav-dot"></span>手动录入</div>
    <div class="nav-label">输出</div>
    <div class="nav-item" onclick="nav(this,'marketing')"><span class="nav-dot"></span>营销话术</div>
  </nav>
  <div style="flex:1"></div>
  <div class="sidebar-foot">
    <div class="spill"><span class="sdot" id="sdot"></span><span id="stxt">未加载</span></div>
    <div style="font-size:11px;color:#444;margin-top:5px;font-family:var(--mono)" id="upd"></div>
    <div style="margin-top:12px"><a href="/logout" style="font-size:11px;color:#555;font-family:var(--mono)">退出登录</a></div>
  </div>
</aside>

<main class="main">

<!-- 总览 -->
<div class="panel active" id="p-overview">
  <div class="ph">
    <div><div class="pt">总览看板</div><div class="ps">现金流 vs 红利 · 实时对比</div></div>
    <div class="btns">
      <button class="btn sm" onclick="exportCsv()">导出 CSV</button>
      <button class="btn sm pri" onclick="refresh()">刷新数据</button>
    </div>
  </div>
  <div class="metrics">
    <div class="mc"><div class="ml">800现金流 近1年</div><div class="mv up" id="m1">—</div><div class="mn">全指数第一</div></div>
    <div class="mc"><div class="ml">中证现金流 近1年</div><div class="mv up" id="m2">—</div><div class="mn">并列第一</div></div>
    <div class="mc"><div class="ml">超越红利倍数</div><div class="mv up" id="m3">—</div><div class="mn">vs 最优红利指数</div></div>
    <div class="mc"><div class="ml">红利低波 PB分位</div><div class="mv warn">99%+</div><div class="mn">估值极度拥挤</div></div>
  </div>
  <div class="card">
    <div class="ch"><span class="ct">指数速览</span><span class="cm" id="ov-time"></span></div>
    <table><thead><tr><th>指数</th><th>近1月</th><th>近6月</th><th>近1年</th><th>最大回撤</th><th>策略</th><th>数据源</th></tr></thead>
    <tbody id="ov-body"><tr><td colspan="7" style="text-align:center;padding:48px;color:var(--mu)">点击「刷新数据」拉取实时数据，或在「手动录入」填入数据</td></tr></tbody></table>
  </div>
</div>

<!-- 详表 -->
<div class="panel" id="p-table">
  <div class="ph"><div><div class="pt">指数详表</div><div class="ps">全周期数据</div></div></div>
  <div class="card">
    <div class="ch"><span class="ct">完整数据</span></div>
    <table><thead><tr><th>代码</th><th>名称</th><th>近1月</th><th>近3月</th><th>近6月</th><th>近1年</th><th>最大回撤</th><th>点位</th><th>操作</th></tr></thead>
    <tbody id="tb-body"><tr><td colspan="9" style="text-align:center;padding:48px;color:var(--mu)">暂无数据</td></tr></tbody></table>
  </div>
</div>

<!-- ETF行情 -->
<div class="panel" id="p-etf">
  <div class="ph">
    <div><div class="pt">ETF行情</div><div class="ps">全市场场内ETF</div></div>
    <input type="text" id="etf-q" placeholder="搜索名称/代码..." style="width:180px" oninput="filterEtf()">
  </div>
  <div class="card">
    <div class="ch"><span class="ct">场内ETF</span><span class="cm" id="etf-cnt"></span></div>
    <div style="overflow-x:auto">
    <table><thead><tr><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th><th>成交额(万)</th><th>折溢价率</th><th>规模(亿)</th></tr></thead>
    <tbody id="etf-body"><tr><td colspan="7" style="text-align:center;padding:48px;color:var(--mu)">点击「刷新数据」加载</td></tr></tbody></table>
    </div>
  </div>
</div>

<!-- 手动录入 -->
<div class="panel" id="p-manual">
  <div class="ph"><div><div class="pt">手动录入</div><div class="ps">网络受限时使用，与实时数据合并显示</div></div></div>
  <div class="card">
    <div class="ch"><span class="ct">录入指数数据</span></div>
    <div class="fg">
      <div class="fgg"><label class="fl">指数代码</label><input id="fc" placeholder="932368"></div>
      <div class="fgg"><label class="fl">指数名称 *</label><input id="fn" placeholder="800现金流"></div>
      <div class="fgg"><label class="fl">当前点位</label><input id="fp" type="number" placeholder="1234.56"></div>
      <div class="fgg"><label class="fl">近1月(%)</label><input id="f1m" type="number" placeholder="5.20"></div>
      <div class="fgg"><label class="fl">近3月(%)</label><input id="f3m" type="number" placeholder="12.10"></div>
      <div class="fgg"><label class="fl">近6月(%)</label><input id="f6m" type="number" placeholder="20.73"></div>
      <div class="fgg"><label class="fl">近1年(%)</label><input id="f1y" type="number" placeholder="31.35"></div>
      <div class="fgg"><label class="fl">最大回撤(%)</label><input id="fdd" type="number" placeholder="-9.02"></div>
    </div>
    <div style="padding:0 18px 18px;display:flex;gap:8px">
      <button class="btn pri" onclick="submitManual()">保存</button>
      <button class="btn" onclick="clearForm()">清空</button>
    </div>
  </div>
  <div class="card">
    <div class="ch"><span class="ct">快速填入历史数据（来自您的分析文件）</span></div>
    <div class="qbtns">
      <button class="btn sm" onclick="qf('800现金流','932368',5.2,12.1,20.73,31.35,-9.02)">800现金流</button>
      <button class="btn sm" onclick="qf('中证现金流','932365',5.0,11.8,20.33,31.05,-9.34)">中证现金流</button>
      <button class="btn sm" onclick="qf('国证自由现金流','980092',8.1,15.2,28.05,19.66,-12.54)">国证自由现金流</button>
      <button class="btn sm" onclick="qf('富时现金流','FT',1.2,3.8,5.33,15.88,-9.14)">富时现金流</button>
      <button class="btn sm" onclick="qf('中证红利','000922',1.2,3.5,7.55,6.95,-6.69)">中证红利</button>
      <button class="btn sm" onclick="qf('上证红利','000015',1.5,4.2,10.91,7.77,-7.15)">上证红利</button>
      <button class="btn sm" onclick="qf('红利低波','H30269',0.8,2.1,3.85,5.38,-8.51)">红利低波</button>
      <button class="btn sm pri" onclick="fillAll()">一键全部填入 →</button>
    </div>
  </div>
</div>

<!-- 营销话术 -->
<div class="panel" id="p-marketing">
  <div class="ph">
    <div><div class="pt">营销话术</div><div class="ps">基于当前数据自动生成</div></div>
    <button class="btn sm" onclick="copyMkt()">复制全文</button>
  </div>
  <div id="mkt-content"></div>
</div>

</main>
</div>
<div id="toast"></div>

<script>
let D = {index_perf:[], etf_spot:[]};
let etfAll = [];

function nav(el, id) {
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('p-'+id).classList.add('active');
  if(id==='marketing') renderMkt();
}

function toast(msg,dur=2500){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),dur)}

function fp(v){if(v==null)return'<span style="color:var(--mu)">—</span>';const s=v>=0?'+':'';return`<span class="${v>=0?'up':'dn'}">${s}${(+v).toFixed(2)}%</span>`}
function fn2(v){return v==null||v===''?'—':(+v).toFixed(2)}

function strat(n){const cf=['800现金流','中证现金流','国证自由现金流','富时现金流'];return cf.includes(n)?'<span class="badge b-ph">质量因子</span>':'<span class="badge b-a">红利因子</span>'}
function srcBadge(s){if(s==='akshare')return'<span class="tag-src src-ak">实时</span>';if(s==='manual')return'<span class="tag-src src-m">手动</span>';return'<span class="tag-src src-e">错误</span>'}

function setStatus(s,txt){
  const d=document.getElementById('sdot'),t=document.getElementById('stxt');
  d.className='sdot '+s; t.textContent=txt;
}

async function refresh(){
  setStatus('loading','加载中...');
  toast('正在拉取数据...');
  await fetch('/api/refresh',{method:'POST'});
  const iv=setInterval(async()=>{
    const r=await fetch('/api/status').then(r=>r.json());
    if(r.status==='ok'){clearInterval(iv);setStatus('ok','数据正常');await load();toast('更新成功 ✓')}
    else if(r.status==='error'){clearInterval(iv);setStatus('error','拉取失败');toast('失败: '+r.error,4000)}
  },1500);
}

async function load(){
  const r=await fetch('/api/data').then(r=>r.json());
  D=r; etfAll=r.etf_spot||[];
  renderOv(D.index_perf);
  renderTb(D.index_perf);
  renderEtf(etfAll);
  if(r.last_update){document.getElementById('ov-time').textContent=r.last_update;document.getElementById('upd').textContent=r.last_update;setStatus('ok','数据正常')}
}

function renderOv(data){
  if(!data||!data.length)return;
  const cf=data.filter(d=>['800现金流','中证现金流'].includes(d.name));
  const dv=data.filter(d=>['中证红利','上证红利','红利低波'].includes(d.name));
  const b1y=Math.max(...cf.map(d=>d.ret_1y||0));
  const bd=Math.max(...dv.map(d=>d.ret_1y||0));
  document.getElementById('m1').innerHTML=fp(cf.find(d=>d.name==='800现金流')?.ret_1y);
  document.getElementById('m2').innerHTML=fp(cf.find(d=>d.name==='中证现金流')?.ret_1y);
  document.getElementById('m3').textContent=bd?(b1y/bd).toFixed(1)+'×':'—';
  document.getElementById('ov-body').innerHTML=data.map(d=>`
    <tr class="${d.is_penghua?'pr-row':''}">
      <td><b>${d.name}</b>${d.is_penghua?' <span class="badge b-ph">鹏华</span>':''}</td>
      <td>${fp(d.ret_1m)}</td><td>${fp(d.ret_6m)}</td>
      <td style="font-weight:500">${fp(d.ret_1y)}</td>
      <td><span class="dn">${d.max_dd!=null?d.max_dd.toFixed(2)+'%':'—'}</span></td>
      <td>${strat(d.name)}</td><td>${srcBadge(d.source)}</td>
    </tr>`).join('');
}

function renderTb(data){
  if(!data||!data.length)return;
  document.getElementById('tb-body').innerHTML=data.map(d=>`
    <tr class="${d.is_penghua?'pr-row':''}">
      <td style="font-family:var(--mono);font-size:12px;color:var(--mu)">${d.code||'—'}</td>
      <td><b>${d.name}</b></td>
      <td>${fp(d.ret_1m)}</td><td>${fp(d.ret_3m)}</td><td>${fp(d.ret_6m)}</td>
      <td style="font-weight:500">${fp(d.ret_1y)}</td>
      <td><span class="dn">${d.max_dd!=null?d.max_dd.toFixed(2)+'%':'—'}</span></td>
      <td style="font-family:var(--mono);font-size:12px">${d.price??'—'}</td>
      <td><button class="btn sm danger" onclick="del('${d.name}')">删除</button></td>
    </tr>`).join('');
}

function renderEtf(data){
  document.getElementById('etf-cnt').textContent=data.length+'只';
  if(!data.length){document.getElementById('etf-body').innerHTML='<tr><td colspan="7" style="text-align:center;padding:48px;color:var(--mu)">暂无数据</td></tr>';return}
  document.getElementById('etf-body').innerHTML=data.slice(0,150).map(d=>{
    const p=parseFloat(d['折溢价率']||0),c=parseFloat(d['涨跌幅']||0);
    return`<tr>
      <td style="font-family:var(--mono);font-size:12px">${d['代码']||'—'}</td>
      <td>${d['名称']||'—'}</td>
      <td style="font-family:var(--mono)">${fn2(d['最新价'])}</td>
      <td>${fp(c)}</td>
      <td style="font-family:var(--mono);font-size:12px">${d['成交额']||'—'}</td>
      <td>${p>=0?`<span class="up">+${p.toFixed(2)}%</span>`:`<span class="dn">${p.toFixed(2)}%</span>`}</td>
      <td>${d['规模']||'—'}</td>
    </tr>`}).join('');
}

function filterEtf(){
  const q=document.getElementById('etf-q').value.toLowerCase();
  renderEtf(etfAll.filter(d=>(d['名称']||'').toLowerCase().includes(q)||(d['代码']||'').toLowerCase().includes(q)));
}

function qf(name,code,m1,m3,m6,m1y,dd){
  document.getElementById('fc').value=code;
  document.getElementById('fn').value=name;
  document.getElementById('f1m').value=m1;
  document.getElementById('f3m').value=m3;
  document.getElementById('f6m').value=m6;
  document.getElementById('f1y').value=m1y;
  document.getElementById('fdd').value=dd;
  document.getElementById('fp').value='';
}

function clearForm(){['fc','fn','fp','f1m','f3m','f6m','f1y','fdd'].forEach(id=>document.getElementById(id).value='')}

async function fillAll(){
  const all=[
    ['800现金流','932368',5.2,12.1,20.73,31.35,-9.02],
    ['中证现金流','932365',5.0,11.8,20.33,31.05,-9.34],
    ['国证自由现金流','980092',8.1,15.2,28.05,19.66,-12.54],
    ['富时现金流','FT',1.2,3.8,5.33,15.88,-9.14],
    ['中证红利','000922',1.2,3.5,7.55,6.95,-6.69],
    ['上证红利','000015',1.5,4.2,10.91,7.77,-7.15],
    ['红利低波','H30269',0.8,2.1,3.85,5.38,-8.51],
  ];
  for(const[name,code,m1,m3,m6,m1y,dd] of all){
    await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,code,ret_1m:m1,ret_3m:m3,ret_6m:m6,ret_1y:m1y,max_dd:dd,price:null})});
  }
  await load(); toast('已填入全部历史数据 ✓');
}

async function submitManual(){
  const name=document.getElementById('fn').value;
  if(!name){toast('请填写指数名称');return}
  const row={code:document.getElementById('fc').value,name,
    price:parseFloat(document.getElementById('fp').value)||null,
    ret_1m:parseFloat(document.getElementById('f1m').value)||null,
    ret_3m:parseFloat(document.getElementById('f3m').value)||null,
    ret_6m:parseFloat(document.getElementById('f6m').value)||null,
    ret_1y:parseFloat(document.getElementById('f1y').value)||null,
    max_dd:parseFloat(document.getElementById('fdd').value)||null};
  await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(row)});
  await load(); toast('已保存 ✓'); clearForm();
}

async function del(name){
  if(!confirm(`确认删除「${name}」？`))return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  await load(); toast('已删除');
}

function renderMkt(){
  const data=D.index_perf||[];
  const cf=data.filter(d=>['800现金流','中证现金流'].includes(d.name));
  const dv=data.filter(d=>['中证红利','上证红利','红利低波'].includes(d.name));
  const b1y=cf.length?Math.max(...cf.map(d=>d.ret_1y||0)):31.35;
  const bn=cf.find(d=>d.ret_1y===b1y)?.name||'800现金流';
  const bd=dv.length?Math.max(...dv.map(d=>d.ret_1y||0)):7.77;
  const ratio=(b1y/bd).toFixed(1);
  const pts=[
    {n:'01',t:'不只选「分红高」，更选「盈利能力」',
     b:`以自由现金流率（FCF/EV）为核心筛选标准，前瞻性衡量企业真实盈利质量，规避勉强维持分红的「价值陷阱」。<br><br><strong>${bn}近一年回报 +${b1y.toFixed(2)}%</strong>，是最优红利指数（+${bd.toFixed(2)}%）的约 <strong>${ratio}倍</strong>，展现现金流策略在经济复苏周期的显著锐度。`},
    {n:'02',t:'估值更健康，安全垫更厚',
     b:`红利系列指数 PB 分位点已超 <strong>99%</strong>，「低估值」核心逻辑严重动摇，安全垫极薄。<br><br>800现金流 PB 分位仅 <strong>53.62%</strong>，中证现金流仅 <strong>33.22%</strong>，处于历史中低位，配置性价比显著更高。`},
    {n:'03',t:'聚焦实体经济，把握复苏主线',
     b:`明确剔除金融地产，前三大行业为<strong>原材料（27%）、可选消费（23%）、工业（20%）</strong>，均衡布局实体经济复苏核心链条。<br><br>红利低波金融权重高达 50.82%，行业过度集中，单一板块系统性风险显著。`},
    {n:'04',t:'季度调仓，比红利指数更敏捷',
     b:`依据最新季报及时优化成分股，组合始终保持当季盈利质量最高的标的。红利系列指数均为<strong>年度调仓</strong>，应对市场变化明显滞后。`},
    {n:'05',t:'800现金流 vs 中证现金流：分层满足不同需求',
     b:`<strong>800现金流</strong>：聚焦大中盘蓝筹，流动性最佳，适合大资金作核心底仓。<br><br><strong>中证现金流</strong>：覆盖全市场，PB分位仅33%，含中小盘高现金流黑马，弹性更高，适合进取型投资者。`},
  ];
  document.getElementById('mkt-content').innerHTML=pts.map(p=>`
    <div class="mkt-card"><div class="mkt-n">${p.n}</div><div class="mkt-t">${p.t}</div><div class="mkt-b">${p.b}</div></div>`).join('');
}

function copyMkt(){
  const txt=document.getElementById('mkt-content').innerText;
  navigator.clipboard.writeText(txt).then(()=>toast('已复制 ✓'));
}

function exportCsv(){
  const data=D.index_perf;
  if(!data.length){toast('暂无数据');return}
  let csv='\ufeff指数名称,代码,近1月(%),近3月(%),近6月(%),近1年(%),最大回撤(%),点位,数据源\n';
  data.forEach(d=>csv+=`${d.name},${d.code||''},${d.ret_1m||''},${d.ret_3m||''},${d.ret_6m||''},${d.ret_1y||''},${d.max_dd||''},${d.price||''},${d.source||''}\n`);
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download=`ETF跟踪_${new Date().toLocaleDateString('zh')}.csv`;
  a.click(); toast('已导出 ✓');
}

window.onload=async()=>{
  const s=await fetch('/api/status').then(r=>r.json());
  if(s.status==='ok'){await load()}else{setStatus('idle','待加载')}
};
</script>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  鹏华ETF跟踪平台")
    print(f"  本地访问: http://localhost:{port}")
    print(f"  默认密码: {ACCESS_PASSWORD}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
