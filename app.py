"""
鹏华ETF AI营销平台 v4
- 69只鹏华ETF完整产品库（含卖点/基转基对标）
- AKShare实时：规模/溢价/涨跌/成交/跟踪误差
- AKShare实时：指数历史行情对比
- Claude AI：根据最新数据实时生成营销话术
- 部署：Railway
"""

from flask import Flask, jsonify, render_template_string, request, session, redirect
import threading, time, os, json, functools
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "phfund2024etf")
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "penghua2024")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ══════════════════════════════════════════
# 产品数据库（从etf_db.json加载）
# ══════════════════════════════════════════
DB = {"products": {}, "peer_groups": {}, "index_to_codes": {}}

def load_db():
    global DB
    # 多个路径候选，依次尝试
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_db.json"),
        os.path.join(os.getcwd(), "etf_db.json"),
        "etf_db.json",
        "/app/etf_db.json",
    ]
    db_path = None
    for p in candidates:
        if os.path.exists(p):
            db_path = p
            break
    if db_path:
        with open(db_path, encoding="utf-8") as f:
            DB = json.load(f)
        print(f"[DB] 加载完成: {len(DB['products'])}只产品, {len(DB['peer_groups'])}组对标")
    else:
        print("[DB] 警告: etf_db.json未找到，使用空数据库")

# ══════════════════════════════════════════
# 全局实时缓存
# ══════════════════════════════════════════
CACHE = {
    "status": "idle",
    "error": "",
    "last_update": None,
    "spot": {},          # code -> {price, pct_chg, premium, turnover, aum}
    "index_perf": {},    # index_code -> {ret_1m, ret_3m, ret_6m, ret_1y, max_dd, bounce}
    "peer_aum": {},      # index_name -> [{code, name, manager, aum, fee, premium}]
    "holdings": {},      # code -> {"top10": {...}, "industry": {...}}
    "holdings_date": None,
}
LOCK = threading.Lock()


