"""鹏华ETF AI营销平台 v8 - 已验证接口版本"""
import os
# 绕过系统代理（关掉代理后此设置确保Python不走任何代理）
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
from flask import Flask,jsonify,render_template_string,request,session,redirect
import threading,time,os,json,functools,re,base64
from datetime import datetime,date
from concurrent.futures import ThreadPoolExecutor,as_completed

app=Flask(__name__)
app.secret_key=os.environ.get("SECRET_KEY","phfund2024etf")
PW=os.environ.get("ACCESS_PASSWORD","penghua2024")
AI_KEY=os.environ.get("ANTHROPIC_API_KEY","")

# ══ 内嵌数据（base64）══
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'embeds.py')).read() if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),'embeds.py')) else "S='';I='';C=''")

def _dec(b): 
    try: return json.loads(base64.b64decode(b).decode()) if b else {}
    except: return {}

_SLIM=_dec(S); _IDX=_dec(I); _CAT=_dec(C)
PRODUCTS=_SLIM.get('products',{})
# 清理category字段（去掉换行/首发日期等脏数据，如"港美股\n2026.3.23首发"→"港美股"）
import re as _re
for _code,_p in PRODUCTS.items():
    cat = _p.get('category','') or ''
    cat = _re.sub(r'[\n\r].*','',cat).split('（')[0].split('(')[0].strip()
    _p['category'] = cat
# 清理category字段（去掉换行/首发日期等脏数据）
for _code,_p in PRODUCTS.items():
    cat=_p.get('category','') or ''
    # 去掉换行后的内容（如"港美股\n2026.3.23首发" -> "港美股"）
    if '\n' in cat: cat = cat.split('\n')[0].strip()
    if '（' in cat: cat = cat.split('（')[0].strip()
    if '(' in cat: cat = cat.split('(')[0].strip()
    _p['category']=cat
IDX_TO_CODES=_IDX  # index_name -> [{code,name,manager}]
CODE_TO_CAT=_CAT.get('code_to_cat',{})
CAT_TO_ALL=_CAT.get('cat_to_all',{})
ALL_NAME=_CAT.get('all_code_name',{})
ALL_MGR=_CAT.get('all_code_manager',{})

# 指数名称->代码（用于拉历史行情）
# 指数名称->新浪代码（用于拉历史行情，新浪接口全球可访问）
INDEX_CODE={
    "沪深300":"sh000300","中证500":"sh000905","中证1000":"sh000852","中证800":"sh000906",
    "中证A500":"sh000510","中证A50":"sh000050","中证国防":"sz399973","中证银行":"sz399986",
    "中证传媒":"sz399971","中证酒":"sh930782","中证中药":"sh000978","中证畜牧":"sh931579",
    "中证现金流":"sh932365","800现金流":"sh932368","光伏产业":"sh931151","机器人产业":"sz980032",
    "云计算":"sh930851","工业互联":"sh931142","CS车联网":"sh930899","细分化工":"sh000813",
    "全指公用":"sh000270","内地低碳":"sz399989","科创50":"sh000688","科创100":"sh000698",
    "科创200":"sh000699","科创综指":"sh000681","科创创业AI":"sh932000","科创新能":"sh000692",
    "科创生物":"sh000683","科创芯片":"sh000685","科创芯片设计":"sh950125","创业板综":"sz399102",
    "创业板50":"sz399673","创新能源":"sz399814","国证有色":"sz399395","国证粮食":"sz399294",
    "国证油气":"sz399308","证券龙头":"sz399437","工业有色":"sh930708","食品":"sh000998",
    "消费电子":"sz399994","疫苗生科":"sz399998","港股通科技":"sh931573","港股通消费":"sh931454",
    "港股通医药C":"sh930965","港股通创新药":"sh931787","ESG 300":"sh931463","上证180":"sh000010",
    "国证芯片(CNI)":"sz980017","中证800证保":"sz399966","中证电信":"sh930901",
    "卫星产业":"sh932209","通用航空":"sh932210","工程机械主题":"sh931244","金融科技":"sz399699",
    "科创AI":"sh000685",
}
# 也保留纯数字代码映射（用于成分股接口）
INDEX_CODE_NUM={k:v[2:] for k,v in INDEX_CODE.items()}
HK_INDICES={"恒生科技","恒生指数","恒生生物科技","恒生中国央企指数","道琼斯工业平均",
            "标普港股通低波红利指数(港币)"}

# ══ 缓存 ══
C_={
    "status":"idle","error":"","last_update":None,
    "spot":{},        # code->{price,pct_chg,premium,turnover_yi,aum}
    "index_perf":{},  # idx_code->{ret_1m,ret_3m,ret_6m,ret_1y,max_dd,bounce}
    "index_cons":{},  # idx_code->{top10,date}  (成分股)
    "peer_aum":{},    # idx_name->[{code,name,manager,aum,premium,pct_chg}]
}
LK=threading.Lock()

# ══ 登录 ══
def lr(f):
    @functools.wraps(f)
    def w(*a,**k):
        if not session.get("ok"):
            return (jsonify({"error":"未登录"}),401) if request.path.startswith("/api/") else redirect("/login")
        return f(*a,**k)
    return w

@app.route("/login",methods=["GET","POST"])
def login():
    e=""
    if request.method=="POST":
        if request.form.get("password")==PW: session["ok"]=True; return redirect("/")
        e="密码错误"
    return render_template_string(LOGIN_HTML,error=e)

@app.route("/logout")
def logout(): session.clear(); return redirect("/login")

# ══ 数据拉取 ══
def sf(v):
    try: return round(float(str(v).replace("%","").replace(",","").strip()),4)
    except: return None

def fetch_spot():
    try:
        import akshare as ak
        df=ak.fund_etf_spot_em()
        print(f"[spot] 全部列名({len(df.columns)}列): {list(df.columns)}")
        if len(df)>0:
            row0=df.iloc[0]
            print(f"[spot] 第一行: { {col:str(row0[col])[:15] for col in df.columns} }")
        # 获取数据日期（用于标注）
        data_date = ""
        if len(df) > 0:
            raw_date = str(df.iloc[0].get("数据日期",""))
            if raw_date and raw_date != "nan":
                # 格式：2026-04-10 00:00，取日期部分并推算
                try:
                    from datetime import datetime as _dt, timedelta as _td
                    d = _dt.strptime(raw_date[:10], "%Y-%m-%d").date()
                    data_date = d.strftime("%m月%d日")
                except:
                    data_date = raw_date[:10]
        print(f"[spot] 数据日期: {data_date}")

        r={}
        for _,row in df.iterrows():
            c=str(row.get("代码","")).strip()
            if not c: continue
            price=sf(row.get("最新价"))
            pct=sf(row.get("涨跌幅"))
            prem=sf(row.get("基金折价率"))
            # 最新份额（份）× 最新价 ÷ 1亿 = 规模（亿）
            shares=sf(row.get("最新份额"))
            if shares and price and shares > 0 and price > 0:
                aum=round(shares * price / 1e8, 2)
            else:
                # fallback：总市值（元）÷ 1亿
                mktcap=sf(row.get("总市值"))
                aum=round(mktcap/1e8,2) if mktcap and mktcap>0 else None
            # 成交额（元）转亿
            raw_vol=sf(row.get("成交额",0)) or 0
            vol_yi=round(raw_vol/1e8,2) if raw_vol and raw_vol>1e4 else None
            r[c]={"price":price,"pct_chg":pct,"premium":prem,
                  "turnover_yi":vol_yi,"aum":aum,"shares":shares}
        aum_count=sum(1 for v in r.values() if v.get('aum'))
        print(f"[spot] {len(r)}只，规模非空: {aum_count}")
        # 把数据日期存到全局供前端显示
        import builtins; builtins._SPOT_DATE = data_date
        return r
    except Exception as e:
        print(f"[spot] 失败: {e}"); return {}

