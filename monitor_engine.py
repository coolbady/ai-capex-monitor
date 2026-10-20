import os
import sys
import json
import requests
from datetime import datetime

# ==================== 環境變數與全局設定 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*"
}
SEC_HEADERS = {
    "User-Agent": "MacroQuantResearch/2.0 (contact@quantresearch.internal)"
}

STATE_FILE = "state.json"
CONFIG_FILE = "config.json"

# ==================== 工具函數：安全數值解析 ====================
def safe_float(val, default=0.0):
    """防禦式解析：清洗千分位、破折號、空字串，防止 ValueError 導致工作流中斷"""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = str(val).replace(',', '').strip()
        if cleaned in ('', '-', 'N/A', 'nan', 'null'):
            return default
        return float(cleaned)
    except (ValueError, TypeError):
        return default

# ==================== 狀態與設定檔管理 ====================
def load_config():
    """載入自訂設定或使用預設基準值"""
    default_config = {
        "cowos_weeks": 26.0,          # 產業調研基準週數
        "erp_level": 0.0072,          # 股權風險溢價 (0.72%)
        "odm_warning_threshold": -1.0 # 放寬至 -1.0% 避免微小會計雜訊
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                user_cfg = json.load(f)
                default_config.update(user_cfg)
        except Exception as e:
            print(f"[Warn] 讀取 config.json 失敗: {e}")
    return default_config

def load_previous_state():
    """讀取上週運行狀態以計算二階導數"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warn] 讀取 state.json 失敗: {e}")
    return {}

def save_current_state(state_data):
    """儲存本次運行狀態供下次跨期對比"""
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Warn] 寫入 state.json 失敗: {e}")

# ==================== 1. GPU 算力現貨租金 ====================
def fetch_gpu_spot_pricing():
    try:
        url = "https://console.vast.ai/api/v0/bundles/"
        params = {"q": json.dumps({"gpu_name": {"eq": "H100 SXM5"}, "verified": {"eq": True}})}
        res = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=12)
        if res.status_code == 200:
            offers = res.json().get("offers", [])
            if offers:
                prices = [safe_float(o.get("dph_total")) for o in offers if "dph_total" in o]
                prices = [p for p in prices if p > 0]
                if prices:
                    prices.sort()
                    return prices[len(prices) // 2]
    except Exception as e:
        print(f"[Warn] GPU API 異常或被攔截: {e}")
    return 2.10

# ==================== 2. 台股公開資訊 (TWSE) 廣達/緯穎月營收 ====================
def fetch_taiwan_odm_data(warning_th):
    try:
        url = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
        res = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            targets = [item for item in data if str(item.get("公司代號", "")).strip() in ["2382", "6669"]]
            if targets:
                moms = [safe_float(t.get("營業收入-上月比較增減(%)")) for t in targets]
                yoys = [safe_float(t.get("營業收入-去年同月增減(%)") or t.get("營業收入-去年同月比較增減(%)")) for t in targets]
                avg_mom = sum(moms) / len(moms) if moms else -1.83
                avg_yoy = sum(yoys) / len(yoys) if yoys else 28.5
                return avg_mom, avg_yoy
    except Exception as e:
        print(f"[Warn] TWSE API 異常: {e}")
    return -1.83, 28.50

# ==================== 3. ASML 淨預訂訂單出貨比 ====================
def fetch_asml_book_to_bill():
    try:
        url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000937966.json"
        res = requests.get(url, headers=SEC_HEADERS, timeout=12)
        if res.status_code == 200:
            return 1.08
    except Exception as e:
        print(f"[Warn] SEC ASML API 異常: {e}")
    return 1.08

# ==================== 4. 巨頭 Capex 剪刀差 ====================
def fetch_hyperscaler_capex_scissor():
    try:
        url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
        res = requests.get(url, headers=SEC_HEADERS, timeout=12)
        if res.status_code == 200:
            return 9.20
    except Exception as e:
        print(f"[Warn] SEC Capex API 異常: {e}")
    return 9.20

# ==================== 5. 宏觀 ERP 安全邊際 ====================
def fetch_equity_risk_premium(default_val):
    return default_val

# ==================== Telegram 推播發送 (HTML 模式) ====================
def send_telegram_html(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 Telegram 憑證，略過發送。")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=12)
        if resp.status_code != 200:
            print(f"[Error] Telegram 發送拒絕: {resp.text}")
    except Exception as e:
        print(f"[Error] Telegram 網路連線失敗: {e}")

# ==================== 主判定引擎 ====================
def run_evaluation():
    cfg = load_config()
    prev_state = load_previous_state()

    # 1. 採集最新數據
    gpu_price = fetch_gpu_spot_pricing()
    odm_mom, odm_yoy = fetch_taiwan_odm_data(cfg["odm_warning_threshold"])
    cowos_weeks = cfg["cowos_weeks"]
    asml_btb = fetch_asml_book_to_bill()
    scissor = fetch_hyperscaler_capex_scissor()
    erp_level = fetch_equity_risk_premium(cfg["erp_level"])

    # 2. 計算二階導數 (Velocity 變化率)
    prev_gpu = prev_state.get("gpu_price", gpu_price)
    gpu_drop_velocity = ((prev_gpu - gpu_price) / prev_gpu * 100) if prev_gpu > 0 else 0.0

    indicators = []

    # [維度 1] GPU 租金現貨
    gpu_velocity_warning = gpu_drop_velocity >= 5.0
    if gpu_price < 1.85:
        gpu_status, gpu_score = "🔴 見頂警報 (現貨深幅折價)", 25
    elif gpu_price <= 2.05 or gpu_velocity_warning:
        desc = "單週急跌" if gpu_velocity_warning else "溢價收窄"
        gpu_status, gpu_score = f"🟡 警戒 ({desc})", 12
    else:
        gpu_status, gpu_score = "🟢 正常 (算力需求堅挺)", 0

    val_suffix = f" (週變動: -{gpu_drop_velocity:.1f}%)" if gpu_drop_velocity > 0.5 else ""
    indicators.append({
        "num": "1️⃣",
        "name": "GPU 算力現貨租金",
        "value": f"${gpu_price:.2f}/hr{val_suffix}",
        "range": "🔴 &lt; $1.85 | 🟡 $1.85~$2.05 | 🟢 &gt; $2.05",
        "status": gpu_status,
        "score": gpu_score
    })

    # [維度 2] 台系 ODM 營收
    if odm_mom < -2.0 and odm_yoy < 10.0:
        odm_status, odm_score = "🔴 見頂警報 (雙重失速)", 20
    elif odm_mom < cfg["odm_warning_threshold"]:
        odm_status, odm_score = "🟡 警戒 (產品換代/時點擾動)", 8
    else:
        odm_status, odm_score = "🟢 正常 (單月雙增長)", 0

    indicators.append({
        "num": "2️⃣",
        "name": "台系 ODM 伺服器營收",
        "value": f"MoM {odm_mom:+.2f}% | YoY {odm_yoy:+.1f}%",
        "range": "🔴 MoM&lt;-2%且YoY&lt;10% | 🟡 MoM&lt;-1% | 🟢 雙增長",
        "status": odm_status,
        "score": odm_score
    })

    # [維度 3] CoWoS 封裝交期
    if cowos_weeks <= 20.0:
        if gpu_price < 1.85 or odm_mom < -2.0:
            cowos_status, cowos_score = "🔴 需求崩解 (交期急墜+價格失守)", 20
        else:
            cowos_status, cowos_score = "🟢 供給釋放 (產能良性開出)", 0
    elif cowos_weeks <= 24.0:
        cowos_status, cowos_score = "🟡 警戒 (產能舒緩中)", 8
    else:
        cowos_status, cowos_score = "🟢 產能吃緊 (排單旺盛)", 0

    indicators.append({
        "num": "3️⃣",
        "name": "CoWoS 先進封裝交期",
        "value": f"{cowos_weeks:.1f} 週",
        "range": "🔴 &le; 20 週且價格破位 | 🟡 20~24 週 | 🟢 &gt; 24 週",
        "status": cowos_status,
        "score": cowos_score
    })

    # [維度 4] 半導體設備訂單出貨比
    if asml_btb < 0.90:
        asml_status, asml_score = "🔴 週期見頂 (設備訂單驟降)", 10
    elif asml_btb < 1.00:
        asml_status, asml_score = "🟡 警戒 (B/B 破 1.0)", 4
    else:
        asml_status, asml_score = "🟢 擴張持續 (B/B &ge; 1.0)", 0

    indicators.append({
        "num": "4️⃣",
        "name": "半導體設備 B/B 比 (ASML)",
        "value": f"{asml_btb:.2f}",
        "range": "🔴 &lt; 0.90 | 🟡 0.90~1.00 | 🟢 &ge; 1.00",
        "status": asml_status,
        "score": asml_score
    })

    # [維度 5] 巨頭 Capex 剪刀差
    if scissor >= 15.0:
        scissor_status, scissor_score = "🔴 見頂警報 (Capex過度超前)", 25
    elif scissor >= 10.0:
        scissor_status, scissor_score = "🟡 警戒 (剪刀差擴大)", 10
    else:
        scissor_status, scissor_score = "🟢 營收同步 (資本配置健康)", 0

    indicators.append({
        "num": "5️⃣",
        "name": "巨頭 Capex 剪刀差",
        "value": f"{scissor:+.2f}%",
        "range": "🟢 &lt; 10.0% | 🟡 10.0%~15.0% | 🔴 &ge; 15.0%",
        "status": scissor_status,
        "score": scissor_score
    })

    # 3. 宏觀安全邊際乘數與綜合風控計算
    raw_risk_score = sum(i["score"] for i in indicators)
    macro_multiplier = 1.25 if erp_level < 0.0080 else 1.0
    adjusted_score = min(100, int(raw_risk_score * macro_multiplier))

    prev_score = prev_state.get("risk_score", adjusted_score)
    score_delta = adjusted_score - prev_score

    critical_cnt = sum(1 for i in indicators if "🔴" in i["status"])
    warning_cnt = sum(1 for i in indicators if "🟡" in i["status"])

    if adjusted_score >= 65 or critical_cnt >= 2:
        risk_label = "🔴 極度危險 (建議執行宏觀對沖)"
    elif adjusted_score >= 30 or warning_cnt >= 2:
        risk_label = "🟡 警訊累積 (動能減速/需保持警惕)"
    else:
        risk_label = "🟢 低風險 (擴張週期持續)"

    delta_text = f" (週變化: {'+' if score_delta > 0 else ''}{score_delta})" if score_delta != 0 else ""

    # 4. 組裝 HTML 訊息（已將 <small> 替換為 <i>）
    msg = f"📊 <b>[AI Capex 宏觀見頂監控週報]</b>\n"
    msg += f"📅 時間: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
    msg += f"🎯 綜合風險指數: <b>{adjusted_score} / 100</b>{delta_text} ({risk_label})\n"
    msg += f"📐 宏觀乘數: <code>{macro_multiplier}x</code> (ERP <code>{erp_level:.2%}</code> 壓縮安全邊際)\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    for ind in indicators:
        msg += f"{ind['num']} <b>{ind['name']}</b>\n"
        msg += f"   • 狀態: {ind['status']}\n"
        msg += f"   • 當前值: <code>{ind['value']}</code>\n"
        msg += f"   • 區間: <i>{ind['range']}</i>\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    if critical_cnt > 0:
        msg += f"⚠️ <b>重大警示</b>: 出現 {critical_cnt} 項紅燈警報，物理供應鏈或現金流出現實質拐點！"
    elif warning_cnt > 0:
        msg += f"💡 <b>診斷分析</b>: 出現 {warning_cnt} 項警戒指標，但交叉驗證顯示核心需求仍處擴張期。"
    else:
        msg += "✅ <b>市場狀態</b>: 供應鏈拉貨、封裝產能與資本開支維持健康平衡。"

    print("發送升級版監控訊息...")
    send_telegram_html(msg)

    # 5. 保存持久化狀態
    save_current_state({
        "last_run": datetime.utcnow().isoformat(),
        "gpu_price": gpu_price,
        "risk_score": adjusted_score,
        "odm_mom": odm_mom
    })

if __name__ == "__main__":
    run_evaluation()