# ══════════════════════════════════════════
# 登录
# ══════════════════════════════════════════
def login_required(f):
    @functools.wraps(f)
    def wrap(*args, **kwargs):
        if not session.get("ok"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect("/login")
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


# ══════════════════════════════════════════
# 数据拉取
# ══════════════════════════════════════════
def _safe_float(v, scale=1):
    try:
        return round(float(str(v).replace("%","").replace(",","").strip()) * scale, 4)
    except:
        return None

def fetch_spot():
    """拉全市场ETF实时行情"""
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        result = {}
        for _, row in df.iterrows():
            code = str(row.get("代码","")).strip()
            result[code] = {
                "price":    _safe_float(row.get("最新价")),
                "pct_chg":  _safe_float(row.get("涨跌幅")),
                "premium":  _safe_float(row.get("折溢价率")),
                "turnover": str(row.get("成交额","")),
                "aum":      _safe_float(row.get("规模",row.get("规模(亿)"))),
            }
        return result
    except Exception as e:
        print(f"[spot] 失败: {e}")
        return {}

def fetch_index_perf(index_code):
    """拉单个指数近1年日线，计算各周期收益"""
    try:
        import akshare as ak, pandas as pd
        end = date.today()
        df = ak.index_zh_a_hist(
            symbol=index_code, period="daily",
            start_date=end.replace(year=end.year-1).strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df.empty:
            return None
        df = df.sort_values("日期")
        c = df["收盘"].values

        def r(n):
            return round((c[-1]/c[-n]-1)*100, 2) if len(c) >= n else None

        roll = pd.Series(c).cummax()
        dd = round(((pd.Series(c)-roll)/roll).min()*100, 2)
        # 低点弹性（最低价到最新价涨幅）
        min_idx = pd.Series(c).idxmin()
        bounce = round((c[-1]/c[min_idx]-1)*100, 2) if min_idx < len(c)-1 else 0

        return {
            "ret_1m": r(22), "ret_3m": r(63),
            "ret_6m": r(126), "ret_1y": r(252),
            "max_dd": dd, "bounce": bounce,
            "price": round(float(c[-1]), 2),
        }
    except Exception as e:
        return None

def fetch_peer_aum(index_name):
    """按跟踪指数名从全量ETF列表找同业规模"""
    try:
        import akshare as ak
        df = ak.fund_etf_fund_info_em()
        col_map = {c: c for c in df.columns}
        peers = []
        for _, row in df.iterrows():
            vals = " ".join(str(v) for v in row.values)
            if index_name[:4] in vals:
                peers.append({
                    "code":    str(row.get("基金代码", row.get("代码",""))).replace(".OF",""),
                    "name":    str(row.get("基金名称", row.get("名称",""))),
                    "manager": str(row.get("基金公司", row.get("管理人",""))),
                    "aum":     _safe_float(row.get("规模(亿)", row.get("规模",""))),
                    "fee":     str(row.get("管理费率","")),
                })
        peers.sort(key=lambda x: x["aum"] or 0, reverse=True)
        return peers[:10]
    except:
        return []

def _latest_quarter():
    """返回最近可用的季报年份和季度"""
    m = date.today().month
    y = date.today().year
    month_to_q = {1:"4",2:"4",3:"4",4:"4",5:"1",6:"1",7:"1",
                  8:"2",9:"2",10:"3",11:"3",12:"3"}
    q = month_to_q[m]
    yr = str(y-1) if q == "4" else str(y)
    return yr, q

def _fetch_top10(code: str) -> dict:
    try:
        import akshare as ak
        yr, q = _latest_quarter()
        df = ak.fund_portfolio_hold_em(code=code, year=yr, quarter=q)
        if df is None or df.empty:
            q2 = str(int(q)-1) if q!="1" else "4"
            yr2 = yr if q!="1" else str(int(yr)-1)
            df = ak.fund_portfolio_hold_em(code=code, year=yr2, quarter=q2)
        if df is None or df.empty:
            return {}
        stocks = []
        for _, row in df.head(10).iterrows():
            name = str(row.get("股票名称",""))
            weight = str(row.get("占净值比例","0")).replace("%","")
            try: w = float(weight)
            except: w = 0
            stocks.append({"name": name, "weight": w})
        return {"stocks": stocks, "date": f"{yr}-Q{q}"}
    except Exception as e:
        return {"error": str(e)}

def _fetch_industry(code: str) -> dict:
    try:
        import akshare as ak
        yr, q = _latest_quarter()
        df = ak.fund_industry_allocation_em(code=code, year=yr, quarter=q)
        if df is None or df.empty:
            return {}
        inds = []
        for _, row in df.iterrows():
            name = str(row.get("行业名称",""))
            weight = str(row.get("占净值比例","0")).replace("%","")
            try: w = float(weight)
            except: w = 0
            inds.append({"name": name, "weight": w})
        inds.sort(key=lambda x: x["weight"], reverse=True)
        return {"industries": inds[:8], "date": f"{yr}-Q{q}"}
    except Exception as e:
        return {"error": str(e)}

def _fetch_holdings_batch(codes: list) -> dict:
    result = {}
    def fetch_one(code):
        time.sleep(0.3)
        top10 = _fetch_top10(code)
        time.sleep(0.2)
        industry = _fetch_industry(code)
        return code, {"top10": top10, "industry": industry}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futures):
            try:
                code, data = fut.result()
                result[code] = data
            except:
                pass
    return result

def _dynamic_peers(index_name: str, own_code: str) -> list:
    """
    动态竞品：同指数规模前2名（排除自身），用于基转基对标
    """
    with LOCK:
        peers = CACHE["peer_aum"].get(index_name, [])
    result = [p for p in peers if p.get("code") != own_code and p.get("aum")]
    return result[:2]

def fetch_all_data():
    with LOCK:
        CACHE["status"] = "loading"
        CACHE["error"] = ""

    try:
        import akshare as ak

        print("[数据] 开始拉取...")

        # 1. 全市场ETF行情（并发友好，一次性拉完）
        print("[1/3] ETF实时行情...")
        spot = fetch_spot()
        print(f"      获取 {len(spot)} 只ETF行情")

        # 2. 鹏华产品对应指数行情（并发）
        print("[2/3] 指数历史行情...")
        ph_products = DB.get("products", {})
        index_codes = set()
        for p in ph_products.values():
            # 从 peer_groups 里找指数代码
            code = p.get("code","")
            pg = DB.get("peer_groups", {}).get(code, {})
            self_data = pg.get("self", {}) or {}
            idx_raw = self_data.get("index","")
            # 提取指数代码（数字部分）
            import re
            m = re.search(r'(\d{6})', idx_raw)
            if m:
                index_codes.add(m.group(1))

        index_perf = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fetch_index_perf, ic): ic for ic in list(index_codes)[:30]}
            for fut in as_completed(futures):
                ic = futures[fut]
                result = fut.result()
                if result:
                    index_perf[ic] = result
        print(f"      获取 {len(index_perf)} 个指数行情")

        # 3. 同业规模（用spot数据直接计算，不额外请求）
        print("[3/3] 同业排名计算...")
        index_to_codes = DB.get("index_to_codes", {})
        peer_aum = {}
        for idx_name, entries in index_to_codes.items():
            peers = []
            for e in entries:
                c = e["code"]
                sp = spot.get(c, {})
                peers.append({
                    "code":    c,
                    "name":    e["name"],
                    "manager": e["manager"],
                    "aum":     sp.get("aum"),
                    "premium": sp.get("premium"),
                    "pct_chg": sp.get("pct_chg"),
                })
            peers.sort(key=lambda x: x["aum"] or 0, reverse=True)
            peer_aum[idx_name] = peers

        # 4. 持仓数据（季报，并发拉取）
        print("[4/4] 拉取ETF持仓季报...")
        ph_codes = list(DB.get("products", {}).keys())
        holdings = _fetch_holdings_batch(ph_codes[:35])
        print(f"      获取 {len(holdings)} 只ETF持仓")

        with LOCK:
            CACHE["spot"] = spot
            CACHE["index_perf"] = index_perf
            CACHE["peer_aum"] = peer_aum
            CACHE["holdings"] = holdings
            CACHE["holdings_date"] = date.today().strftime("%Y-%m-%d")
            CACHE["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            CACHE["status"] = "ok"
        print("[数据] 完成!")

    except Exception as e:
        with LOCK:
            CACHE["status"] = "error"
            CACHE["error"] = str(e)
        print(f"[数据] 错误: {e}")


# ══════════════════════════════════════════
# AI生成话术
# ══════════════════════════════════════════
def build_ai_prompt(product_data: dict, peer_data: list, index_perf_data: dict,
                    holdings_data: dict = None, dynamic_peers: list = None) -> str:
    p = product_data
    code = p.get("code","")

    # ── 指数收益 ──
    perf_lines = []
    for k, label in [("ret_1m","近1月"),("ret_3m","近3月"),("ret_6m","近6月"),("ret_1y","近1年")]:
        v = index_perf_data.get(k)
        if v is not None:
            perf_lines.append(f"{label}：{'+' if v>=0 else ''}{v:.2f}%")
    bounce = index_perf_data.get("bounce")
    if bounce:
        perf_lines.append(f"低点以来最大弹性：+{bounce:.2f}%")

    # ── 同业规模排名 ──
    idx_name = p.get("index_name","")
    all_peers = peer_data  # 全量同指数ETF
    own_rank = next((i+1 for i,pe in enumerate(all_peers) if pe.get("code")==code), None)
    total_peers = len(all_peers)
    rank_text = f"同指数共{total_peers}只ETF，本产品规模排第{own_rank}名" if own_rank else "同业规模数据加载中"

    # ── 动态竞品（规模前2名，排除自身） ──
    dyn_peers = dynamic_peers or _dynamic_peers(idx_name, code)
    peer_lines = []
    for i, pe in enumerate(dyn_peers[:2], 1):
        aum = pe.get("aum")
        peer_lines.append(
            f"竞品{i}：{pe.get('name','')}（{pe.get('manager','')}）"
            f"，规模{aum:.2f}亿" if aum else f"竞品{i}：{pe.get('name','')}"
        )

    # ── 持仓数据（季报） ──
    top10_text = "暂无持仓数据"
    industry_text = "暂无行业数据"
    if holdings_data:
        top10 = holdings_data.get("top10", {})
        industry = holdings_data.get("industry", {})
        if top10 and "stocks" in top10:
            date_str = top10.get("date","")
            stocks_str = "、".join(
                f"{s['name']}({s['weight']:.2f}%)" for s in top10["stocks"][:5]
            )
            top10_text = f"前五大重仓股（{date_str}）：{stocks_str}"
        if industry and "industries" in industry:
            date_str = industry.get("date","")
            ind_str = "、".join(
                f"{ind['name']}({ind['weight']:.1f}%)" for ind in industry["industries"][:4]
            )
            industry_text = f"行业配置（{date_str}）：{ind_str}"

    prompt = f"""你是鹏华基金的ETF营销专家，请根据以下最新真实数据，为"{p.get('name','')}"生成营销话术。

【产品基本信息】
产品名称：{p.get('name','')}（{p.get('code','')}）
所属板块：{p.get('board','')}
特殊标签：{(p.get('special_tag','') or '无').replace(chr(10),' ')}
主理人：{p.get('manager','—')}
场内独家：{'✅ 是' if p.get('is_exclusive') else '❌ 否'}
费率最低档：{'✅ 是' if p.get('fee_lowest') else '❌ 否'}
规模最大：{'✅ 是' if p.get('aum_largest') else '❌ 否'}
估值低位：{(p.get('valuation_low','') or '无特别优势').replace(chr(10),' ')}
跟踪误差最小：{(p.get('tracking_err_min','') or '无特别优势').replace(chr(10),' ')}

【最新季报持仓数据】
{top10_text}
{industry_text}

【跟踪指数实时表现】
{chr(10).join(perf_lines) if perf_lines else '暂无数据（建议先刷新数据）'}

【同业竞争格局】
{rank_text}
{chr(10).join(peer_lines) if peer_lines else '竞品数据加载中'}

【卖点图谱中的历史话术（供参考，请结合最新数据更新）】
版本1：{p.get('slogan_v1','—')}
版本2：{p.get('slogan_v2','—')}
热门股卖点：{(p.get('hot_stock','') or '—').replace(chr(10),' ')}
赛道卖点：{(p.get('key_sector','') or '—').replace(chr(10),' ')}

【生成要求】
请输出以下5项，用JSON格式返回：
1. slogan_v1（字符串）：15字以内一句话，突出最差异化优势，适合微信推送
2. slogan_v2（字符串）：20字以内，补充第二维度，与版本1互换使用
3. product_points（数组，3条）：产品维度卖点，每条≤50字，基于费率/规模/标签/估值/跟踪误差
4. index_points（数组，2条）：指数维度卖点，每条≤60字，基于业绩/持仓/赛道/弹性
5. peer_pitch（字符串）：基转基话术≤120字，针对竞品持有者，说明为什么转入本产品更优

严格要求：只引用上方提供的真实数据，禁止编造任何数字；语言简洁有力。
JSON字段名必须完全一致，不要加markdown代码块标记。"""

    return prompt

def generate_ai_slogan(product_code: str) -> dict:
    """调用Claude API生成话术"""
    if not ANTHROPIC_API_KEY:
        return {"error": "未配置ANTHROPIC_API_KEY，请在Railway环境变量中添加"}

    p = DB.get("products", {}).get(product_code, {})
    if not p:
        return {"error": "产品不存在"}

    # 获取实时数据
    with LOCK:
        spot_data = CACHE["spot"].get(product_code, {})
        idx_name = p.get("index_name","")
        peer_list = CACHE["peer_aum"].get(idx_name, [])
        holdings_data = CACHE["holdings"].get(product_code, {})
        import re
        pg = DB.get("peer_groups", {}).get(product_code, {})
        self_data = pg.get("self", {}) or {}
        idx_raw = self_data.get("index","")
        m = re.search(r'(\d{6})', idx_raw)
        idx_code = m.group(1) if m else ""
        idx_perf = CACHE["index_perf"].get(idx_code, {})

    # 动态竞品（规模前2名，排除自身）
    dyn_peers = _dynamic_peers(idx_name, product_code)

    prompt = build_ai_prompt(p, peer_list, idx_perf, holdings_data, dyn_peers)

    try:
        import urllib.request
        import json as json_mod

        payload = json_mod.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json_mod.loads(resp.read())

        text = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
        # 提取JSON
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            result = json_mod.loads(m.group())
            result["raw"] = text
            return result
        return {"raw": text, "error": "AI返回格式异常，请查看raw字段"}

    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════
# API路由
# ══════════════════════════════════════════
@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    if CACHE["status"] == "loading":
        return jsonify({"ok": False, "msg": "正在加载中"})
    threading.Thread(target=fetch_all_data, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/status")
@login_required
def api_status():
    with LOCK:
        return jsonify({
            "status": CACHE["status"],
            "last_update": CACHE["last_update"],
            "error": CACHE["error"],
        })

@app.route("/api/products")
@login_required
def api_products():
    """返回所有鹏华ETF列表（含实时行情）"""
    products = DB.get("products", {})
    result = []
    with LOCK:
        spot = CACHE["spot"]
        peer_aum = CACHE["peer_aum"]

    for code, p in products.items():
        sp = spot.get(code, {})
        idx_name = p.get("index_name","")
        peers = peer_aum.get(idx_name, [])
        # 同业排名
        rank = next((i+1 for i,pe in enumerate(peers) if pe.get("code")==code), None)
        result.append({
            **p,
            "price":    sp.get("price"),
            "pct_chg":  sp.get("pct_chg"),
            "premium":  sp.get("premium"),
            "turnover": sp.get("turnover"),
            "aum_rt":   sp.get("aum"),       # 实时规模
            "peer_rank": rank,
            "peer_total": len(peers),
        })
    result.sort(key=lambda x: x.get("aum_rt") or 0, reverse=True)
    return jsonify(result)

@app.route("/api/product/<code>")
@login_required
def api_product_detail(code):
    """单产品详情：实时数据+同业对比+指数表现"""
    p = DB.get("products", {}).get(code, {})
    if not p:
        return jsonify({"error": "产品不存在"}), 404

    with LOCK:
        spot = CACHE["spot"]
        peer_aum = CACHE["peer_aum"]
        index_perf = CACHE["index_perf"]
        holdings_data = CACHE["holdings"].get(code, {})

    # 实时行情
    sp = spot.get(code, {})

    # 同业（实时规模）
    idx_name = p.get("index_name","")
    peers_rt = peer_aum.get(idx_name, [])

    # 动态竞品（规模前2名）
    dyn_peers = _dynamic_peers(idx_name, code)

    # 指数表现
    pg = DB.get("peer_groups", {}).get(code, {})
    self_data = pg.get("self", {}) or {}
    import re
    idx_raw = self_data.get("index","")
    m = re.search(r'(\d{6})', idx_raw)
    idx_code = m.group(1) if m else ""
    idx_perf = index_perf.get(idx_code, {})

    # Excel基转基对标
    peers_excel = pg.get("peers", [])

    return jsonify({
        "product": p,
        "spot": sp,
        "index_code": idx_code,
        "index_raw": idx_raw,
        "index_perf": idx_perf,
        "peers_rt": peers_rt[:8],
        "peers_excel": peers_excel,
        "pg_self": self_data,
        "holdings": holdings_data,
        "dynamic_peers": dyn_peers,
    })

@app.route("/api/ai_slogan/<code>", methods=["POST"])
@login_required
def api_ai_slogan(code):
    """AI生成话术（耗时较长，约5-15秒）"""
    result = generate_ai_slogan(code)
    return jsonify(result)

@app.route("/api/index/<index_code>")
@login_required
def api_index_detail(index_code):
    """指数详情：历史表现 + 全市场对比"""
    with LOCK:
        perf = CACHE["index_perf"].get(index_code, {})

    # 如果没有缓存，实时拉取
    if not perf:
        perf = fetch_index_perf(index_code) or {}

    return jsonify({"index_code": index_code, "perf": perf})

@app.route("/api/etf_search")
@login_required
def api_etf_search():
    q = request.args.get("q","").lower()
    with LOCK:
        spot = CACHE["spot"]
    products = DB.get("products",{})
    result = []
    for code, p in products.items():
        if q in p.get("name","").lower() or q in code:
            sp = spot.get(code,{})
            result.append({**p, **sp})
    return jsonify(result[:20])

@app.route("/")
@login_required
def index():
    return render_template_string(MAIN_HTML)


# ══════════════════════════════════════════
# 前端HTML
# ══════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF · AI营销平台</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f5f4f0;font-family:'Noto Sans SC',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;border:1px solid #e2e0d8;border-radius:10px;padding:52px 44px;width:380px}
.brand{font-family:'DM Mono',monospace;font-size:11px;color:#aaa;letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}
.title{font-size:20px;font-weight:500;margin-bottom:8px}
.sub{font-size:12px;color:#999;margin-bottom:32px}
label{font-size:11px;color:#999;font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:6px}
input{width:100%;border:1px solid #e2e0d8;border-radius:5px;padding:11px 13px;font-size:14px;font-family:'Noto Sans SC',sans-serif;margin-bottom:22px;color:#1a1916;outline:none}
input:focus{border-color:#3d2b8a}
button{width:100%;background:#1a1916;color:#f0ede6;border:none;border-radius:5px;padding:12px;font-size:14px;cursor:pointer}
button:hover{background:#333}
.err{color:#c0392b;font-size:13px;margin-bottom:16px}
</style></head><body>
<div class="box">
  <div class="brand">鹏华基金管理有限公司</div>
  <div class="title">ETF AI营销平台</div>
  <div class="sub">实时数据 · AI生成话术 · 同业对比</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>访问密码</label>
    <input type="password" name="password" placeholder="请输入团队密码" autofocus>
    <button type="submit">进入平台</button>
  </form>
</div></body></html>"""


MAIN_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF · AI营销平台</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f4f0;--s:#fff;--b:#e2e0d8;--t:#1a1916;--mu:#7a7870;
  --ph:#3d2b8a;--phl:#eeebf8;--phb:#534AB7;
  --red:#c0392b;--redl:#fdf0ef;--grn:#2d5a3d;--grnl:#e8f0eb;
  --amb:#b7790f;--ambl:#fdf6e3;--blu:#1a4a7a;--blul:#eaf0f8;
  --mono:'DM Mono',monospace;--sans:'Noto Sans SC',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:var(--sans);color:var(--t);font-size:14px;line-height:1.6;height:100vh;overflow:hidden}
.shell{display:grid;grid-template-columns:240px 1fr;height:100vh}
.sb{background:var(--t);display:flex;flex-direction:column;overflow-y:auto}
.main{display:grid;grid-template-rows:1fr;overflow:hidden}
.content{overflow-y:auto;padding:24px 28px}

/* Sidebar */
.logo{padding:22px 22px 16px;border-bottom:1px solid #2a2826}
.logo-sub{font-family:var(--mono);font-size:10px;color:#555;letter-spacing:.12em;text-transform:uppercase;margin-bottom:4px}
.logo-title{font-size:14px;font-weight:500;color:#f0ede6;line-height:1.35}
.logo-badge{display:inline-block;margin-top:6px;font-size:10px;font-family:var(--mono);background:#2a3f2c;color:#8fbc9c;padding:2px 8px;border-radius:3px}
.nav{padding:10px 0;flex:1}
.nl{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:#444;padding:8px 22px 4px}
.ni{display:flex;align-items:center;gap:8px;padding:8px 22px;font-size:13px;color:#888;cursor:pointer;border-left:2px solid transparent;transition:all .12s}
.ni:hover{color:#f0ede6;background:#1e1d1a}
.ni.on{color:#f0ede6;border-left-color:#8fbc9c;background:#1a1916}
.nd{width:5px;height:5px;border-radius:50%;background:#333;flex-shrink:0}
.ni.on .nd{background:#8fbc9c}
.sb-foot{padding:14px 22px;border-top:1px solid #2a2826}
.sdot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#444;margin-right:5px;vertical-align:middle}
.sdot.ok{background:#5ab97a}.sdot.loading{background:#f0b429;animation:blink 1s infinite}.sdot.error{background:#e05a4e}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}

/* Search box in sidebar */
.sb-search{padding:10px 22px}
.sb-search input{width:100%;background:#2a2826;border:1px solid #3a3836;border-radius:4px;padding:7px 10px;font-size:12px;color:#ccc;font-family:var(--sans);outline:none}
.sb-search input::placeholder{color:#555}
.sb-search input:focus{border-color:#534AB7}
.product-list{padding:0 0 8px}
.product-item{padding:7px 22px;cursor:pointer;border-left:2px solid transparent;transition:all .1s}
.product-item:hover{background:#1e1d1a}
.product-item.on{background:#1a1916;border-left-color:#8fbc9c}
.product-item-name{font-size:12px;color:#ccc;line-height:1.3}
.product-item-meta{font-size:10px;color:#555;font-family:var(--mono);margin-top:1px}
.product-item.on .product-item-name{color:#f0ede6}

/* Main panels */
.panel{display:none}.panel.on{display:block}
.ph{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;gap:12px}
.pt{font-size:19px;font-weight:500;letter-spacing:-.02em}
.ps{font-size:12px;color:var(--mu);margin-top:2px;font-family:var(--mono)}
.btns{display:flex;gap:6px;flex-shrink:0}
.btn{padding:7px 14px;border-radius:4px;border:1px solid var(--b);background:var(--s);cursor:pointer;font-size:12px;font-family:var(--sans);color:var(--t);transition:all .12s;white-space:nowrap}
.btn:hover{border-color:#888}.btn.pri{background:var(--t);color:#f0ede6;border-color:var(--t)}.btn.pri:hover{background:#333}
.btn.ph-btn{background:var(--ph);color:#fff;border-color:var(--ph)}.btn.ph-btn:hover{background:#2a1f6b}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* Metrics */
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:18px}
.mc{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:12px 14px}
.ml{font-size:10px;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.mv{font-size:20px;font-weight:300;letter-spacing:-.025em}
.mn{font-size:11px;color:var(--mu);margin-top:2px}
.up{color:var(--red)}.dn{color:var(--blu)}.warn{color:var(--amb)}.ok-c{color:var(--grn)}

/* Cards */
.card{background:var(--s);border:1px solid var(--b);border-radius:6px;overflow:hidden;margin-bottom:14px}
.ch{padding:11px 16px;border-bottom:1px solid var(--b);display:flex;align-items:center;justify-content:space-between;gap:8px}
.ct{font-size:13px;font-weight:500}
.cm{font-size:11px;color:var(--mu);font-family:var(--mono)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:8px 12px;font-size:10px;font-weight:400;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--b);background:#faf9f7;white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #f0ede8;vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:#faf9f7}
.ph-row td{background:var(--phl)}.ph-row:hover td{background:#e5e1f5}
.rank1 td{background:#e8f0eb}.rank1:hover td{background:#dce8de}

/* Badges */
.badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-family:var(--mono)}
.b-ph{background:var(--phl);color:var(--ph)}.b-g{background:var(--grnl);color:var(--grn)}
.b-r{background:var(--redl);color:var(--red)}.b-a{background:var(--ambl);color:var(--amb)}
.b-b{background:var(--blul);color:var(--blu)}.b-gray{background:#f0ede8;color:#666}

/* Tags */
.tag-check{color:#2d5a3d;font-size:13px}
.tag-cross{color:#999;font-size:13px}

/* AI卖点区域 */
.ai-box{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:16px;margin-bottom:14px}
.ai-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.ai-title{font-size:13px;font-weight:500;display:flex;align-items:center;gap:6px}
.ai-badge{font-size:10px;font-family:var(--mono);background:#eeedfe;color:#534AB7;padding:2px 7px;border-radius:3px}
.slogan-box{background:#f9f8f6;border-radius:4px;padding:10px 14px;margin-bottom:8px;border-left:3px solid var(--ph)}
.slogan-label{font-size:10px;color:var(--mu);font-family:var(--mono);text-transform:uppercase;margin-bottom:4px}
.slogan-text{font-size:14px;font-weight:500;line-height:1.5}
.point-item{padding:8px 0;border-bottom:1px solid #f0ede8;font-size:13px;color:#333;line-height:1.6}
.point-item:last-child{border-bottom:none}
.point-num{display:inline-block;width:18px;height:18px;border-radius:50%;background:var(--phl);color:var(--ph);font-size:10px;font-family:var(--mono);text-align:center;line-height:18px;margin-right:6px;flex-shrink:0}
.loading-ai{text-align:center;padding:24px;color:var(--mu);font-size:13px}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid #e2e0d8;border-top-color:var(--ph);border-radius:50%;animation:spin .8s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

/* 详情页布局 */
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.detail-kv{display:flex;flex-direction:column;gap:6px}
.kv-row{display:flex;align-items:baseline;gap:8px;font-size:13px}
.kv-label{color:var(--mu);font-size:11px;min-width:80px;flex-shrink:0}
.kv-val{font-weight:400}

/* 总览卡片列表 */
.etf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:14px}
.etf-card{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:12px 14px;cursor:pointer;transition:all .12s}
.etf-card:hover{border-color:#888;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.etf-card.ph{border-left:3px solid var(--ph)}
.etf-card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px}
.etf-card-name{font-size:13px;font-weight:500;line-height:1.3;flex:1}
.etf-card-chg{font-size:14px;font-weight:500;min-width:60px;text-align:right}
.etf-card-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.etf-card-tag{font-size:10px;color:var(--mu);font-family:var(--mono)}

/* 过滤器 */
.filter-bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.filter-bar select,.filter-bar input{padding:6px 10px;border:1px solid var(--b);border-radius:4px;font-size:12px;background:var(--s);color:var(--t);font-family:var(--sans)}
.filter-bar input{width:160px}

/* Toast */
#toast{position:fixed;bottom:18px;right:18px;background:var(--t);color:#f0ede6;padding:8px 14px;border-radius:4px;font-size:12px;font-family:var(--mono);opacity:0;transition:opacity .2s;pointer-events:none;z-index:999;max-width:300px}
#toast.show{opacity:1}

::-webkit-scrollbar{width:3px;height:3px}::-webkit-scrollbar-thumb{background:#ccc;border-radius:2px}
</style></head>
<body>
<div class="shell">

<!-- ═══ Sidebar ═══ -->
<aside class="sb">
  <div class="logo">
    <div class="logo-sub">鹏华基金管理有限公司</div>
    <div class="logo-title">ETF AI营销平台</div>
    <span class="logo-badge">69只产品 · AI实时生成</span>
  </div>

  <nav class="nav">
    <div class="nl">导航</div>
    <div class="ni on" onclick="showPage('overview',this)"><span class="nd"></span>总览看板</div>
    <div class="ni" onclick="showPage('detail',this)" id="nav-detail" style="display:none"><span class="nd"></span>产品详情</div>
    <div class="nl">产品列表</div>
  </nav>

  <div class="sb-search">
    <input type="text" placeholder="搜索产品..." id="sb-search" oninput="filterSidebar()">
  </div>

  <div class="product-list" id="product-list"></div>

  <div class="sb-foot">
    <div style="font-size:11px;color:#666;font-family:var(--mono)">
      <span class="sdot" id="sdot"></span><span id="stxt">未加载</span>
    </div>
    <div style="font-size:10px;color:#444;margin-top:3px;font-family:var(--mono)" id="upd"></div>
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center">
      <button class="btn" style="font-size:11px;padding:4px 10px" onclick="doRefresh()">刷新数据</button>
      <a href="/logout" style="font-size:11px;color:#555;font-family:var(--mono)">退出</a>
    </div>
  </div>
</aside>

<!-- ═══ Main Content ═══ -->
<div class="main">
<div class="content">

  <!-- ── 总览 ── -->
  <div class="panel on" id="p-overview">
    <div class="ph">
      <div><div class="pt">总览看板</div><div class="ps">全部鹏华场内ETF · 实时数据</div></div>
      <div class="btns">
        <select id="cat-filter" onchange="filterOverview()" style="font-size:12px;padding:6px 10px;border:1px solid var(--b);border-radius:4px">
          <option value="">全部板块</option>
        </select>
        <button class="btn" onclick="exportOverviewCsv()">导出CSV</button>
        <button class="btn pri" onclick="doRefresh()">刷新数据</button>
      </div>
    </div>
    <div class="metrics">
      <div class="mc"><div class="ml">产品总数</div><div class="mv" id="ov-cnt">—</div><div class="mn">鹏华场内ETF</div></div>
      <div class="mc"><div class="ml">今日平均涨跌</div><div class="mv" id="ov-avg">—</div><div class="mn">等权平均</div></div>
      <div class="mc"><div class="ml">规模第一产品</div><div class="mv" id="ov-top" style="font-size:13px;margin-top:2px">—</div><div class="mn" id="ov-top-aum"></div></div>
      <div class="mc"><div class="ml">溢价率>1%</div><div class="mv warn" id="ov-prem">—</div><div class="mn">只需关注</div></div>
    </div>
    <div class="filter-bar">
      <input type="text" placeholder="搜索..." id="ov-search" oninput="filterOverview()">
    </div>
    <div class="card">
      <div class="ch"><span class="ct">全部产品</span><span class="cm" id="ov-count-label"></span></div>
      <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>产品名称</th><th>代码</th><th>板块</th>
          <th>今日涨跌</th><th>折溢价率</th><th>实时规模(亿)</th>
          <th>同业排名</th><th>特殊标签</th><th>主理人</th>
        </tr></thead>
        <tbody id="ov-body"><tr><td colspan="9" style="text-align:center;padding:40px;color:var(--mu)">点击右上角「刷新数据」加载</td></tr></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- ── 产品详情 ── -->
  <div class="panel" id="p-detail">
    <div class="ph">
      <div>
        <div class="pt" id="d-title">产品详情</div>
        <div class="ps" id="d-subtitle"></div>
      </div>
      <div class="btns">
        <button class="btn" onclick="copyAll()">复制话术</button>
        <button class="btn ph-btn" id="btn-ai" onclick="doAI()">✦ AI生成卖点</button>
      </div>
    </div>

    <!-- 基础信息 + 实时行情 -->
    <div class="detail-grid">
      <div class="card" style="margin-bottom:0">
        <div class="ch"><span class="ct">产品基础信息</span><span class="cm badge b-ph">鹏华</span></div>
        <div style="padding:12px 16px">
          <div class="detail-kv" id="d-basic"></div>
        </div>
      </div>
      <div class="card" style="margin-bottom:0">
        <div class="ch"><span class="ct">实时行情</span><span class="cm" id="d-spot-time"></span></div>
        <div style="padding:12px 16px">
          <div class="detail-kv" id="d-spot"></div>
        </div>
      </div>
    </div>
    <div style="margin-bottom:14px"></div>

    <!-- 产品维度卖点（来自Excel） -->
    <div class="card">
      <div class="ch"><span class="ct">产品维度卖点标签</span><span class="cm">来自卖点图谱</span></div>
      <div style="padding:12px 16px" id="d-tags"></div>
    </div>

    <!-- AI生成卖点 -->
    <div class="ai-box" id="ai-result-box">
      <div class="ai-header">
        <div class="ai-title">✦ AI实时生成卖点 <span class="ai-badge">Claude驱动</span></div>
        <button class="btn" onclick="doAI()" style="font-size:11px;padding:4px 10px">重新生成</button>
      </div>
      <div id="ai-content">
        <div style="color:var(--mu);font-size:13px;padding:8px 0">点击右上角「AI生成卖点」按钮，根据最新实时数据生成营销话术</div>
      </div>
    </div>

    <!-- 指数表现 -->
    <div class="card">
      <div class="ch"><span class="ct">跟踪指数表现</span><span class="cm" id="d-idx-name"></span></div>
      <div style="padding:12px 16px" id="d-idx-perf">
        <div style="color:var(--mu);font-size:13px">加载中...</div>
      </div>
    </div>

    <!-- 同业对比（实时规模排名） -->
    <div class="card">
      <div class="ch"><span class="ct">同指数ETF规模排名</span><span class="cm">实时数据</span></div>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>#</th><th>产品名称</th><th>管理人</th><th>实时规模(亿)</th><th>折溢价率</th><th>今日涨跌</th></tr></thead>
        <tbody id="d-peers-rt"></tbody>
      </table>
      </div>
    </div>

    <!-- 基转基对标（来自Excel） -->
    <div class="card" id="d-peer-excel-card">
      <div class="ch"><span class="ct">基转基指数对标</span><span class="cm">来自卖点图谱</span></div>
      <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>对标指数</th><th>ETF简称</th><th>公司</th><th>规模(亿)</th>
          <th>热门股A</th><th>热门股B</th><th>赛道A</th>
          <th>近1月</th><th>近3月</th><th>近6月</th><th>近1年</th>
          <th>低点弹性(近6月)</th>
        </tr></thead>
        <tbody id="d-peers-excel"></tbody>
      </table>
      </div>
      <div id="d-peer-slogan" style="padding:12px 16px;font-size:13px;color:#333;line-height:1.8;border-top:1px solid var(--b);display:none"></div>
    </div>

  </div>

</div>
</div>
</div>

<div id="toast"></div>

<script>
// ══ 全局状态 ══
let allProducts = [];
let currentCode = null;
let currentDetail = null;
let statusInterval = null;

// ══ 工具函数 ══
function toast(msg, dur=2500){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),dur);
}
function setStatus(s,txt){
  document.getElementById('sdot').className='sdot '+s;
  document.getElementById('stxt').textContent=txt;
}
function fp(v,dec=2){
  if(v==null||v===''||isNaN(parseFloat(v)))return'<span style="color:var(--mu)">—</span>';
  const n=parseFloat(v);
  const s=n>=0?'+':''; const cls=n>=0?'up':'dn';
  return`<span class="${cls}">${s}${n.toFixed(dec)}%</span>`;
}
function fn(v,dec=2){
  if(v==null||v===''||isNaN(parseFloat(v)))return'—';
  return parseFloat(v).toFixed(dec);
}
function check(v){return v?'<span class="tag-check">✅</span>':'<span class="tag-cross">❌</span>'}
function esc(s){return String(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

// ══ 导航 ══
function showPage(id, el){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('on'));
  document.getElementById('p-'+id).classList.add('on');
  if(el) el.classList.add('on');
}

function showDetail(code){
  currentCode = code;
  document.getElementById('nav-detail').style.display='flex';
  showPage('detail', document.getElementById('nav-detail'));
  // 高亮侧边栏
  document.querySelectorAll('.product-item').forEach(el=>{
    el.classList.toggle('on', el.dataset.code===code);
  });
  loadDetail(code);
}

// ══ 侧边栏产品列表 ══
function renderSidebar(products){
  const el = document.getElementById('product-list');
  const q = document.getElementById('sb-search').value.toLowerCase();
  const filtered = products.filter(p=>
    !q || p.name.toLowerCase().includes(q) || p.code.includes(q) ||
    (p.category||'').toLowerCase().includes(q)
  );
  el.innerHTML = filtered.map(p=>`
    <div class="product-item${p.code===currentCode?' on':''}" 
         data-code="${p.code}" onclick="showDetail('${p.code}')">
      <div class="product-item-name">${esc(p.name)}</div>
      <div class="product-item-meta">${p.code} · ${p.category||p.board||''}</div>
    </div>`).join('');
}

function filterSidebar(){
  renderSidebar(allProducts);
}

// ══ 数据刷新 ══
async function doRefresh(){
  if(statusInterval) clearInterval(statusInterval);
  setStatus('loading','加载中...');
  toast('正在拉取全市场数据，约1-2分钟...',4000);
  await fetch('/api/refresh',{method:'POST'});
  statusInterval = setInterval(async()=>{
    const r=await fetch('/api/status').then(r=>r.json());
    document.getElementById('upd').textContent=r.last_update||'';
    if(r.status==='ok'){
      clearInterval(statusInterval);
      setStatus('ok','数据正常');
      await loadProducts();
      toast('数据已更新 ✓');
      if(currentCode) loadDetail(currentCode);
    } else if(r.status==='error'){
      clearInterval(statusInterval);
      setStatus('error','加载失败');
      toast('错误: '+r.error.slice(0,60),4000);
    }
  },2000);
}

// ══ 产品列表 ══
async function loadProducts(){
  allProducts = await fetch('/api/products').then(r=>r.json());
  renderSidebar(allProducts);
  renderOverview(allProducts);
}

// ══ 总览 ══
function renderOverview(data){
  // 板块过滤器
  const cats = [...new Set(data.map(p=>p.board||p.category||'').filter(Boolean))].sort();
  const sel = document.getElementById('cat-filter');
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部板块</option>' + cats.map(c=>`<option value="${c}"${c===cur?' selected':''}>${esc(c)}</option>`).join('');

  filterOverview();
}

function filterOverview(){
  const q = document.getElementById('ov-search').value.toLowerCase();
  const cat = document.getElementById('cat-filter').value;
  const filtered = allProducts.filter(p=>{
    const matchQ = !q || p.name.toLowerCase().includes(q) || p.code.includes(q);
    const matchCat = !cat || p.board===cat || p.category===cat;
    return matchQ && matchCat;
  });

  // 指标卡
  document.getElementById('ov-cnt').textContent = allProducts.length + '只';
  const chgs = filtered.filter(p=>p.pct_chg!=null).map(p=>p.pct_chg);
  const avg = chgs.length ? chgs.reduce((a,b)=>a+b,0)/chgs.length : null;
  document.getElementById('ov-avg').innerHTML = avg!=null ? fp(avg) : '—';
  const top = [...filtered].sort((a,b)=>(b.aum_rt||0)-(a.aum_rt||0))[0];
  if(top){ document.getElementById('ov-top').textContent=top.name; document.getElementById('ov-top-aum').textContent=top.aum_rt?top.aum_rt.toFixed(1)+'亿':''; }
  const prem = filtered.filter(p=>p.premium!=null&&Math.abs(p.premium)>1).length;
  document.getElementById('ov-prem').textContent = prem+'只';
  document.getElementById('ov-count-label').textContent = `共${filtered.length}只`;

  document.getElementById('ov-body').innerHTML = filtered.map(p=>`
    <tr onclick="showDetail('${p.code}')" style="cursor:pointer">
      <td><b>${esc(p.name)}</b></td>
      <td style="font-family:var(--mono);font-size:11px;color:var(--mu)">${p.code}</td>
      <td><span class="badge b-gray">${esc(p.board||p.category||'—')}</span></td>
      <td>${fp(p.pct_chg)}</td>
      <td>${p.premium!=null?fp(p.premium):'—'}</td>
      <td style="font-family:var(--mono)">${p.aum_rt?p.aum_rt.toFixed(2):'—'}</td>
      <td>${p.peer_rank?`<span class="badge ${p.peer_rank===1?'b-g':'b-gray'}">${p.peer_rank===1?'规模第1':'第'+p.peer_rank+'/'+p.peer_total}</span>`:'—'}</td>
      <td style="font-size:11px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.special_tag||'').replace(/\n/g,' ')}</td>
      <td style="font-size:11px;color:var(--mu)">${esc(p.manager||'—')}</td>
    </tr>`).join('');
}

// ══ 产品详情 ══
async function loadDetail(code){
  document.getElementById('d-title').textContent='加载中...';
  document.getElementById('ai-content').innerHTML='<div style="color:var(--mu);font-size:13px;padding:8px 0">点击「AI生成卖点」按钮，根据最新实时数据生成营销话术</div>';

  const data = await fetch(`/api/product/${code}`).then(r=>r.json());
  currentDetail = data;
  const p = data.product || {};
  const sp = data.spot || {};
  const idx = data.index_perf || {};
  const peers_rt = data.peers_rt || [];
  const peers_ex = data.peers_excel || [];

  document.getElementById('d-title').textContent = p.name || code;
  document.getElementById('d-subtitle').textContent = `${code} · ${p.category||p.board||''} · 主理人：${p.manager||'—'}`;

  // 基础信息
  document.getElementById('d-basic').innerHTML = [
    ['产品全称', p.etf_full_name||p.name||'—'],
    ['代码', p.code||'—'],
    ['跟踪指数', p.index_name||'—'],
    ['板块分类', p.board||p.category||'—'],
    ['特殊标签', (p.special_tag||'—').replace(/\n/g,' ')],
    ['主理人', p.manager||'—'],
    ['卖点更新', p.update_date||'—'],
  ].map(([k,v])=>`<div class="kv-row"><span class="kv-label">${k}</span><span class="kv-val">${esc(v)}</span></div>`).join('');

  // 实时行情
  document.getElementById('d-spot-time').textContent = data.last_update||'';
  document.getElementById('d-spot').innerHTML = [
    ['最新价', sp.price?sp.price.toFixed(3):'—'],
    ['今日涨跌', ''],
    ['折溢价率', ''],
    ['实时规模', sp.aum?sp.aum.toFixed(2)+'亿':'—'],
    ['成交额', sp.turnover||'—'],
  ].map(([k,v],i)=>`<div class="kv-row"><span class="kv-label">${k}</span><span class="kv-val">${
    i===1?fp(sp.pct_chg):i===2?fp(sp.premium):esc(v)
  }</span></div>`).join('');

  // 产品维度卖点标签
  const tags = [
    ['场内独家', p.is_exclusive, p.is_exclusive?'全市场唯一':''],
    ['费率最低档', p.fee_lowest, ''],
    ['规模最大', p.aum_largest, ''],
    ['估值低位', !!p.valuation_low, p.valuation_low||''],
    ['成交量最大', p.volume_largest, ''],
    ['跟踪误差最小', !!p.tracking_err_min, p.tracking_err_min||''],
  ];
  const hotStock = p.hot_stock ? `<div style="margin-top:8px;font-size:12px"><b>热门股：</b>${esc(p.hot_stock.replace(/\n/g,' '))}</div>` : '';
  const keySector = p.key_sector ? `<div style="font-size:12px;margin-top:4px"><b>赛道：</b>${esc(p.key_sector.replace(/\n/g,' '))}</div>` : '';
  document.getElementById('d-tags').innerHTML = 
    `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">` +
    tags.map(([label,val,detail])=>`
      <div style="display:flex;align-items:center;gap:4px;font-size:12px">
        ${val?'<span class="tag-check">✅</span>':'<span class="tag-cross" style="opacity:.4">❌</span>'}
        <span style="${val?'':'color:var(--mu)'}">${label}</span>
        ${detail?`<span style="color:var(--mu);font-size:11px">(${esc(detail.slice(0,30))})</span>`:''}
      </div>`).join('') +
    `</div>${hotStock}${keySector}` +
    (p.slogan_v1||p.slogan_v2 ? `
      <div style="margin-top:12px;border-top:1px solid var(--b);padding-top:10px">
        <div style="font-size:10px;color:var(--mu);font-family:var(--mono);margin-bottom:6px">EXISTING SLOGANS</div>
        ${p.slogan_v1?`<div class="slogan-box"><div class="slogan-label">版本1</div><div class="slogan-text">${esc(p.slogan_v1)}</div></div>`:''}
        ${p.slogan_v2?`<div class="slogan-box" style="border-left-color:#1a4a7a"><div class="slogan-label">版本2</div><div class="slogan-text">${esc(p.slogan_v2)}</div></div>`:''}
      </div>`:'' );

  // 指数表现
  document.getElementById('d-idx-name').textContent = data.index_raw||data.index_code||'';
  if(Object.keys(idx).length){
    document.getElementById('d-idx-perf').innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px">
        ${[['近1月',idx.ret_1m],['近3月',idx.ret_3m],['近6月',idx.ret_6m],['近1年',idx.ret_1y]].map(([l,v])=>`
          <div class="mc" style="padding:8px 10px">
            <div class="ml">${l}</div>
            <div class="mv" style="font-size:16px">${fp(v)}</div>
          </div>`).join('')}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <div class="mc" style="padding:8px 10px"><div class="ml">最大回撤</div><div class="mv" style="font-size:16px"><span class="dn">${idx.max_dd!=null?idx.max_dd.toFixed(2)+'%':'—'}</span></div></div>
        <div class="mc" style="padding:8px 10px"><div class="ml">低点以来弹性</div><div class="mv" style="font-size:16px"><span class="up">${idx.bounce!=null?'+'+idx.bounce.toFixed(2)+'%':'—'}</span></div></div>
      </div>`;
  } else {
    document.getElementById('d-idx-perf').innerHTML='<div style="color:var(--mu);font-size:13px">暂无指数行情数据（刷新后可用）</div>';
  }

  // 同业排名（实时）
  document.getElementById('d-peers-rt').innerHTML = peers_rt.length ?
    peers_rt.map((pe,i)=>`
      <tr class="${i===0?'rank1':''}${pe.manager&&pe.manager.includes('鹏华')?'ph-row':''}">
        <td style="font-family:var(--mono);color:var(--mu)">${i+1}</td>
        <td><b>${esc(pe.name)}</b>${pe.manager&&pe.manager.includes('鹏华')?' <span class="badge b-ph">鹏华</span>':''}</td>
        <td style="font-size:11px;color:var(--mu)">${esc(pe.manager||'—')}</td>
        <td style="font-family:var(--mono);font-weight:${i===0?'500':'400'}">${pe.aum?pe.aum.toFixed(2):'—'}</td>
        <td>${pe.premium!=null?fp(pe.premium):'—'}</td>
        <td>${pe.pct_chg!=null?fp(pe.pct_chg):'—'}</td>
      </tr>`).join('') :
    '<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--mu)">暂无同业数据（刷新后可用）</td></tr>';

  // 基转基对标（Excel）
  const pgSelf = data.pg_self || {};
  const selfRow = pgSelf.code ? `
    <tr class="ph-row">
      <td style="font-size:11px">${esc(pgSelf.index||'').replace(/\n/g,' ')}</td>
      <td><b>${esc(pgSelf.name||'')}</b> <span class="badge b-ph">我方</span></td>
      <td><span class="badge b-ph">鹏华</span></td>
      <td style="font-family:var(--mono)">${pgSelf.aum||'—'}</td>
      <td style="font-size:11px">${esc((pgSelf.hot_a||'').replace(/\n/g,' '))}</td>
      <td style="font-size:11px">${esc((pgSelf.hot_b||'').replace(/\n/g,' '))}</td>
      <td style="font-size:11px">${esc((pgSelf.sector_a||'').replace(/\n/g,' '))}</td>
      <td>${pgSelf.ret_1m?fp(parseFloat(pgSelf.ret_1m)*100):'—'}</td>
      <td>${pgSelf.ret_3m?fp(parseFloat(pgSelf.ret_3m)*100):'—'}</td>
      <td>${pgSelf.ret_6m?fp(parseFloat(pgSelf.ret_6m)*100):'—'}</td>
      <td>${pgSelf.ret_1y?fp(parseFloat(pgSelf.ret_1y)*100):'—'}</td>
      <td>${pgSelf.bounce_6m?fp(parseFloat(pgSelf.bounce_6m)*100):'—'}</td>
    </tr>` : '';

  document.getElementById('d-peers-excel').innerHTML = selfRow + (peers_ex.length ?
    peers_ex.map(pe=>`
      <tr>
        <td style="font-size:11px;max-width:120px">${esc((pe.index||'').replace(/\n/g,' ').slice(0,30))}</td>
        <td>${esc(pe.name||'—')}</td>
        <td style="font-size:11px;color:var(--mu)">${esc(pe.company||'—')}</td>
        <td style="font-family:var(--mono)">${pe.aum||'—'}</td>
        <td style="font-size:11px;max-width:100px;overflow:hidden">${esc((pe.hot_a||'').replace(/\n/g,' ').slice(0,25))}</td>
        <td style="font-size:11px;max-width:100px;overflow:hidden">${esc((pe.hot_b||'').replace(/\n/g,' ').slice(0,25))}</td>
        <td style="font-size:11px;max-width:100px;overflow:hidden">${esc((pe.sector_a||'').replace(/\n/g,' ').slice(0,25))}</td>
        <td>${pe.ret_1m?fp(parseFloat(pe.ret_1m)*100):'—'}</td>
        <td>${pe.ret_3m?fp(parseFloat(pe.ret_3m)*100):'—'}</td>
        <td>${pe.ret_6m?fp(parseFloat(pe.ret_6m)*100):'—'}</td>
        <td>${pe.ret_1y?fp(parseFloat(pe.ret_1y)*100):'—'}</td>
        <td>${pe.bounce_6m?fp(parseFloat(pe.bounce_6m)*100):'—'}</td>
      </tr>`).join('') :
    '<tr><td colspan="12" style="text-align:center;padding:16px;color:var(--mu)">暂无基转基对标数据</td></tr>');

  // 持仓数据（季报）
  const holdings = data.holdings || {};
  const top10 = holdings.top10 || {};
  const industry = holdings.industry || {};
  const dynPeers = data.dynamic_peers || [];

  // 动态竞品展示（加入基转基对标区域）
  let dynPeerHtml = '';
  if(dynPeers.length){
    dynPeerHtml = `
      <div style="margin-top:12px;padding:10px 14px;background:#f0ede8;border-radius:4px">
        <div style="font-size:10px;color:var(--mu);font-family:var(--mono);margin-bottom:6px">动态竞品（同指数规模前2名）</div>
        ${dynPeers.map(pe=>`
          <div style="font-size:12px;margin-bottom:4px">
            <b>${esc(pe.name||'')}</b>
            <span style="color:var(--mu);margin-left:6px">${esc(pe.manager||'')}</span>
            <span style="font-family:var(--mono);margin-left:8px">${pe.aum?pe.aum.toFixed(2)+'亿':'—'}</span>
          </div>`).join('')}
      </div>`;
  }

  // 持仓卡片（插在产品标签卡片后面）
  let holdingsHtml = '';
  if(top10.stocks && top10.stocks.length){
    const dateStr = top10.date||'';
    holdingsHtml += `
      <div class="card" style="margin-bottom:14px">
        <div class="ch">
          <span class="ct">前十大重仓股</span>
          <span class="cm">季报 · ${esc(dateStr)}</span>
        </div>
        <div style="overflow-x:auto">
        <table>
          <thead><tr><th>#</th><th>股票名称</th><th>持仓占比</th></tr></thead>
          <tbody>
            ${top10.stocks.map((s,i)=>`
              <tr>
                <td style="font-family:var(--mono);color:var(--mu)">${i+1}</td>
                <td>${esc(s.name)}</td>
                <td>
                  <div style="display:flex;align-items:center;gap:6px">
                    <div style="width:${Math.min(s.weight*5,80)}px;height:3px;background:var(--ph);border-radius:2px"></div>
                    <span style="font-family:var(--mono);font-size:12px">${s.weight.toFixed(2)}%</span>
                  </div>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
        </div>
      </div>`;
  }
  if(industry.industries && industry.industries.length){
    const dateStr = industry.date||'';
    holdingsHtml += `
      <div class="card" style="margin-bottom:14px">
        <div class="ch">
          <span class="ct">行业配置</span>
          <span class="cm">季报 · ${esc(dateStr)}</span>
        </div>
        <div style="padding:12px 16px">
          ${industry.industries.slice(0,6).map(ind=>`
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:12px">
              <span style="min-width:90px;color:var(--t)">${esc(ind.name)}</span>
              <div style="flex:1;height:6px;background:#f0ede8;border-radius:3px">
                <div style="width:${Math.min(ind.weight,100)}%;height:6px;background:var(--ph);border-radius:3px"></div>
              </div>
              <span style="font-family:var(--mono);min-width:40px;text-align:right">${ind.weight.toFixed(1)}%</span>
            </div>`).join('')}
        </div>
      </div>`;
  }

  // 插入持仓卡片（在AI卖点框之前）
  const aiBox = document.getElementById('ai-result-box');
  const existingHoldings = document.getElementById('holdings-section');
  if(existingHoldings) existingHoldings.remove();
  if(holdingsHtml){
    const div = document.createElement('div');
    div.id = 'holdings-section';
    div.innerHTML = holdingsHtml;
    aiBox.parentNode.insertBefore(div, aiBox);
  }

  // 动态竞品插入基转基区域
  const dynEl = document.getElementById('dynamic-peers-box');
  if(dynEl) dynEl.remove();
  if(dynPeerHtml){
    const div = document.createElement('div');
    div.id = 'dynamic-peers-box';
    div.innerHTML = dynPeerHtml;
    document.getElementById('d-peer-excel-card').appendChild(div);
  }

  // 显示基转基话术
  if(pgSelf.slogan){
    const sl=document.getElementById('d-peer-slogan');
    sl.style.display='block';
    sl.innerHTML='<b>基转基参考话术：</b><br>'+esc(pgSelf.slogan).replace(/\n/g,'<br>');
  }
}

// ══ AI生成 ══
async function doAI(){
  if(!currentCode){toast('请先选择产品');return}
  document.getElementById('btn-ai').disabled=true;
  document.getElementById('ai-content').innerHTML=`
    <div class="loading-ai">
      <span class="spinner"></span>AI正在分析最新数据，生成专属话术...（约10-20秒）
    </div>`;

  try {
    const data = await fetch(`/api/ai_slogan/${currentCode}`,{method:'POST'}).then(r=>r.json());
    if(data.error && !data.slogan_v1){
      document.getElementById('ai-content').innerHTML=`<div style="color:var(--red);padding:8px">${esc(data.error)}</div>`;
    } else {
      renderAI(data);
    }
  } catch(e){
    document.getElementById('ai-content').innerHTML=`<div style="color:var(--red);padding:8px">请求失败：${esc(e.message)}</div>`;
  }
  document.getElementById('btn-ai').disabled=false;
}

function renderAI(d){
  const pts = (arr, color='var(--ph)') => (arr||[]).map((t,i)=>`
    <div class="point-item" style="display:flex;align-items:flex-start;gap:6px">
      <span class="point-num" style="background:${color}15;color:${color}">${i+1}</span>
      <span>${esc(t)}</span>
    </div>`).join('');

  document.getElementById('ai-content').innerHTML = `
    ${d.slogan_v1?`<div class="slogan-box"><div class="slogan-label">版本1（AI生成）</div><div class="slogan-text">${esc(d.slogan_v1)}</div></div>`:''}
    ${d.slogan_v2?`<div class="slogan-box" style="border-left-color:#1a4a7a"><div class="slogan-label">版本2（AI生成）</div><div class="slogan-text">${esc(d.slogan_v2)}</div></div>`:''}
    ${d.product_points?.length?`
      <div style="margin-top:12px;margin-bottom:4px;font-size:11px;color:var(--mu);font-family:var(--mono)">产品维度卖点</div>
      ${pts(d.product_points,'#3d2b8a')}`:'' }
    ${d.index_points?.length?`
      <div style="margin-top:10px;margin-bottom:4px;font-size:11px;color:var(--mu);font-family:var(--mono)">指数维度卖点</div>
      ${pts(d.index_points,'#1a4a7a')}`:'' }
    ${d.peer_pitch?`
      <div style="margin-top:10px;padding:10px 14px;background:#f9f8f6;border-radius:4px;border-left:3px solid #2d5a3d">
        <div style="font-size:10px;color:var(--mu);font-family:var(--mono);margin-bottom:4px">基转基话术</div>
        <div style="font-size:13px;line-height:1.7">${esc(d.peer_pitch)}</div>
      </div>`:''}
    <div style="margin-top:8px;font-size:10px;color:var(--mu);font-family:var(--mono)">
      ⚡ Claude AI生成 · ${new Date().toLocaleString('zh')} · 数据请以官方为准
    </div>`;
}

// ══ 复制所有话术 ══
function copyAll(){
  const el = document.getElementById('ai-content');
  if(!el || !el.innerText.trim() || el.innerText.includes('点击')) {
    toast('请先点击「AI生成卖点」'); return;
  }
  navigator.clipboard.writeText(el.innerText).then(()=>toast('已复制 ✓'));
}

// ══ 导出总览CSV ══
function exportOverviewCsv(){
  if(!allProducts.length){toast('暂无数据');return}
  let csv='\ufeff产品名称,代码,板块,今日涨跌(%),折溢价率(%),实时规模(亿),同业排名,主理人\n';
  allProducts.forEach(p=>csv+=`"${p.name}",${p.code},"${p.board||p.category||''}",${p.pct_chg||''},${p.premium||''},${p.aum_rt||''},${p.peer_rank?p.peer_rank+'/'+p.peer_total:''},"${p.manager||''}"\n`);
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download=`鹏华ETF_${new Date().toLocaleDateString('zh')}.csv`;
  a.click(); toast('已导出 ✓');
}

// ══ 初始化 ══
window.onload = async () => {
  const s = await fetch('/api/status').then(r=>r.json());
  if(s.status==='ok'){
    setStatus('ok','数据正常');
    document.getElementById('upd').textContent=s.last_update||'';
    await loadProducts();
  } else {
    setStatus('idle','待加载');
    // 启动时自动预加载
    doRefresh();
  }
};
</script>
</body></html>"""


# ══════════════════════════════════════════
# 启动
# ══════════════════════════════════════════
if __name__ == "__main__":
    load_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  鹏华ETF AI营销平台 v4")
    print(f"  访问: http://localhost:{port}")
    print(f"  密码: {ACCESS_PASSWORD}")
    print(f"  AI: {'已配置' if ANTHROPIC_API_KEY else '未配置 (请设置ANTHROPIC_API_KEY环境变量)'}")
    # 生产环境启动后自动预加载数据
    if os.environ.get("PORT"):
        threading.Thread(target=fetch_all_data, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