def fetch_perf(sina_code):
    """用新浪接口拉指数历史行情（全球可访问）"""
    if not sina_code or sina_code in ('','None'): return None
    # 跳过港股/海外
    if not sina_code.startswith(('sh','sz')): return None
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_zh_index_daily(symbol=sina_code)
        if df is None or df.empty: return None
        df = df.sort_values("date")
        c = df["close"].astype(float).values
        if len(c) < 5: return None
        def r(n): return round((c[-1]/c[-n]-1)*100,2) if len(c)>=n else None
        c1y = c[-252:] if len(c)>=252 else c
        cs = pd.Series(c1y); roll = cs.cummax()
        dd = round(((cs-roll)/roll).min()*100,2)
        mi = int(cs.idxmin())
        bounce = round((c1y[-1]/c1y[mi]-1)*100,2) if mi<len(c1y)-1 else 0
        return {"ret_1m":r(22),"ret_3m":r(63),"ret_6m":r(126),"ret_1y":r(252),
                "max_dd":dd,"bounce":bounce}
    except Exception as e:
        print(f"[perf] {sina_code}: {e}")
        return None

def fetch_cons(idx_code):
    """拉指数成分股权重"""
    if not idx_code or idx_code.startswith(('HS','DJI','SP')): return {}
    try:
        import akshare as ak
        import pandas as pd
        # 方式1：中证权重接口（akshare>=1.18.54有权重列，值为小数如0.442表示0.442%）
        try:
            df = ak.index_stock_cons_weight_csindex(symbol=idx_code)
            if df is not None and not df.empty and '权重' in df.columns:
                df = df.rename(columns={'成分券名称': 'name', '权重': 'weight'})
                df['weight'] = pd.to_numeric(df['weight'], errors='coerce').fillna(0)
                df = df.sort_values('weight', ascending=False)
                top10 = [{"name": str(row['name']), "weight": round(float(row['weight']), 3)}
                         for _, row in df.head(10).iterrows()]
                report_date = str(df.iloc[0].get('日期', date.today()))[:10]
                print(f"[cons] {idx_code} 权重接口OK，前3: {top10[:3]}")
                return {"top10": top10, "date": report_date, "total": len(df)}
        except Exception as e1:
            print(f"[cons] {idx_code} 权重接口失败: {e1}")
        # 方式2：中证成分股接口（无权重，等权）
        try:
            df = ak.index_stock_cons_csindex(symbol=idx_code)
            if df is not None and not df.empty:
                df = df.rename(columns={'成分券名称': 'name'})
                n = len(df)
                top10 = [{"name": str(row.get('name', '')), "weight": round(100/n, 2)}
                         for _, row in df.head(10).iterrows()]
                print(f"[cons] {idx_code} 等权接口OK，共{n}只")
                return {"top10": top10, "date": date.today().isoformat(),
                        "total": n, "equal_weight": True}
        except Exception as e2:
            print(f"[cons] {idx_code} 等权接口失败: {e2}")
        # 方式3：通用成分股接口
        try:
            df = ak.index_stock_cons(symbol=idx_code)
            if df is not None and not df.empty:
                n = len(df)
                col = '品种名称' if '品种名称' in df.columns else df.columns[1]
                top10 = [{"name": str(row.get(col, '')), "weight": round(100/n, 2)}
                         for _, row in df.head(10).iterrows()]
                print(f"[cons] {idx_code} 通用接口OK，共{n}只")
                return {"top10": top10, "date": date.today().isoformat(),
                        "total": n, "equal_weight": True}
        except Exception as e3:
            print(f"[cons] {idx_code} 通用接口失败: {e3}")
        return {}
    except Exception as e:
        print(f"[cons] {idx_code}: {e}"); return {}

def fetch_all():
    with LK: C_["status"]="loading"; C_["error"]=""
    try:
        # 1. ETF行情
        print("[1/3] 拉取ETF行情...")
        spot=fetch_spot()
        print(f"      获取{len(spot)}只")

        # 2. 同业排名
        print("[2/3] 计算同业排名...")
        peer_aum={}
        for idx_name,entries in IDX_TO_CODES.items():
            peers=[{"code":e["code"],"name":e["name"],"manager":e["manager"],
                    "aum":spot.get(e["code"],{}).get("aum"),
                    "premium":spot.get(e["code"],{}).get("premium"),
                    "pct_chg":spot.get(e["code"],{}).get("pct_chg")}
                   for e in entries]
            peers.sort(key=lambda x:x["aum"] or 0,reverse=True)
            peer_aum[idx_name]=peers

        # 3. 指数历史行情（并发拉取，跳过港股海外）
        # 3. 指数历史行情（用新浪接口，全球可访问）
        print("[3/3] 拉取指数历史...")
        sina_codes = list({v for k,v in INDEX_CODE.items()
                           if k not in HK_INDICES and v and v.startswith(('sh','sz'))})
        index_perf = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(fetch_perf, sc): sc for sc in sina_codes[:45]}
            for fut in as_completed(futs):
                sc = futs[fut]
                r = fut.result()
                if r: index_perf[sc] = r
        print(f"      获取{len(index_perf)}个指数")

        with LK:
            C_["spot"]=spot; C_["peer_aum"]=peer_aum; C_["index_perf"]=index_perf
            C_["last_update"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            C_["status"]="ok"
        print("[完成]")
    except Exception as e:
        with LK: C_["status"]="error"; C_["error"]=str(e)
        print(f"[错误] {e}")

# ══ API ══
@app.route("/api/refresh",methods=["POST"])
@lr
def api_refresh():
    if C_["status"]=="loading": return jsonify({"ok":False})
    threading.Thread(target=fetch_all,daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/status")
@lr
def api_status():
    import builtins
    spot_date = getattr(builtins, '_SPOT_DATE', '')
    with LK: return jsonify({"status":C_["status"],"last_update":C_["last_update"],
                             "error":C_["error"],"spot_date":spot_date})

@app.route("/api/products")
@lr
def api_products():
    with LK: spot=C_["spot"]; peer_aum=C_["peer_aum"]
    out=[]
    for code,p in PRODUCTS.items():
        sp=spot.get(code,{}); idx=p.get("index_name","")
        peers=peer_aum.get(idx,[])
        rank=next((i+1 for i,pe in enumerate(peers) if pe["code"]==code),None)
        out.append({**p,"price":sp.get("price"),"pct_chg":sp.get("pct_chg"),
                    "premium":sp.get("premium"),"aum":sp.get("aum"),
                    "turnover_yi":sp.get("turnover_yi"),
                    "peer_rank":rank,"peer_total":len(peers)})
    sort_by = request.args.get("sort","aum")
    if sort_by == "pct_chg":
        out.sort(key=lambda x: x.get("pct_chg") or -999, reverse=True)
    elif sort_by == "pct_chg_asc":
        out.sort(key=lambda x: x.get("pct_chg") or 999, reverse=False)
    else:
        out.sort(key=lambda x: x.get("aum") or 0, reverse=True)
    return jsonify(out)

@app.route("/api/product/<code>")
@lr
def api_product(code):
    p=PRODUCTS.get(code)
    if not p: return jsonify({"error":"不存在"}),404
    with LK:
        sp=C_["spot"].get(code,{}); idx=p.get("index_name","")
        peers_rt=C_["peer_aum"].get(idx,[])
        idx_sina=INDEX_CODE.get(idx,"")
        idx_code=INDEX_CODE_NUM.get(idx,"")  # 纯数字代码，用于成分股接口
        idx_perf=C_["index_perf"].get(idx_sina,{})
        cons=C_["index_cons"].get(idx_code,{})
    # 同类排名
    cat=CODE_TO_CAT.get(code,"")
    own_rank=next((i+1 for i,pe in enumerate(peers_rt) if pe["code"]==code),None)
    dyn_peers=[pe for pe in peers_rt if pe["code"]!=code and pe.get("aum")][:2]
    # 触发成分股后台拉取
    is_hk=p.get("index_name","") in HK_INDICES
    if idx_code and not cons and not is_hk:
        threading.Thread(target=_bg_cons,args=(idx_code,),daemon=True).start()
    elif idx_sina and not idx_code and not cons and not is_hk:
        # fallback: 用新浪代码的数字部分
        threading.Thread(target=_bg_cons,args=(idx_sina[2:],),daemon=True).start()
    return jsonify({"product":p,"spot":sp,"index_name":idx,
                    "index_code":idx_code,"index_sina":idx_sina,
                    "index_perf":idx_perf,"cons":cons,"peers_rt":peers_rt[:12],
                    "dyn_peers":dyn_peers,"own_rank":own_rank,"category":cat,
                    "is_hk":is_hk,"last_update":C_.get("last_update","")})

def _bg_cons(idx_code):
    r=fetch_cons(idx_code)
    if r:
        with LK: C_["index_cons"][idx_code]=r

@app.route("/api/cons/<idx_code>")
@lr
def api_cons(idx_code):
    with LK: cached=C_["index_cons"].get(idx_code,{})
    if cached: return jsonify(cached)
    r=fetch_cons(idx_code)
    if r:
        with LK: C_["index_cons"][idx_code]=r
    return jsonify(r)

@app.route("/api/cat/<path:cat>")
@lr
def api_cat(cat):
    codes=CAT_TO_ALL.get(cat,[])
    with LK: spot=C_["spot"]
    out=[]
    for code in codes:
        sp=spot.get(code,{})
        out.append({"code":code,"name":ALL_NAME.get(code,code),
                    "manager":ALL_MGR.get(code,""),
                    "is_ph":"鹏华" in ALL_MGR.get(code,""),
                    "aum":sp.get("aum"),"premium":sp.get("premium"),
                    "pct_chg":sp.get("pct_chg")})
    out.sort(key=lambda x:x.get("aum") or 0,reverse=True)
    return jsonify({"cat":cat,"total":len(out),"etfs":out[:30]})

@app.route("/api/ai/<code>",methods=["POST"])
@lr
def api_ai(code):
    if not AI_KEY: return jsonify({"error":"请在Railway配置 ANTHROPIC_API_KEY"})
    p=PRODUCTS.get(code)
    if not p: return jsonify({"error":"产品不存在"})
    with LK:
        sp=C_["spot"].get(code,{}); idx=p.get("index_name","")
        idx_sina=INDEX_CODE.get(idx,"")
        idx_code=INDEX_CODE_NUM.get(idx,"")
        perf=C_["index_perf"].get(idx_sina,{})
        cons=C_["index_cons"].get(idx_code,{})
        peers=C_["peer_aum"].get(idx,[])
    own_rank=next((i+1 for i,pe in enumerate(peers) if pe["code"]==code),None)
    dyn=[pe for pe in peers if pe["code"]!=code and pe.get("aum")][:2]
    prompt=_prompt(p,sp,idx,perf,cons,peers,own_rank,dyn)
    try:
        import urllib.request
        pl=json.dumps({"model":"claude-sonnet-4-20250514","max_tokens":1000,
                       "messages":[{"role":"user","content":prompt}]}).encode()
        req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=pl,
            headers={"Content-Type":"application/json","x-api-key":AI_KEY,"anthropic-version":"2023-06-01"})
        with urllib.request.urlopen(req,timeout=30) as resp:
            data=json.loads(resp.read())
        text="".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
        m=re.search(r'\{[\s\S]*\}',text)
        if m:
            r=json.loads(m.group()); r["raw"]=text; return jsonify(r)
        return jsonify({"raw":text,"error":"格式异常"})
    except Exception as e: return jsonify({"error":str(e)})

def _prompt(p,sp,idx,perf,cons,peers,rank,dyn):
    pf=[]; 
    for k,l in [("ret_1m","近1月"),("ret_3m","近3月"),("ret_6m","近6月"),("ret_1y","近1年")]:
        v=perf.get(k)
        if v is not None: pf.append(f"{l}{'+' if v>=0 else ''}{v:.2f}%")
    if perf.get("bounce"): pf.append(f"低点弹性+{perf['bounce']:.2f}%")
    top3=cons.get("top10",[])[:3]
    t3="、".join(f"{s['name']}({s['weight']:.1f}%)" for s in top3) if top3 else "加载中"
    aum=sp.get("aum"); pct=sp.get("pct_chg"); prem=sp.get("premium")
    rank_s=f"同指数共{len(peers)}只，规模第{rank}名" if rank else f"同指数{len(peers)}只"
    dyn_s="\n".join(f"竞品{i+1}:{pe['name']}({pe.get('manager','')})规模{pe['aum']:.1f}亿"
                    for i,pe in enumerate(dyn)) if dyn else "无竞品数据"
    return f"""你是鹏华基金ETF营销专家，为「{p.get('name','')}」生成话术。

产品：{p.get('name','')}({p.get('code','')}) 板块:{p.get('board',p.get('category',''))}
实时规模：{f"{aum:.2f}亿" if aum else "—"} 今日涨跌：{f"{pct:+.2f}%" if pct else "—"} 折溢价：{f"{prem:+.2f}%" if prem else "—"}
独家：{"✅" if p.get("is_exclusive") else "❌"} 费率最低：{"✅" if p.get("fee_lowest") else "❌"}

指数({idx})表现：{" | ".join(pf) or "暂无"}
前三大成分股：{t3}
同业：{rank_s}
{dyn_s}

请用JSON输出（不加```）：
{{"slogan_v1":"15字内卖点","slogan_v2":"20字内第二版本","product_points":["产品卖点1","2","3"],"index_points":["指数卖点1","2"],"peer_pitch":"基转基话术≤120字"}}
只引用真实数据，禁止编造。"""

@app.route("/")
@lr
def index(): return render_template_string(HTML)

LOGIN_HTML="""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#f5f4f0;font-family:'Noto Sans SC',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}.box{background:#fff;border:1px solid #e2e0d8;border-radius:10px;padding:52px 44px;width:380px}.brand{font-family:'DM Mono',monospace;font-size:11px;color:#aaa;letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}.title{font-size:20px;font-weight:500;margin-bottom:6px}.sub{font-size:12px;color:#999;margin-bottom:32px}label{font-size:11px;color:#999;font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:6px}input{width:100%;border:1px solid #e2e0d8;border-radius:5px;padding:11px 13px;font-size:14px;font-family:'Noto Sans SC',sans-serif;margin-bottom:22px;color:#1a1916;outline:none}input:focus{border-color:#3d2b8a}button{width:100%;background:#1a1916;color:#f0ede6;border:none;border-radius:5px;padding:12px;font-size:14px;cursor:pointer}button:hover{background:#333}.err{color:#c0392b;font-size:13px;margin-bottom:16px}</style></head><body>
<div class="box"><div class="brand">鹏华基金管理有限公司</div><div class="title">ETF AI营销平台</div><div class="sub">实时数据 · AI生成话术 · 同业对比</div>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="POST"><label>访问密码</label><input type="password" name="password" placeholder="请输入团队密码" autofocus><button type="submit">进入平台</button></form></div></body></html>"""

HTML=r"""<!DOCTYPE html><html lang="zh"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>鹏华ETF · AI营销平台</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#f5f4f0;--s:#fff;--b:#e2e0d8;--t:#1a1916;--mu:#7a7870;--ph:#3d2b8a;--phl:#eeebf8;--red:#c0392b;--grn:#2d5a3d;--grnl:#e8f0eb;--amb:#b7790f;--blu:#1a4a7a;--mono:'DM Mono',monospace;--sans:'Noto Sans SC',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:var(--sans);color:var(--t);font-size:14px;line-height:1.6;height:100vh;overflow:hidden;display:grid;grid-template-columns:240px 1fr}
aside{background:var(--t);display:flex;flex-direction:column;height:100vh;overflow-y:auto}
main{overflow-y:auto;padding:22px 26px}
.logo{padding:18px 20px 14px;border-bottom:1px solid #2a2826}
.logo-sub{font-family:var(--mono);font-size:10px;color:#555;letter-spacing:.12em;text-transform:uppercase;margin-bottom:4px}
.logo-name{font-size:14px;font-weight:500;color:#f0ede6;line-height:1.35}
.logo-badge{display:inline-block;margin-top:5px;font-size:10px;font-family:var(--mono);background:#1a3320;color:#8fbc9c;padding:2px 8px;border-radius:3px}
nav{padding:8px 0;flex:1}.nl{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:#444;padding:8px 20px 4px}
.ni{display:flex;align-items:center;gap:8px;padding:8px 20px;font-size:13px;color:#888;cursor:pointer;border-left:2px solid transparent;transition:all .1s}
.ni:hover{color:#f0ede6;background:#1e1d1a}.ni.on{color:#f0ede6;border-left-color:#8fbc9c;background:#1a1916}
.nd{width:5px;height:5px;border-radius:50%;background:#333;flex-shrink:0}.ni.on .nd{background:#8fbc9c}
.sb-s{padding:8px 20px}.sb-s input{width:100%;background:#2a2826;border:1px solid #3a3836;border-radius:4px;padding:6px 10px;font-size:12px;color:#ccc;outline:none;font-family:var(--sans)}
.sb-s input:focus{border-color:#534AB7}
.plist{overflow-y:auto}.pi{padding:7px 20px;cursor:pointer;border-left:2px solid transparent}
.pi:hover{background:#1e1d1a}.pi.on{background:#1a1916;border-left-color:#8fbc9c}
.pi-n{font-size:12px;color:#ccc;line-height:1.3}.pi.on .pi-n{color:#f0ede6}
.pi-m{font-size:10px;color:#555;font-family:var(--mono);margin-top:1px}
.sb-f{padding:12px 20px;border-top:1px solid #2a2826;margin-top:auto}
.sdot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#444;margin-right:5px;vertical-align:middle}
.sdot.ok{background:#5ab97a}.sdot.loading{background:#f0b429;animation:bk 1s infinite}.sdot.error{background:#e05a4e}
@keyframes bk{0%,100%{opacity:1}50%{opacity:.2}}
.panel{display:none}.panel.on{display:block}
.ph{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:18px;gap:12px}
.pt{font-size:19px;font-weight:500;letter-spacing:-.02em}.ps{font-size:12px;color:var(--mu);margin-top:2px;font-family:var(--mono)}
.btns{display:flex;gap:6px;flex-shrink:0}
.btn{padding:6px 13px;border-radius:4px;border:1px solid var(--b);background:var(--s);cursor:pointer;font-size:12px;font-family:var(--sans);color:var(--t);transition:all .1s;white-space:nowrap}
.btn:hover{border-color:#888}.btn.pri{background:var(--t);color:#f0ede6;border-color:var(--t)}.btn.ai{background:var(--ph);color:#fff;border-color:var(--ph)}.btn:disabled{opacity:.4;cursor:not-allowed}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:16px}
.mc{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:11px 13px}
.ml{font-size:10px;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.mv{font-size:20px;font-weight:300;letter-spacing:-.025em}.mn{font-size:11px;color:var(--mu);margin-top:2px}
.up{color:var(--red)}.dn{color:var(--blu)}.warn{color:var(--amb)}
.card{background:var(--s);border:1px solid var(--b);border-radius:6px;overflow:hidden;margin-bottom:12px}
.ch{padding:10px 14px;border-bottom:1px solid var(--b);display:flex;align-items:center;justify-content:space-between;gap:8px}
.ct{font-size:13px;font-weight:500}.cm{font-size:11px;color:var(--mu);font-family:var(--mono)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:7px 11px;font-size:10px;font-weight:400;color:var(--mu);font-family:var(--mono);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--b);background:#faf9f7;white-space:nowrap}
td{padding:8px 11px;border-bottom:1px solid #f0ede8;vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:#faf9f7}
.ph-r td{background:var(--phl)}.ph-r:hover td{background:#e5e1f5}
.r1 td{background:#e8f0eb}.r1:hover td{background:#dce8de}
.badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-family:var(--mono)}
.b-ph{background:var(--phl);color:var(--ph)}.b-g{background:var(--grnl);color:var(--grn)}.b-gray{background:#f0ede8;color:#666}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.kv{display:flex;align-items:baseline;gap:8px;font-size:13px;padding:3px 0}
.kl{color:var(--mu);font-size:11px;min-width:78px;flex-shrink:0}
.perf6{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin-bottom:10px}
.bar-r{display:flex;align-items:center;gap:6px;margin-bottom:6px;font-size:12px}
.bar-bg{flex:1;height:4px;background:var(--b);border-radius:2px}
.bar-f{height:4px;border-radius:2px;background:var(--ph)}
.ai-box{background:var(--s);border:1px solid var(--b);border-radius:6px;padding:14px;margin-bottom:12px}
.sl{background:#f9f8f6;border-radius:4px;padding:10px 13px;margin-bottom:8px;border-left:3px solid var(--ph)}
.sl-l{font-size:10px;color:var(--mu);font-family:var(--mono);text-transform:uppercase;margin-bottom:3px}
.sl-t{font-size:14px;font-weight:500;line-height:1.5}
.pt-i{padding:7px 0;border-bottom:1px solid #f0ede8;font-size:13px;line-height:1.6;display:flex;gap:6px}
.pt-i:last-child{border-bottom:none}
.pt-n{width:18px;height:18px;min-width:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-family:var(--mono);margin-top:2px}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #e2e0d8;border-top-color:var(--ph);border-radius:50%;animation:sp .8s linear infinite;margin-right:6px;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}
.mu-t{color:var(--mu);font-size:12px;font-family:var(--mono)}
#toast{position:fixed;bottom:16px;right:16px;background:var(--t);color:#f0ede6;padding:8px 13px;border-radius:4px;font-size:12px;font-family:var(--mono);opacity:0;transition:opacity .2s;pointer-events:none;z-index:999}
#toast.show{opacity:1}
::-webkit-scrollbar{width:3px;height:3px}::-webkit-scrollbar-thumb{background:#ccc;border-radius:2px}
</style></head><body>
<aside>
  <div class="logo">
    <div class="logo-sub">鹏华基金管理有限公司</div>
    <div class="logo-name">ETF AI营销平台</div>
    <span class="logo-badge">实时数据 · 69只产品</span>
  </div>
  <nav>
    <div class="nl">导航</div>
    <div class="ni on" id="nav-ov" onclick="showP('overview',this)"><span class="nd"></span>总览看板</div>
    <div class="ni" id="nav-dt" onclick="showP('detail',this)" style="display:none"><span class="nd"></span>产品详情</div>
    <div class="nl">产品列表</div>
  </nav>
  <div class="sb-s"><input placeholder="搜索产品..." id="sbq" oninput="fSb()"></div>
  <div class="plist" id="plist"></div>
  <div class="sb-f">
    <div style="font-size:11px;color:#666;font-family:var(--mono)"><span class="sdot" id="sdot"></span><span id="stxt">未加载</span></div>
    <div style="font-size:10px;color:#444;margin-top:2px;font-family:var(--mono)" id="upd"></div>
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      <button class="btn" style="font-size:11px;padding:4px 10px" onclick="doRefresh()">刷新数据</button>
      <a href="/logout" style="font-size:11px;color:#555;font-family:var(--mono)">退出</a>
    </div>
  </div>
</aside>

<main>
  <!-- 总览 -->
  <div class="panel on" id="p-overview">
    <div class="ph">
      <div><div class="pt">总览看板</div><div class="ps">全部鹏华场内ETF · 实时行情</div></div>
      <div class="btns">
        <select id="csel" onchange="fOv()" style="font-size:12px;padding:5px 9px;border:1px solid var(--b);border-radius:4px;background:var(--s)"><option value="">全部板块</option></select>
        <button class="btn" onclick="expCsv()">导出CSV</button>
        <button class="btn pri" onclick="doRefresh()">刷新数据</button>
      </div>
    </div>
    <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
      <span style="font-size:12px;color:var(--mu)">排序：</span>
      <button class="btn" id="sort-aum" onclick="setSort('aum')" style="font-size:11px;padding:4px 10px;background:var(--t);color:#f0ede6;border-color:var(--t)">规模↓</button>
      <button class="btn" id="sort-up" onclick="setSort('pct_chg')" style="font-size:11px;padding:4px 10px">涨幅↓</button>
      <button class="btn" id="sort-dn" onclick="setSort('pct_chg_asc')" style="font-size:11px;padding:4px 10px">跌幅↓</button>
    </div>
    <div class="metrics">
      <div class="mc"><div class="ml">产品总数</div><div class="mv" id="m-cnt">—</div><div class="mn">鹏华场内ETF</div></div>
      <div class="mc"><div class="ml">今日平均涨跌</div><div class="mv" id="m-avg">—</div><div class="mn">等权平均</div></div>
      <div class="mc"><div class="ml">规模最大</div><div class="mv" id="m-top" style="font-size:13px;margin-top:3px">—</div><div class="mn" id="m-top2"></div></div>
      <div class="mc"><div class="ml">溢价率>1%</div><div class="mv warn" id="m-prem">—</div><div class="mn">只需关注</div></div>
    </div>
    <div style="margin-bottom:10px"><input type="text" id="ovq" placeholder="搜索..." oninput="fOv()" style="padding:6px 11px;border:1px solid var(--b);border-radius:4px;font-size:13px;width:200px;background:var(--s);color:var(--t)"></div>
    <div class="card">
      <div class="ch"><span class="ct">全部产品</span><span class="cm" id="ov-cnt2"></span></div>
      <div style="overflow-x:auto">
      <table><thead><tr><th>产品名称</th><th>代码</th><th>板块</th><th>今日涨跌</th><th>折溢价率</th><th>规模估算(亿)</th><th>成交额(亿)</th><th>同指数排名</th><th>独家</th></tr></thead>
      <tbody id="ov-body"><tr><td colspan="9" style="text-align:center;padding:36px;color:var(--mu)">点击「刷新数据」加载</td></tr></tbody></table>
      </div>
    </div>
  </div>

  <!-- 详情 -->
  <div class="panel" id="p-detail">
    <div class="ph">
      <div><div class="pt" id="d-n">产品详情</div><div class="ps" id="d-s"></div></div>
      <div class="btns">
        <button class="btn" onclick="cpAI()">复制话术</button>
        <button class="btn ai" id="btn-ai" onclick="doAI()">✦ AI生成卖点</button>
      </div>
    </div>

    <div class="g2">
      <div class="card" style="margin:0">
        <div class="ch"><span class="ct">实时行情</span><span class="cm" id="d-time"></span></div>
        <div style="padding:12px 14px" id="d-spot"></div>
      </div>
      <div class="card" style="margin:0">
        <div class="ch"><span class="ct">同指数规模排名</span><span class="cm" id="d-rank-cm"></span></div>
        <div style="padding:12px 14px" id="d-rank"></div>
      </div>
    </div>
    <div style="margin-bottom:12px"></div>

    <div class="card">
      <div class="ch"><span class="ct">跟踪指数实时表现</span><span class="cm" id="d-idx-l"></span></div>
      <div style="padding:14px" id="d-idx"></div>
    </div>

    <div class="card">
      <div class="ch"><span class="ct">指数成分股</span><span class="cm" id="d-cons-l"></span></div>
      <div style="padding:14px" id="d-cons"><div class="mu-t"><span class="spin"></span>加载中...</div></div>
    </div>

    <div class="ai-box">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div style="font-size:13px;font-weight:500">✦ AI实时生成卖点 <span class="badge b-ph">Claude</span></div>
        <button class="btn" style="font-size:11px;padding:3px 9px" onclick="doAI()">重新生成</button>
      </div>
      <div id="ai-out"><div class="mu-t">点击「AI生成卖点」根据实时数据生成话术</div></div>
    </div>

    <div class="card">
      <div class="ch"><span class="ct">同指数ETF完整排名</span><span class="cm">实时规模</span></div>
      <div style="overflow-x:auto">
      <table><thead><tr><th>#</th><th>产品名称</th><th>管理人</th><th>实时规模(亿)</th><th>折溢价率</th><th>今日涨跌</th></tr></thead>
      <tbody id="d-peers"></tbody></table>
      </div>
    </div>

    <div class="card" id="d-cat-card" style="display:none">
      <div class="ch"><span class="ct">同类ETF规模排名</span><span class="cm" id="d-cat-cm"></span></div>
      <div style="overflow-x:auto">
      <table><thead><tr><th>#</th><th>产品名称</th><th>管理人</th><th>实时规模(亿)</th><th>折溢价率</th><th>今日涨跌</th></tr></thead>
      <tbody id="d-cat"></tbody></table>
      </div>
    </div>

    <div class="card" id="d-dyn-card" style="display:none">
      <div class="ch"><span class="ct">动态竞品对比</span><span class="cm">同指数规模前2名</span></div>
      <div style="overflow-x:auto">
      <table><thead><tr><th>产品</th><th>管理人</th><th>跟踪指数</th><th>实时规模(亿)</th><th>折溢价率</th><th>今日涨跌</th></tr></thead>
      <tbody id="d-dyn"></tbody></table>
      </div>
    </div>
  </div>
</main>
<div id="toast"></div>

<script>
let allP=[],cur=null,curD=null,consPoll=null;
const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function toast(msg,dur=2500){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),dur)}
function setSt(s,t){document.getElementById('sdot').className='sdot '+s;document.getElementById('stxt').textContent=t}
function fp(v,d=2){
  if(v==null||v===''||isNaN(parseFloat(v)))return'<span style="color:var(--mu)">—</span>';
  const n=parseFloat(v);return`<span class="${n>=0?'up':'dn'}">${n>=0?'+':''}${n.toFixed(d)}%</span>`;
}
function fn(v,d=2){return(v==null||isNaN(parseFloat(v)))?'—':parseFloat(v).toFixed(d)}

function showP(id,el){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('on'));
  document.getElementById('p-'+id).classList.add('on');
  if(el)el.classList.add('on');
}
function goDetail(code){
  cur=code;
  if(consPoll){clearInterval(consPoll);consPoll=null}
  document.getElementById('nav-dt').style.display='flex';
  showP('detail',document.getElementById('nav-dt'));
  document.querySelectorAll('.pi').forEach(el=>el.classList.toggle('on',el.dataset.code===code));
  loadD(code);
}
function fSb(){
  const q=document.getElementById('sbq').value.toLowerCase();
  const f=allP.filter(p=>!q||p.name.toLowerCase().includes(q)||p.code.includes(q)||(p.category||'').toLowerCase().includes(q));
  document.getElementById('plist').innerHTML=f.map(p=>`
    <div class="pi${p.code===cur?' on':''}" data-code="${p.code}" onclick="goDetail('${p.code}')">
      <div class="pi-n">${esc(p.name)}</div>
      <div class="pi-m">${p.code} · ${p.category||p.board||''}</div>
    </div>`).join('');
}
async function doRefresh(){
  setSt('loading','加载中...');toast('拉取实时数据，约1-2分钟...',4000);
  await fetch('/api/refresh',{method:'POST'});
  const iv=setInterval(async()=>{
    const r=await fetch('/api/status').then(r=>r.json());
    document.getElementById('upd').textContent=r.last_update||'';
    if(r.status==='ok'){clearInterval(iv);setSt('ok','数据正常');await loadProds();toast('已更新 ✓');if(cur)loadD(cur)}
    else if(r.status==='error'){clearInterval(iv);setSt('error','失败');toast('错误: '+r.error.slice(0,50),4000)}
  },2000);
}
let curSort='aum';
async function loadProds(){
  allP=await fetch('/api/products?sort='+curSort).then(r=>r.json());
  fSb();rOv(allP);
}
function setSort(s){
  curSort=s;
  ['sort-aum','sort-up','sort-dn'].forEach(id=>{
    const el=document.getElementById(id);
    if(el){el.style.cssText='font-size:11px;padding:4px 10px'}
  });
  const m={'aum':'sort-aum','pct_chg':'sort-up','pct_chg_asc':'sort-dn'}[s];
  const el=document.getElementById(m);
  if(el) el.style.cssText='font-size:11px;padding:4px 10px;background:var(--t);color:#f0ede6;border-color:var(--t)';
  loadProds();
}
function rOv(data){
  const cats=[...new Set(data.map(p=>p.category||p.board||'').filter(Boolean))].sort();
  const sel=document.getElementById('csel'),cur=sel.value;
  sel.innerHTML='<option value="">全部板块</option>'+cats.map(c=>`<option value="${c}"${c===cur?' selected':''}>${esc(c)}</option>`).join('');
  fOv();
}
function fOv(){
  const q=document.getElementById('ovq').value.toLowerCase();
  const cat=document.getElementById('csel').value;
  const f=allP.filter(p=>(!q||p.name.toLowerCase().includes(q)||p.code.includes(q))&&(!cat||p.category===cat||p.board===cat));
  document.getElementById('m-cnt').textContent=allP.length+'只';
  const chgs=f.filter(p=>p.pct_chg!=null).map(p=>p.pct_chg);
  const avg=chgs.length?chgs.reduce((a,b)=>a+b,0)/chgs.length:null;
  document.getElementById('m-avg').innerHTML=avg!=null?fp(avg):'—';
  const top=[...f].sort((a,b)=>(b.aum||0)-(a.aum||0))[0];
  if(top){document.getElementById('m-top').textContent=top.name;document.getElementById('m-top2').textContent=top.aum?top.aum.toFixed(1)+'亿':''}
  document.getElementById('m-prem').textContent=f.filter(p=>p.premium!=null&&Math.abs(p.premium)>1).length+'只';
  document.getElementById('ov-cnt2').textContent='共'+f.length+'只';
  document.getElementById('ov-body').innerHTML=f.map(p=>`
    <tr onclick="goDetail('${p.code}')" style="cursor:pointer">
      <td><b>${esc(p.name)}</b></td>
      <td style="font-family:var(--mono);color:var(--mu);font-size:11px">${p.code}</td>
      <td><span class="badge b-gray">${esc(p.category||p.board||'—')}</span></td>
      <td>${fp(p.pct_chg)}</td><td>${fp(p.premium)}</td>
      <td style="font-family:var(--mono)">${p.aum?p.aum.toFixed(2):'—'}</td>
      <td style="font-family:var(--mono)">${p.turnover_yi?p.turnover_yi.toFixed(2):'—'}</td>
      <td>${p.peer_rank?`<span class="badge ${p.peer_rank===1?'b-g':'b-gray'}">${p.peer_rank===1?'第1名':'第'+p.peer_rank+'/'+p.peer_total}</span>`:'—'}</td>
      <td>${p.is_exclusive?'<span class="badge b-g">独家</span>':''}</td>
    </tr>`).join('');
}
async function loadD(code){
  document.getElementById('d-n').textContent='加载中...';
  document.getElementById('d-cons').innerHTML='<div class="mu-t"><span class="spin"></span>拉取成分股...</div>';
  document.getElementById('ai-out').innerHTML='<div class="mu-t">点击「AI生成卖点」根据实时数据生成</div>';
  ['d-cat-card','d-dyn-card'].forEach(id=>document.getElementById(id).style.display='none');

  const data=await fetch('/api/product/'+code).then(r=>r.json());
  curD=data;
  const p=data.product||{},sp=data.spot||{},idx=data.index_perf||{},pr=data.peers_rt||[];
  const dyn=data.dyn_peers||[],cat=data.category||'';

  document.getElementById('d-n').textContent=p.name||code;
  document.getElementById('d-s').textContent=code+' · '+(p.index_name||'')+' · '+(p.category||p.board||'');
  document.getElementById('d-time').textContent=data.last_update||'';

  // 行情
  document.getElementById('d-spot').innerHTML=`
    <div class="kv"><span class="kl">最新价</span><span style="font-size:18px;font-weight:300">${sp.price?sp.price.toFixed(3):'—'}</span></div>
    <div class="kv"><span class="kl">今日涨跌</span><span style="font-size:16px">${fp(sp.pct_chg)}</span></div>
    <div class="kv"><span class="kl">折溢价率</span><span>${fp(sp.premium)}</span></div>
    <div class="kv"><span class="kl">实时规模</span><span style="font-weight:500">${sp.aum?sp.aum.toFixed(2)+'亿':'—'}</span></div>
    <div class="kv"><span class="kl">成交额</span><span style="color:var(--mu)">${sp.turnover_yi?sp.turnover_yi.toFixed(2)+'亿':'—'}</span></div>
    <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
      ${p.is_exclusive?'<span class="badge b-g">场内独家</span>':''}
      ${p.fee_lowest?'<span class="badge b-ph">费率最低档</span>':''}
    </div>`;

  // 排名
  const rank=pr.findIndex(pe=>pe.code===code)+1;
  document.getElementById('d-rank-cm').textContent='共'+pr.length+'只';
  document.getElementById('d-rank').innerHTML=rank?
    `<div style="font-size:28px;font-weight:300;color:${rank===1?'var(--grn)':'var(--t)'}">第${rank}名</div>
     <div style="font-size:12px;color:var(--mu);margin-top:4px">同指数${pr.length}只ETF实时规模排序</div>`:
    '<div class="mu-t">数据加载中</div>';

  // 指数表现
  document.getElementById('d-idx-l').textContent=(data.index_name||'')+' ('+(data.index_code||data.index_sina||'')+')';
  // 指数历史行情
  {
    const idxN = data.index_name||'';
    const idxCode = data.index_code||'';
    if(data.is_hk || idxN.includes('道琼斯') || idxN.includes('标普')){
      let url = data.is_hk||idxN.includes('恒生') ? 'https://www.hsi.com.hk' :
                'https://www.spglobal.com/spdji/en/';
      document.getElementById('d-idx').innerHTML=`<div style="color:var(--mu);font-size:13px;margin-bottom:8px">港股/海外指数请前往官网查询</div>
        <a href="${url}" target="_blank" class="btn" style="font-size:12px;text-decoration:none">🔗 前往官网</a>`;
    } else if(Object.keys(idx).length){
      document.getElementById('d-idx').innerHTML=
        `<div class="perf6">`+
        [['近1月',idx.ret_1m],['近3月',idx.ret_3m],['近6月',idx.ret_6m],['近1年',idx.ret_1y],
         ['最大回撤',idx.max_dd,true],['低点弹性',idx.bounce]].map(([l,v,neg])=>
          `<div class="mc" style="padding:8px 10px"><div class="ml">${l}</div><div class="mv" style="font-size:15px">${
            v!=null?(neg?`<span class="dn">${v.toFixed(2)}%</span>`:fp(v)):'—'}</div></div>`).join('')+`</div>`;
    } else {
      // 无历史数据：显示官网外链
      let site2='https://www.csindex.com.cn', siteName2='中证指数官网';
      if(idxN.startsWith('国证')||idxN.includes('国证')){site2='https://www.cnindex.com.cn';siteName2='国证指数官网';}
      document.getElementById('d-idx').innerHTML=`
        <div style="color:var(--mu);font-size:13px;margin-bottom:10px">历史行情请前往指数官网查询</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <a href="${site2}" target="_blank" class="btn" style="font-size:12px;text-decoration:none">🔗 ${siteName2}</a>
          ${idxCode?`<a href="https://www.csindex.com.cn/zh-CN/indices/index-detail/${idxCode}" target="_blank" class="btn" style="font-size:12px;text-decoration:none">📊 ${esc(idxN)}</a>`:''}
        </div>`;
    }
  }

  // 成分股
  if(data.is_hk){
    document.getElementById('d-cons').innerHTML='<div class="mu-t">港股指数成分股请访问 <a href="https://www.hsi.com.hk" target="_blank" style="color:var(--ph)">恒生指数公司官网</a></div>';
    document.getElementById('d-cons-l').textContent='';
  } else if(data.cons&&data.cons.top10&&data.cons.top10.length){
    rCons(data.cons);
  } else if(data.index_code){
    pollCons(data.index_code);
  }

  // 同指数排名
  document.getElementById('d-peers').innerHTML=pr.length?
    pr.map((pe,i)=>`<tr class="${i===0?'r1':''}${pe.manager?.includes('鹏华')?' ph-r':''}">
      <td style="font-family:var(--mono);color:var(--mu)">${i+1}</td>
      <td><b>${esc(pe.name)}</b>${pe.manager?.includes('鹏华')?' <span class="badge b-ph">鹏华</span>':''}</td>
      <td style="font-size:11px;color:var(--mu)">${esc(pe.manager||'—')}</td>
      <td style="font-family:var(--mono);font-weight:${i===0?500:400}">${pe.aum?pe.aum.toFixed(2):'—'}</td>
      <td>${fp(pe.premium)}</td><td>${fp(pe.pct_chg)}</td></tr>`).join(''):
    '<tr><td colspan="6" style="text-align:center;padding:18px;color:var(--mu)">刷新数据后可用</td></tr>';

  // 同类排名
  if(cat){loadCat(cat)}

  // 动态竞品
  if(dyn.length){
    document.getElementById('d-dyn-card').style.display='block';
    // 查询竞品的跟踪指数名称
    const peerIndexMap = {};
    (data.peers_rt||[]).forEach(pe => {
      // 通过产品代码在全局产品列表中找跟踪指数
      const prod = allP.find(p=>p.code===pe.code);
      peerIndexMap[pe.code] = prod ? (prod.index_name||'—') : '—';
    });
    document.getElementById('d-dyn').innerHTML=dyn.map(pe=>`<tr>
      <td><b>${esc(pe.name)}</b></td>
      <td style="font-size:11px;color:var(--mu)">${esc(pe.manager||'—')}</td>
      <td style="font-size:11px;color:var(--mu)">${esc(peerIndexMap[pe.code]||'—')}</td>
      <td style="font-family:var(--mono)">${pe.aum?pe.aum.toFixed(2):'—'}</td>
      <td>${fp(pe.premium)}</td><td>${fp(pe.pct_chg)}</td></tr>`).join('');
  }
}

function rCons(cons){
  const dateStr=cons.date||"";
  const eqNote=cons.equal_weight?" · 等权（官网暂无权重）":"";
  document.getElementById("d-cons-l").textContent="更新: "+dateStr+eqNote;
  const top10=cons.top10||[];
  if(!top10.length){document.getElementById("d-cons").innerHTML='<div class="mu-t">成分股数据暂无</div>';return;}
  function mkRow(s,num){
    const w=typeof s.weight==="number"?s.weight:0;
    const wStr=w>0?w.toFixed(3)+"%":"—";
    const bw=Math.min(w*8,100);
    return '<div class="bar-r">'+
      '<span style="color:var(--mu);font-family:var(--mono);font-size:11px;min-width:18px;text-align:right">'+num+'</span>'+
      '<span style="min-width:72px;font-size:12px;margin-left:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(s.name||"")+'</span>'+
      '<div class="bar-bg" style="flex:1"><div class="bar-f" style="width:'+bw+'%"></div></div>'+
      '<span style="font-family:var(--mono);font-size:11px;min-width:42px;text-align:right">'+wStr+'</span>'+
      '</div>';
  }
  const left=top10.slice(0,5),right=top10.slice(5,10);
  const leftHtml=left.map((s,i)=>mkRow(s,i+1)).join("");
  const rightHtml=right.map((s,i)=>mkRow(s,i+6)).join("");
  document.getElementById("d-cons").innerHTML=
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'+
    '<div>'+leftHtml+'</div>'+
    '<div>'+rightHtml+'</div>'+
    '</div>'+
    '<div style="margin-top:8px;font-size:11px;color:var(--mu);font-family:var(--mono)">共'+(cons.total||top10.length)+'只成分股</div>';
}

function pollCons(idxCode){
  if(consPoll)clearInterval(consPoll);
  let tries=0;
  consPoll=setInterval(async()=>{
    tries++;
    const data=await fetch('/api/cons/'+idxCode).then(r=>r.json()).catch(()=>({}));
    if(data.top10&&data.top10.length){clearInterval(consPoll);consPoll=null;rCons(data)}
    if(tries>15){clearInterval(consPoll);consPoll=null;
      document.getElementById('d-cons').innerHTML='<div class="mu-t">成分股接口暂时无法获取，请稍后再试</div>'}
  },3000);
}

async function loadCat(cat){
  try{
    const data=await fetch('/api/cat/'+encodeURIComponent(cat)).then(r=>r.json());
    const etfs=data.etfs||[];
    if(!etfs.length)return;
    document.getElementById('d-cat-card').style.display='block';
    document.getElementById('d-cat-cm').textContent=cat+' · 共'+data.total+'只';
    document.getElementById('d-cat').innerHTML=etfs.map((pe,i)=>`
      <tr class="${pe.is_ph?'ph-r':''}${i===0?' r1':''}">
        <td style="font-family:var(--mono);color:var(--mu)">${i+1}</td>
        <td><b>${esc(pe.name)}</b>${pe.is_ph?' <span class="badge b-ph">鹏华</span>':''}</td>
        <td style="font-size:11px;color:var(--mu)">${esc(pe.manager||'—')}</td>
        <td style="font-family:var(--mono);font-weight:${i===0?500:400}">${pe.aum?pe.aum.toFixed(2):'—'}</td>
        <td>${fp(pe.premium)}</td><td>${fp(pe.pct_chg)}</td>
      </tr>`).join('');
  }catch(e){}
}

async function doAI(){
  if(!cur){toast('请先选择产品');return}
  document.getElementById('btn-ai').disabled=true;
  document.getElementById('ai-out').innerHTML='<div class="mu-t"><span class="spin"></span>AI分析中，约15秒...</div>';
  try{
    const data=await fetch('/api/ai/'+cur,{method:'POST'}).then(r=>r.json());
    if(data.error&&!data.slogan_v1){
      document.getElementById('ai-out').innerHTML='<div style="color:var(--red);font-size:13px">'+esc(data.error)+'</div>';
    }else{
      const pts=(arr,c)=>(arr||[]).map((t,i)=>`<div class="pt-i"><span class="pt-n" style="background:${c}18;color:${c}">${i+1}</span><span>${esc(t)}</span></div>`).join('');
      document.getElementById('ai-out').innerHTML=
        (data.slogan_v1?`<div class="sl"><div class="sl-l">一句话卖点 V1</div><div class="sl-t">${esc(data.slogan_v1)}</div></div>`:'')+
        (data.slogan_v2?`<div class="sl" style="border-left-color:var(--blu)"><div class="sl-l">一句话卖点 V2</div><div class="sl-t">${esc(data.slogan_v2)}</div></div>`:'')+
        (data.product_points?.length?`<div style="margin-top:10px;font-size:10px;color:var(--mu);font-family:var(--mono);margin-bottom:4px">产品维度</div>${pts(data.product_points,'#3d2b8a')}`:'')+
        (data.index_points?.length?`<div style="margin-top:10px;font-size:10px;color:var(--mu);font-family:var(--mono);margin-bottom:4px">指数维度</div>${pts(data.index_points,'#1a4a7a')}`:'')+
        (data.peer_pitch?`<div style="margin-top:10px;padding:10px 13px;background:#f9f8f6;border-radius:4px;border-left:3px solid var(--grn)"><div style="font-size:10px;color:var(--mu);font-family:var(--mono);margin-bottom:4px">基转基话术</div><div style="font-size:13px;line-height:1.7">${esc(data.peer_pitch)}</div></div>`:'')+
        `<div style="margin-top:8px;font-size:10px;color:var(--mu);font-family:var(--mono)">⚡ Claude · ${new Date().toLocaleString('zh')}</div>`;
    }
  }catch(e){document.getElementById('ai-out').innerHTML='<div style="color:var(--red)">'+esc(e.message)+'</div>'}
  document.getElementById('btn-ai').disabled=false;
}

function cpAI(){
  const el=document.getElementById('ai-out');
  if(el.innerText.includes('点击')){toast('请先生成话术');return}
  navigator.clipboard.writeText(el.innerText).then(()=>toast('已复制 ✓'));
}

function expCsv(){
  if(!allP.length){toast('暂无数据');return}
  let csv='\ufeff产品名称,代码,板块,跟踪指数,涨跌(%),溢价率(%),规模(亿),成交额(亿),同业排名\n';
  allP.forEach(p=>csv+=`"${p.name}",${p.code},"${p.category||p.board||''}","${p.index_name||''}",${p.pct_chg||''},${p.premium||''},${p.aum||''},${p.turnover_yi||''},${p.peer_rank?p.peer_rank+'/'+p.peer_total:''}\n`);
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download='鹏华ETF_'+new Date().toLocaleDateString('zh')+'.csv';a.click();toast('已导出 ✓');
}

window.onload=async()=>{
  const s=await fetch('/api/status').then(r=>r.json());
  if(s.status==='ok'){setSt('ok','数据正常');document.getElementById('upd').textContent=(s.last_update||'')+(s.spot_date?' · 规模截至'+s.spot_date:'');await loadProds()}
  else{setSt('idle','待加载');doRefresh()}
};
</script></body></html>"""

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    print(f"\n  鹏华ETF AI营销平台 v8 → http://localhost:{port}")
    print(f"  密码: {PW}  AI: {'已配置' if AI_KEY else '未配置'}")
    if os.environ.get("PORT"):
        threading.Thread(target=fetch_all,daemon=True).start()
    app.run(host="0.0.0.0",port=port,debug=False)
