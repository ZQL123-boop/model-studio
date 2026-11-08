"""文件工具：让模型能读本地文件（只读，带沙箱）。

## 为什么只做只读

写文件/删文件的风险不对等：
- 读错了：模型答得不对，你知道
- 写错了：代码被覆盖、配置被改，可能没法恢复

所以第一版只给 `read_file` / `list_dir` / `find_files`。
要写操作的话必须配人工确认（前端弹窗 + 用户点同意），那是另一个设计。

## 沙箱

所有路径 `resolve()` 之后必须是 `ALLOW_ROOT` 的子路径。
`../../etc/passwd` 这种会被 `resolve()` 折叠成绝对路径，然后被 `is_relative_to` 拦住。

## 还拦了敏感文件

`.env`、私钥、`credentials` 这类即使在工作区内也不给读——
模型读到 API key 之后可能写进回答里，那就泄漏了。
"""
from __future__ import annotations

import contextvars
import fnmatch
import json
import os
import re
import time
from pathlib import Path

# 默认沙箱：用户的项目目录。想放开就改这个，或者设环境变量。
# 读范围默认放开到整机。想收回就设 MS_FILE_ROOT（比如 ~/Projects）。
# 敏感文件黑名单仍然生效，即使读根是 /。
ALLOW_ROOT = Path(os.environ.get("MS_FILE_ROOT", "/")).resolve()

# 从 / 遍历时必须跳过：系统卷、设备、挂载点。不跳过 find_files 会扫几十万文件。
SKIP_DIRS_ABS = {
    "/System", "/dev", "/Volumes", "/cores", "/Library", "/usr",
    "/bin", "/sbin", "/opt", "/Applications", "/Network", "/.Trashes",
    # 只跳 /private 下真正没用的部分。/private 整个跳会连 /etc/hosts 都读不到
    # （macOS 的 /etc 是 /private/etc 的软链，resolve 之后落在 /private）
    "/private/var", "/private/tmp",
}

# 写操作单独一个根，默认不碰真实项目。
# 想让它改真项目就把 MS_WRITE_ROOT 指过去——但那要你自己承担风险。
WRITE_ROOT = Path(os.environ.get("MS_WRITE_ROOT",
                                 str(Path.home() / "Projects" / "sandbox"))).resolve()
BACKUP_DIR = WRITE_ROOT / ".ms_backup"

# 额外允许写的目录。桌面是用户自己放东西的地方，风险低于项目代码，
# 而且"生成个文件放桌面"是最常见的要求，不支持的话很难用。
EXTRA_WRITE_ROOTS = [
    Path.home() / "Desktop",
    Path.home() / "Projects",      # 所有项目都能改（有自动备份）
]


def _write_roots() -> list[Path]:
    return [WRITE_ROOT] + [p.resolve() for p in EXTRA_WRITE_ROOTS if p.exists()]

MAX_READ_BYTES = 100 * 1024
WRITE_PROTECT = False     # 由 /api/tools/write-protect 切换。开则覆盖已存在文件会拒绝        # 单文件最多读 100 KB
MAX_LIST = 200                     # 目录最多列 200 项

# 即使在沙箱内也不给读的文件
DENY_NAMES = {
    ".env", ".env.local", ".env.production",
    "id_rsa", "id_ed25519", "id_dsa",
    "credentials", "credentials.json", "secrets.yml", "secrets.yaml",
    ".netrc", ".pgpass",
}
DENY_DIRS = {".ssh", ".gnupg", ".aws", ".config/gcloud"}
DENY_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

# 文本类文件才读，二进制直接拒（不然会往 prompt 里塞乱码）
TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".sh", ".bash", ".zsh", ".css", ".html", ".xml",
    ".sql", ".java", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".rb", ".php",
    ".swift", ".kt", ".lua", ".r", ".m", ".log", ".csv", ".gitignore", "",
}


def _denied(p: Path) -> str | None:
    """返回拒绝原因，None 表示可以读。"""
    if p.name in DENY_NAMES:
        return f"{p.name} 是敏感文件，不读"
    if p.suffix.lower() in DENY_SUFFIXES:
        return f"{p.suffix} 是密钥类文件，不读"
    for d in DENY_DIRS:
        if f"/{d}/" in str(p) or str(p).endswith(f"/{d}"):
            return f"{d} 目录不允许访问"
    for d in SKIP_DIRS_ABS:
        if str(p) == d or str(p).startswith(d + "/"):
            return f"{d} 是系统目录，不读"
    return None


def _fix_linux_path(raw: str) -> str:
    """把模型爱写的 Linux 路径纠正成本机实际路径。

    模型的训练语料里大量 /home/user、/root，它生成路径时先验就是这个。
    直接拒绝的话用户会看到"路径不存在"，其实只差一个前缀。
    """
    home = str(Path.home())
    for wrong in ("/home/user", "/home/ubuntu", "/root", "/home/"):
        if raw == wrong or raw.startswith(wrong + "/"):
            return home + raw[len(wrong):]
    return raw


def _resolve(path: str) -> Path:
    """把用户/模型给的路径解析成绝对路径，并校验沙箱。"""
    raw = _fix_linux_path((path or "").strip())
    if not raw:
        raise ValueError("路径不能为空")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        # 相对路径基准用 ~，不能用 ALLOW_ROOT——读根是 / 的时候，
        # "model-studio" 会被拼成 /model-studio，全错。
        # 另外模型经常写 "sandbox/x.md"，去掉这层前缀再拼，避免 sandbox/sandbox。
        if p.parts and p.parts[0] == WRITE_ROOT.name and len(p.parts) > 1:
            cand = WRITE_ROOT.parent / p
            if cand.exists():
                p = cand
                return p
        a = Path.home() / p
        b = WRITE_ROOT / p
        p = a if a.exists() else (b if b.exists() else a)
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"路径无效：{e}")
    if not p.is_relative_to(ALLOW_ROOT):
        raise ValueError(f"越界：只允许访问 {ALLOW_ROOT} 下的文件")
    return p




# ---------------------------------------------------------------- 汇率
def get_exchange_rate(base: str = "CNY", targets: str = "USD,EUR,JPY,HKD,GBP") -> str:
    """查实时汇率（open.er-api.com，免费无需 key）。

    base 是基准货币（默认人民币），targets 是要看的货币（逗号分隔）。
    返回 1 base = X target 的形式，带更新时间。
    """
    import httpx

    b = (base or "CNY").strip().upper()[:3]
    want = [x.strip().upper() for x in (targets or "").split(",") if x.strip()][:8]

    try:
        with httpx.Client(trust_env=False, timeout=20) as c:
            r = c.get(f"https://open.er-api.com/v6/latest/{b}")
    except Exception as e:                                # noqa: BLE001
        return f"[错误] 汇率服务连不上：{type(e).__name__}"

    if r.status_code != 200:
        return f"[错误] 汇率服务返回 HTTP {r.status_code}"

    try:
        d = r.json()
        rates = d.get("rates") or {}
        upd = d.get("time_last_update_utc", "")
    except ValueError:
        return "[错误] 汇率数据解析失败"

    if not rates:
        return f"[错误] 没有 {b} 的汇率数据（试试 USD/CNY/EUR 这种三字母代码）"

    out = [f"【汇率】基准 {b}  更新时间 {upd}"]
    for k in want:
        if k in rates:
            out.append(f"  1 {k} = {1 / rates[k]:.4f} {b}" if rates[k] else "")
            out.append(f"  1 {b} = {rates[k]} {k}")
    if len(out) <= 1:
        keys = list(rates)[:8]
        out.append(f"  {b} 可换：{', '.join(keys)}（用 targets 指定）")
    out.append("")
    out.append("**直接把这些数字组织成回答，不要甩链接。**")
    return "\n".join(x for x in out if x)


# ---------------------------------------------------------------- 天气
_CITY_PINYIN = {
    "中山": "zhongshan", "深圳": "shenzhen", "广州": "guangzhou", "珠海": "zhuhai",
    "东莞": "dongguan", "佛山": "foshan", "惠州": "huizhou", "江门": "jiangmen",
    "北京": "beijing", "上海": "shanghai", "杭州": "hangzhou", "成都": "chengdu",
    "武汉": "wuhan", "西安": "xian", "南京": "nanjing", "重庆": "chongqing",
    "香港": "hongkong", "澳门": "macau", "台北": "taipei",
}


_WMO_DESC = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}

# 中国天气网的城市编码（已逐个核对 cityInfo.city），覆盖最常用的一批
_CITY_CODE = {
    "深圳": "101280601", "中山": "101281701", "北京": "101010100", "上海": "101020100",
    "广州": "101280101", "成都": "101270101", "杭州": "101210101", "武汉": "101200101",
    "西安": "101110101", "南京": "101190101", "珠海": "101280701", "东莞": "101281601",
    "天津": "101030100", "重庆": "101040100", "苏州": "101190401", "厦门": "101230201",
    "长沙": "101250101", "青岛": "101120201",
}

_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
_CITY_CODE_CACHE: dict[str, str] = {}
_WX_TTL = 600          # 10 分钟内同城不重复请求


def _wx_text(url: str, timeout: float = 8, tries: int = 2,
             headers: dict | None = None) -> str:
    """带重试的 GET 文本。天气源都不稳：wttr 会整站连不上、open-meteo 会 503。"""
    import httpx

    last = "unknown"
    hd = {"User-Agent": "Mozilla/5.0"} | (headers or {})
    for i in range(max(1, tries)):
        try:
            with httpx.Client(trust_env=False, timeout=timeout, headers=hd) as c:
                r = c.get(url)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:                           # noqa: BLE001
            last = type(e).__name__
        time.sleep(0.5 * (i + 1))
    raise RuntimeError(last)


def _city_code(q: str) -> str | None:
    """城市名 → 中国天气网编码。先查内置表，再查它的搜索接口。"""
    if q in _CITY_CODE:
        return _CITY_CODE[q]
    if q in _CITY_CODE_CACHE:
        return _CITY_CODE_CACHE[q]
    from urllib.parse import quote

    try:
        raw = _wx_text("http://toy1.weather.com.cn/search?cityname=" + quote(q),
                       headers={"Referer": "http://www.weather.com.cn/"})
        items = json.loads(raw.strip().lstrip("(").rstrip(")") or "[]")
        for it in items:
            parts = str(it.get("ref", "")).split("~")
            if len(parts) > 2 and parts[2].rstrip("市") == q.rstrip("市"):
                _CITY_CODE_CACHE[q] = parts[0]
                return parts[0]
        if items:
            code = str(items[0].get("ref", "")).split("~")[0]
            if code:
                _CITY_CODE_CACHE[q] = code
                return code
    except Exception:                                    # noqa: BLE001
        pass
    return None


_MET_UA = "model-studio/1.0 (local weather tool)"
_MET_SYM = {
    "clearsky": "晴", "fair": "晴间多云", "partlycloudy": "多云", "cloudy": "阴",
    "fog": "雾", "lightrain": "小雨", "rain": "雨", "heavyrain": "大雨",
    "lightrainshowers": "小阵雨", "rainshowers": "阵雨", "heavyrainshowers": "强阵雨",
    "lightsleet": "小雨夹雪", "sleet": "雨夹雪", "lightsnow": "小雪", "snow": "雪",
    "heavysnow": "大雪", "snowshowers": "阵雪", "thunderstorm": "雷阵雨",
}


# open-meteo 的地理编码对中文支持很差（「纽约」0 结果，「New York」正常），
# Nominatim 这台机器直接连不上（status 000）。所以常用外国城市走内置对照表。
_CITY_EN = {
    "纽约": "New York", "伦敦": "London", "东京": "Tokyo", "巴黎": "Paris",
    "柏林": "Berlin", "莫斯科": "Moscow", "首尔": "Seoul", "新加坡": "Singapore",
    "洛杉矶": "Los Angeles", "旧金山": "San Francisco", "西雅图": "Seattle",
    "芝加哥": "Chicago", "波士顿": "Boston", "多伦多": "Toronto", "温哥华": "Vancouver",
    "悉尼": "Sydney", "墨尔本": "Melbourne", "迪拜": "Dubai", "曼谷": "Bangkok",
    "吉隆坡": "Kuala Lumpur", "阿姆斯特丹": "Amsterdam", "苏黎世": "Zurich",
    "香港": "Hong Kong", "台北": "Taipei", "澳门": "Macau",
}


def _geocode(q: str):
    """城市名 → (lat, lon, 显示名)。先原样查，查不到再翻成英文查一次。"""
    from urllib.parse import quote

    base = q.rstrip("市")
    cands = [q]
    en = _CITY_EN.get(base) or _CITY_EN.get(q)
    if en and en != q:
        cands.append(en)

    for name in cands:
        try:
            d = json.loads(_wx_text(
                "https://geocoding-api.open-meteo.com/v1/search?name=" + quote(name)
                + "&count=1&language=zh&format=json", timeout=8, tries=2))
            hits = d.get("results") or []
            if hits:
                g = hits[0]
                return float(g["latitude"]), float(g["longitude"]), g.get("name") or name
        except Exception:                                # noqa: BLE001
            continue
    return None


def _weather_metno(q: str, lat: float, lon: float) -> str:
    """挪威气象局开源接口，全球覆盖、无需 key——专门补 wttr/open-meteo 挂掉时的缺口。"""
    from collections import defaultdict

    d = json.loads(_wx_text(
        f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}",
        timeout=12, tries=2, headers={"User-Agent": _MET_UA}))
    ts = d["properties"]["timeseries"]
    cur = ts[0]["data"]["instant"]["details"]
    sym0 = ""
    for blk in ("next_1_hours", "next_6_hours"):
        s = ts[0]["data"].get(blk, {}).get("summary", {}).get("symbol_code", "")
        if s:
            sym0 = _MET_SYM.get(s.split("_")[0], s)
            break

    agg = defaultdict(lambda: {"min": 999.0, "max": -999.0, "sym": ""})
    for row in ts[1:]:
        day = row["time"][:10]
        det = row["data"]["instant"]["details"]
        temp = det.get("air_temperature")
        if temp is None:
            continue
        a = agg[day]
        a["min"] = min(a["min"], temp)
        a["max"] = max(a["max"], temp)
        if not a["sym"]:
            for blk in ("next_6_hours", "next_1_hours"):
                sc = row["data"].get(blk, {}).get("summary", {}).get("symbol_code", "")
                if sc:
                    a["sym"] = _MET_SYM.get(sc.split("_")[0], sc)
                    break

    days = [f"| {day} | {a['max']:.0f}°C | {a['min']:.0f}°C | {a['sym'] or '—'} |"
            for day, a in sorted(agg.items())[:3]]
    line = (f"{cur.get('air_temperature', '?')}°C，{sym0 or '—'}，"
            f"湿度 {cur.get('relative_humidity', '?')}%，"
            f"风 {cur.get('wind_speed', '?')} m/s")
    return _fmt_weather(q, line, days)


def _fmt_weather(q: str, cur_line: str, days: list[str], note: str = "") -> str:
    out = [f"【{q} 当前天气】", cur_line, ""]
    if days:
        out += ["【未来几天】", "| 日期 | 最高 | 最低 | 天气 |", "|---|---|---|---|"] + days
        out.append("")
    if note:
        out += [note, ""]
    out.append("**直接把这些数字组织成回答，不要再甩链接。**")
    return "\n".join(out)


def get_weather(city: str) -> str:
    """查城市天气，四路降级：itboy(中国天气网) → weather.com.cn → wttr → open-meteo。

    为什么要这么绕：实测 wttr.in 会整站 ConnectError，open-meteo 会持续 503。
    只剩它们时模型会退到「搜索 + 抓网页」，抓到新闻页 → 答「没找到具体天气」。
    国内可用的两个源（itboy / weather.com.cn）反而稳定，所以放到最前面。
    """
    from urllib.parse import quote

    q = (city or "").strip()
    if not q:
        return "[错误] 城市不能为空"

    hit = _WEATHER_CACHE.get(q)
    if hit and time.time() - hit[0] < _WX_TTL:
        return hit[1]

    errors: list[str] = []
    code = _city_code(q)

    # ---- 源 1：itboy（中国天气网数据，有当前 + 5 天预报）----
    if code:
        try:
            d = json.loads(_wx_text(f"http://t.weather.itboy.net/api/weather/city/{code}",
                                    timeout=8, tries=2))
            info = d.get("cityInfo") or {}
            data = d.get("data") or {}
            if str(data.get("wendu", "")).strip():
                days = []
                for f in (data.get("forecast") or [])[:3]:
                    hi = str(f.get("high", "")).replace("高温", "").strip()
                    lo = str(f.get("low", "")).replace("低温", "").strip()
                    days.append(f"| {f.get('ymd', f.get('date', ''))} | {hi} | {lo} | "
                                f"{f.get('type', '')} |")
                cur_type = ((data.get("forecast") or [{}])[0].get("type")
                            or data.get("type") or "")
                parts_l = [f"{data.get('wendu')}°C", cur_type,
                           f"湿度 {data.get('shidu', '?')}",
                           f"空气质量 {data.get('quality', '?')}"]
                line = "，".join(x for x in parts_l if x)
                text = _fmt_weather(q, line, days)
                _WEATHER_CACHE[q] = (time.time(), text)
                return text
            errors.append("itboy: 数据为空")
        except Exception as e:                           # noqa: BLE001
            errors.append(f"itboy: {type(e).__name__}")

    # ---- 源 2：weather.com.cn 实时（只有当前，没有预报）----
    if code:
        try:
            raw = _wx_text(f"http://d1.weather.com.cn/sk_2d/{code}.html",
                           headers={"Referer": "http://www.weather.com.cn/"})
            js = raw[raw.index("{"):raw.rindex("}") + 1]
            d = json.loads(js)
            line = (f"{d.get('temp')}°C，{d.get('weather', '')}，"
                    f"湿度 {d.get('SD', '?')}，{d.get('WD', '')} {d.get('WS', '')}")
            text = _fmt_weather(q, line, [], note="（该源没有未来预报，只有实时数据）")
            _WEATHER_CACHE[q] = (time.time(), text)
            return text
        except Exception as e:                           # noqa: BLE001
            errors.append(f"weather.com.cn: {type(e).__name__}")

    # ---- 源 3：met.no（挪威气象局，全球覆盖、无需 key）----
    try:
        geo = _geocode(q)
        if not geo:
            raise RuntimeError("地名没匹配上")
        text = _weather_metno(q, geo[0], geo[1])
        _WEATHER_CACHE[q] = (time.time(), text)
        return text
    except Exception as e:                               # noqa: BLE001
        errors.append(f"met.no: {type(e).__name__}")

    # ---- 源 4：wttr.in（英文源，按拼音）----
    key = _CITY_PINYIN.get(q, q)
    try:
        d = json.loads(_wx_text(f"https://wttr.in/{quote(key)}?format=j1",
                                timeout=8, tries=2))
        cur = d["current_condition"][0]
        days = []
        for day in (d.get("weather") or [])[:3]:
            desc = (day.get("hourly") or [{}])[0].get("weatherDesc", [{}])[0].get("value", "")
            days.append(f"| {day['date']} | {day['maxtempC']}°C | {day['mintempC']}°C | {desc} |")
        line = (f"{cur['temp_C']}°C，{cur['weatherDesc'][0]['value']}，"
                f"体感 {cur['FeelsLikeC']}°C，湿度 {cur['humidity']}%，"
                f"风 {cur['windspeedKmph']} km/h {cur.get('winddir16Point', '')}")
        text = _fmt_weather(q, line, days)
        _WEATHER_CACHE[q] = (time.time(), text)
        return text
    except Exception as e:                               # noqa: BLE001
        errors.append(f"wttr: {type(e).__name__}")

    # ---- 源 5：open-meteo（地理编码 + 预报）----
    try:
        geo = json.loads(_wx_text(
            "https://geocoding-api.open-meteo.com/v1/search?name=" + quote(q)
            + "&count=1&language=zh&format=json", timeout=10, tries=2))
        hits = geo.get("results") or []
        if not hits:
            raise RuntimeError("地名没匹配上")
        g = hits[0]
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={g['latitude']}"
               f"&longitude={g['longitude']}"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
               "weather_code,wind_speed_10m"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min"
               "&timezone=Asia%2FShanghai&forecast_days=4")
        d = json.loads(_wx_text(url, timeout=12, tries=3))
        c = d["current"]
        code_w = int(c.get("weather_code", -1))
        daily = d.get("daily") or {}
        days = []
        for i, date in enumerate((daily.get("time") or [])[:4]):
            dc = int((daily.get("weather_code") or [0])[i])
            days.append(f"| {date} | {daily['temperature_2m_max'][i]}°C | "
                        f"{daily['temperature_2m_min'][i]}°C | {_WMO_DESC.get(dc, '未知')} |")
        line = (f"{c['temperature_2m']}°C，{_WMO_DESC.get(code_w, '未知')}，"
                f"体感 {c['apparent_temperature']}°C，湿度 {c['relative_humidity_2m']}%，"
                f"风 {c['wind_speed_10m']} km/h")
        text = _fmt_weather(q, line, days)
        _WEATHER_CACHE[q] = (time.time(), text)
        return text
    except Exception as e:                               # noqa: BLE001
        errors.append(f"open-meteo: {type(e).__name__}")

    return ("[错误] 天气服务暂时不可用（" + "；".join(errors) + "）。"
            "请如实告诉用户查询失败、稍后再试，**绝对不要编造温度**，"
            "也不要拿搜索到的新闻页当天气数据。")


# ---------------------------------------------------------------- 图像生成
IMG_DIR = None          # 延迟到首次调用时定（依赖 WRITE_ROOT）

# 「再生成一张」的记忆。实测存档 002f5502：用户说「再生成一张」，
# 本地 30B 把这句话原样塞进 prompt，拿去生了一张没有意义的图。
_LAST_IMG_PROMPT: str | None = None

# Pollinations 按 IP 短时限流：实测连续第 2 次请求就 402，隔几秒自动恢复。
# 这类是**临时**故障，必须在工具内部退避重试消化掉。把
# 「[错误] HTTP 402」原样甩给模型，30B 会当成「工具真做不到」，
# 转头去 web_search / fetch_url 找图，最后编一句「服务暂时不可用」
# （存档 002f5502：9 轮 / 158.9s，一张图没出）。
_IMG_RETRY_STATUS = {402, 408, 425, 429, 500, 502, 503, 504}
_IMG_BACKOFF = (3, 6, 12)         # 3 次退避 → 最多 4 次尝试，覆盖 ~21s 抖动
_IMG_TIMEOUT = 45                 # 单次请求超时（实测出图 1~8s）
_IMG_ENDPOINTS = (
    "https://image.pollinations.ai/prompt/{q}",
    "https://pollinations.ai/p/{q}",          # 备用入口，主节点抽风时换
)


def _is_repeat_prompt(q: str) -> bool:
    """「再生成一张」这类指代，不是图像描述。"""
    s = (q or "").strip()
    if not s or len(s) > 24:
        return False
    if re.search(r"another|one more|same|again|repeat", s, re.I):
        return True
    return bool(re.search(r"(再|又|重新|多).{0,4}(张|个|幅|图|次|来|生成|画)", s))


def generate_image(prompt: str, width: int = 512, height: int = 512) -> str:
    """用在线服务（Pollinations，免费无需 key）生成图像，存到 sandbox/generated/。

    为什么用在线而不是本地 MLX：本地 SD 模型要 4~6 GB，24 GB 内存跑完 30B 就没空间了。
    在线 1~8 秒出图，代价是要联网 + 会被短时限流（已在工具内部退避重试）。
    """
    import random as _rand
    from urllib.parse import quote

    import httpx

    global _LAST_IMG_PROMPT

    q = (prompt or "").strip()
    if not q:
        return "[错误] prompt 不能为空"
    repeat = _is_repeat_prompt(q)
    if repeat:
        if not _LAST_IMG_PROMPT:
            return ("[提示] 我这边没有上一张图的描述。请把要画的内容写进 prompt "
                    "再调用一次 generate_image（例如「一只橘猫」）。")
        q = _LAST_IMG_PROMPT          # 沿用上一张的描述，别拿「再生成一张」当 prompt

    w = max(256, min(int(width or 512), 1024))
    h = max(256, min(int(height or 512), 1024))
    out_dir = WRITE_ROOT / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    params = {"width": w, "height": h, "nologo": "true"}

    last_err = ""
    for attempt in range(len(_IMG_BACKOFF) + 1):
        if attempt:
            time.sleep(_IMG_BACKOFF[attempt - 1])
        # 每次换 seed：既避开「坏节点 + 旧缓存」，也保证「再生成一张」
        # 不会因为 prompt 相同又拿回上一张图。
        prm = {**params, "seed": _rand.randint(1, 2_000_000_000)}
        # 前两次打主入口，第 3 次起切备用入口
        api = _IMG_ENDPOINTS[0 if attempt < 2 else 1].format(q=quote(q))
        try:
            with httpx.Client(trust_env=False, timeout=_IMG_TIMEOUT,
                              follow_redirects=True) as c:
                r = c.get(api, params=prm)
        except Exception as e:                            # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue
        if r.status_code == 200 and len(r.content) >= 5000:
            # 毫秒级文件名：连续两次生成（「再生成一张」）不会互相覆盖
            fn = out_dir / f"img_{int(time.time() * 1000)}.jpg"
            fn.write_bytes(r.content)
            _LAST_IMG_PROMPT = q
            # 返回可访问的 URL（/generated/xxx.jpg，由 main.py 的静态路由提供），
            # 不是本地绝对路径——前端用不了绝对路径。
            url = f"/generated/{fn.name}"
            return (f"[已生成] {url}\n"
                    f"尺寸 {w}x{h}，{len(r.content) // 1024} KB\n"
                    f"提示词：{q}\n"
                    f"**回答里直接用这行展示图片**（前端会渲染）：\n![{q}]({url})")
        if r.status_code == 200:
            last_err = "HTTP 200 但返回内容太小"
            continue
        if r.status_code not in _IMG_RETRY_STATUS:
            return f"[错误] 图像服务返回 HTTP {r.status_code}（非临时故障，不重试）"
        last_err = f"HTTP {r.status_code}"

    # 重试耗尽：明确告诉模型这是限流、别换工具、别编结论。
    # 只说「服务不可用」会被复述成最终答案，并触发 persona 里「找信息→联网」的链路。
    return ("[临时故障] 图像服务被短时限流，已自动退避重试 4 次仍失败"
            f"（最后一次：{last_err}）。\n"
            "这是**临时限流**，不是任务失败，也不是你调错了工具：\n"
            "1. 不要改用 web_search / fetch_url —— 它们拿不到图片文件；\n"
            "2. 不要向用户编造结果或说「服务永久不可用」；\n"
            "3. 直接**再调用一次 generate_image**（同样参数）重试，或如实告诉用户"
            "「图片服务暂时被限流，等几秒再让我生成一次」。")


# ---------------------------------------------------------------- 知识库
RAG_SEARCH_URL = "http://127.0.0.1:8601/search"


def knowledge_search(query: str, top_k: int = 5) -> str:
    """查本地知识库（algo-rag，OI Wiki 语料）。

    为什么单独做一个工具：知识库是精确的、离线的、0.2 秒；
    联网搜索是模糊的、要几秒、还可能搜偏。所以**先查知识库，没有再联网**。
    """
    import httpx

    q = (query or "").strip()
    if not q:
        return "[错误] 查询为空"
    n = max(1, min(int(top_k or 5), 10))

    def _fallback(reason: str) -> str:
        """知识库不可用时**自己联网**，别把"去联网"这句话甩给模型。

        实测模型收到 "[知识库未启动] ConnectError" 会硬着头皮继续，
        搜偏之后编出一份无关答案（问算法题，答参赛资格）。
        """
        import sys as _sys
        print(f"[knowledge_search] 降级成联网搜索：{reason}", file=_sys.stderr, flush=True)
        try:
            res = web_search(q, 3)
        except Exception as e:                            # noqa: BLE001
            return (f"[知识库和联网都不可用] {reason}；web_search 也失败："
                    f"{type(e).__name__}。请如实告诉用户现在查不了。")
        return f"[知识库{reason}，已自动改用联网搜索]\n{res}"

    try:
        with httpx.Client(trust_env=False, timeout=20) as c:
            r = c.post(RAG_SEARCH_URL, json={"question": q, "top_k": n})
    except Exception as e:                                # noqa: BLE001
        return _fallback(f"未启动（{type(e).__name__}）")

    if r.status_code != 200:
        return _fallback(f"出错（HTTP {r.status_code}）")

    try:
        chunks = (r.json().get("chunks") or [])
    except ValueError:
        return _fallback("返回格式异常")

    if not chunks:
        return _fallback(f"没有「{q}」的内容")

    out = [f"知识库命中 {len(chunks)} 条（「{q}」）："]
    for i, ch in enumerate(chunks, 1):
        out.append(f"{i}. [{ch.get('doc_id', '')}] {ch.get('header_path', '')}"
                   f"（相似度 {ch.get('score', 0)}）")
        body = (ch.get("body") or "").strip().replace("\n", " ")
        out.append(f"   {body[:400]}")
    # 评测 T9：top1 相似度只有 0.1538（命中 Bellman-Ford 段落），模型仍
    # 直接按先验答了一半就收尾，漏答时间复杂度。弱命中必须显式推它换路。
    try:
        top = float(chunks[0].get("score", 0) or 0)
    except (TypeError, ValueError):
        top = 0.0
    if 0 < top < 0.35:
        out.append(f"\n[弱命中] 最高相似度只有 {top:.4f}，这些段落很可能答非所问。")
        out.append("不要直接据此作答：换 2~3 组更短的关键词再调 knowledge_search，"
                   "或改用 web_search 查；仍拿不到就如实说不确定，"
                   "**不要用先验知识把答案补齐**。")
    return "\n".join(out)


# ---------------------------------------------------------------- 联网
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")


def _search_one(q: str, n: int) -> list[tuple[str, str, str]]:
    """单次 Bing 搜索，返回 [(标题, URL, 摘要)]。"""
    import httpx
    from lxml import html as LH

    try:
        with httpx.Client(trust_env=False, timeout=12, follow_redirects=True,
                          headers={"User-Agent": _UA}) as c:
            r = c.get("https://cn.bing.com/search",
                      params={"q": q, "setlang": "zh-CN", "count": n})
    except Exception:                                     # noqa: BLE001
        return []

    if r.status_code >= 400:
        return []

    try:
        doc = LH.fromstring(r.text)
    except Exception:                                     # noqa: BLE001
        return []

    tokens = [t for t in q.replace("，", " ").replace(",", " ").split() if t]
    rows = []
    for li in doc.cssselect("li.b_algo"):
        a = li.cssselect("h2 a")
        if not a:
            continue
        title = (a[0].text_content() or "").strip()
        url = (a[0].get("href") or "").strip()
        sn = li.cssselect(".b_caption p")
        snippet = (sn[0].text_content() or "").strip() if sn else ""
        if not url.startswith("http"):
            continue
        blob = (title + snippet).lower()
        if tokens and not any(t.lower() in blob for t in tokens):
            continue
        rows.append((title, url, snippet))
        if len(rows) >= n:
            break
    return rows


# 从查询里可以安全剥掉的时间/语气词。Bing 对"近7天"这类词几乎不认，
# 带着它搜经常 0 结果（实测"显卡价格 近7天"空，"RTX 5070 价格"有结果）。
_NOISE_WORDS = (
    "近7天", "近七天", "近7日", "最近7天", "最近一周", "最近一个月", "近一个月",
    "这周", "本周", "上周", "今天", "今日", "昨天", "最新", "近期", "目前", "现在",
    "一下", "帮我", "查一下", "查查", "看看", "怎么样", "如何",
)


def _simplify_query(q: str) -> str:
    """去掉时间词和口语词，只留实体词。"""
    import re as _re

    out = q
    for w in _NOISE_WORDS:
        out = out.replace(w, " ")
    out = _re.sub(r"\s+", " ", out).strip(" ，,。?？")
    return out


def _core_query(q: str) -> str:
    """再退一步：只取前两个词（通常是"型号 + 价格"这种最能搜到的组合）。"""
    import re as _re

    parts = [p for p in _re.split(r"[\s，,、+]+", q) if p]
    return " ".join(parts[:2])


def web_search(query, count: int = 5) -> str:
    """多关键词并行搜索，汇总多来源，按 URL 去重。

    参数说明：
      query —— 字符串（单个查询）或字符串列表（多个角度并行搜）
      count —— 每个查询取几条

    为什么要多关键词：一个角度搜不全。问"中山天气"，同时搜
    "中山天气"、"中山天气 今天"、"中山 气温"，三个来源交叉，信息更全。

    为什么要有自动降级：带着"近7天""最新"这类词的查询 Bing 经常直接 0 结果，
    而模型看到空结果就爱回"给我一个网址"——等于把活推回给用户。
    这里在 0 结果时自动剥词、再退到核心词重搜一次，尽量让它拿到东西。
    """
    from concurrent.futures import ThreadPoolExecutor

    if isinstance(query, str):
        qs = [query.strip()] if query.strip() else []
    elif isinstance(query, (list, tuple)):
        qs = [str(x).strip() for x in query if str(x).strip()]
    else:
        qs = [str(query).strip()] if str(query).strip() else []

    if not qs:
        return "[错误] 查询为空"
    qs = qs[:4]                       # 最多 4 个角度，再多意义不大
    n = max(1, min(int(count or 5), 8))

    def _run(batch_qs: list[str]) -> dict[str, tuple[str, str, str, str]]:
        with ThreadPoolExecutor(max_workers=len(batch_qs)) as ex:
            batches = list(ex.map(lambda q: _search_one(q, n), batch_qs))
        seen: dict[str, tuple[str, str, str, str]] = {}
        for q, rows in zip(batch_qs, batches):
            for (t, u, sn) in rows:
                if u not in seen:
                    seen[u] = (t, u, sn, q)
        return seen

    seen = _run(qs)
    used, note = qs, ""

    # 第一级降级：剥时间词/口语词
    if not seen:
        simplified = []
        for q in qs:
            s = _simplify_query(q)
            if s and s not in simplified and s not in qs:
                simplified.append(s)
        if simplified:
            seen = _run(simplified[:4])
            if seen:
                used = simplified[:4]
                note = f"（原查询「{' / '.join(qs)}」无结果，已自动去掉时间词重搜）"

    # 第二级降级：只留核心两词
    if not seen:
        cores = []
        for q in qs:
            c = _core_query(_simplify_query(q) or q)
            if c and c not in cores:
                cores.append(c)
        if cores and cores != used:
            seen = _run(cores[:4])
            if seen:
                used = cores[:4]
                note = f"（原查询「{' / '.join(qs)}」无结果，已改用短关键词「{' / '.join(used)}」重搜）"

    if not seen:
        # 注意：这里**不能放具体商品/型号做示例**。实测模型会把这个示例
        # 当成"搜索结果"复述给用户（答非所问地报一个 RTX 5070 的价格）。
        return (f"[没搜到] 「{' / '.join(qs)}」换了两轮关键词仍然 0 结果。\n"
                "下一步：换成两三个词的短关键词再搜一次，或用 fetch_url 抓已知站点。\n"
                "都拿不到就如实说「搜索没有返回结果」，**不要编造内容**，"
                "也不要把这句提示里的文字当成结果。")

    qline = " | ".join(used) if len(used) > 1 else used[0]
    out = [f"「{qline}」的搜索结果（{len(seen)} 条，已按来源去重）{note}"]
    for i, (t, u, sn, q) in enumerate(seen.values(), 1):
        out.append(f"{i}. {t[:80]}\n   {u[:120]}\n   {sn[:200]}")

    out.append("")
    out.append("⚠️ 以上是标题和摘要，**不是答案**。")
    out.append("摘要里没有具体数据（温度/价格/版本/比分）时：")
    out.append("**用 fetch_url 抓第 1~3 条最相关的页面**，从正文提取事实。")
    out.append("如果多个来源的数据不一致，**都列出来并标明各自来源**。")
    return "\n".join(out)


def fetch_url(url: str, max_chars: int = 6000) -> str:
    """抓网页并用 readability 提取正文（去掉导航、广告、侧栏）。

    不提取的话拿回来是原始 HTML，几千个标签，模型读不懂还占满上下文。
    """
    import httpx
    import re
    from lxml import html as LH

    u = (url or "").strip()
    # 这几个站点反爬很硬（知乎 403、公众号要 JS），试了只会浪费轮次。
    # 直接回一句"换来源"，比让它 403 三次强。
    for bad, why in (("zhihu.com", "知乎"), ("zhuanlan.zhihu.com", "知乎专栏"),
                     ("mp.weixin.qq.com", "公众号"), ("weixin.qq.com", "微信")):
        if bad in u:
            return (f"[换来源] {why}有反爬，抓不到正文。"
                    "改用 ZOL（detail.zol.com.cn）、京东搜索页、慢慢买（manmanbuy）这类报价站。")
    if not u.startswith(("http://", "https://")):
        return "[错误] 只支持 http/https 链接"

    try:
        with httpx.Client(trust_env=False, timeout=15, follow_redirects=True,
                          headers={"User-Agent": _UA}) as c:
            r = c.get(u)
    except Exception as e:                                # noqa: BLE001
        return f"[错误] 抓取失败：{type(e).__name__}: {e}"

    if r.status_code >= 400:
        return f"[错误] HTTP {r.status_code}：{u}"

    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return f"[拒绝] 不是网页（{ctype}），文件请用下载方式"

    text = ""
    try:
        from readability import Document
        doc = Document(r.text)
        text = LH.fromstring(doc.summary()).text_content()
    except Exception:                                     # noqa: BLE001
        body = LH.fromstring(r.text)
        for bad in body.cssselect("script, style, nav, footer, header, aside"):
            bad.getparent().remove(bad)
        text = body.text_content()

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return f"[错误] {u} 提取不到正文（可能是 JS 渲染的页面）"

    limit = max(500, min(int(max_chars or 6000), 12000))
    more = f"\n\n…（共 {len(text)} 字符，只显示前 {limit}）" if len(text) > limit else ""
    return f"{u}\n\n{text[:limit]}{more}"

# ---------------------------------------------------------------- 结构摘要
def summarize_file(p: Path, max_items: int = 8) -> str:
    """不看全文，只给结构：函数名、类名、行数、导入。

    为什么这么做：30B 只有 4 GB 留给上下文，读 3 个 400 行的文件就满了。
    但做重构时模型需要的是"这个文件有什么"，不是"每一行长什么样"。
    摘要把 400 行压成 6 行，需要细节再按行号范围读。
    """
    why = _denied(p)
    if why:
        return f"{p.name}: {why}"
    if not p.exists():
        return f"{p.name}: 文件不存在"
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return f"{p.name}: 不是文本文件"
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        return f"{p.name}: {size // 1024} KB，超过上限"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"{p.name}: 读取失败 {e}"

    lines = text.splitlines()
    n = len(lines)
    head = f"{p.name}（{n} 行, {size} 字节）"

    if p.suffix.lower() == ".py":
        import ast as _ast
        try:
            tree = _ast.parse(text)
        except SyntaxError as e:
            return f"{head}\n  语法错误（第 {e.lineno} 行）：{e.msg[:60]}"
        funcs, classes, imports = [], [], []
        for node in tree.body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                args = len(getattr(node.args, "args", []))
                funcs.append(f"{node.name}({args}参)@{node.lineno}")
            elif isinstance(node, _ast.ClassDef):
                methods = sum(1 for x in node.body
                              if isinstance(x, (_ast.FunctionDef, _ast.AsyncFunctionDef)))
                classes.append(f"{node.name}({methods}方法)@{node.lineno}")
            elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = ",".join(a.name.split(".")[0] for a in node.names[:3])
                imports.append(mod or names)
        out = [head]
        if funcs:
            out.append("  函数: " + ", ".join(funcs[:max_items])
                       + (f" …共{len(funcs)}个" if len(funcs) > max_items else ""))
        if classes:
            out.append("  类: " + ", ".join(classes[:max_items]))
        if imports:
            out.append("  导入: " + ", ".join(dict.fromkeys(imports))[:120])
        if not (funcs or classes):
            out.append("  只有顶层语句，没有函数/类定义")
        return "\n".join(out)

    # 非 Python：首行 + 长度
    first = next((l.strip() for l in lines if l.strip()), "")
    return f"{head}\n  开头: {first[:100]}"


def read_files(paths: list[str], max_items: int = 8) -> str:
    """一次看多个文件的结构。比逐个 read_file 省 80% 上下文。"""
    if not isinstance(paths, list) or not paths:
        return "[错误] paths 必须是非空数组"
    if len(paths) > 10:
        return "[错误] 一次最多 10 个文件"
    out = []
    for raw in paths:
        try:
            out.append(summarize_file(_resolve(str(raw)), max_items))
        except ValueError as e:
            out.append(f"{raw}: {e}")
    return "\n\n".join(out)

# ---------------------------------------------------------------- 工具实现

def read_file(path: str, max_lines: int = 400, start: int = 1, end: int = 0) -> str:
    p = _resolve(path)
    why = _denied(p)
    if why:
        return f"[拒绝] {why}"
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    if p.is_dir():
        return f"[错误] {p} 是目录，用 list_dir"
    # 图片走 OCR（macOS Vision），PDF 先渲染成图再 OCR
    _img_sfx = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".tiff"}
    if p.suffix.lower() in _img_sfx:
        from . import vision as _vis

        return _vis.read_image(p)

    if p.suffix.lower() == ".pdf":
        from . import vision as _vis

        import pymupdf

        try:
            doc = pymupdf.open(str(p))
            n = min(doc.page_count, 3)
            outs = []
            tmp_dir = WRITE_ROOT / ".pdf_pages"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            for i in range(n):
                png = tmp_dir / f"_scan_{i}.png"
                doc.load_page(i).get_pixmap(dpi=140).save(str(png))
                outs.append(f"--- 第 {i + 1} 页 ---\n" + _vis.read_image(png, max_chars=3000))
            doc.close()
            return f"[PDF] {p.name}（共 {doc.page_count} 页，OCR 前 {n} 页）\n\n" + "\n\n".join(outs)
        except Exception as e:                            # noqa: BLE001
            return f"[错误] PDF OCR 失败：{type(e).__name__}: {str(e)[:150]}"

    if p.suffix.lower() not in TEXT_SUFFIXES:
        return f"[拒绝] {p.suffix or '无扩展名'} 不是文本文件，不读"
    size = p.stat().st_size
    # 大文件（日志、导出数据）不整篇加载，流式读指定行段——
    # 一次塞 100 MB 进内存会把模型上下文和进程都撑爆。
    if size > MAX_READ_BYTES:
        from itertools import islice

        a = max(1, start) - 1
        take = (end - start + 1) if end else max_lines
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                chunk_lines = [ln.rstrip("\n") for ln in islice(f, a, a + take)]
        except OSError as e:
            return f"[错误] 读取失败：{e}"
        head_line = (f"{p}（大文件 {size / 1024 / 1024:.1f} MB，"
                     f"读取第 {a + 1}-{a + len(chunk_lines)} 行，共取 {len(chunk_lines)} 行）")
        note = ("\n\n…（文件很大，只给了这一段。要继续读用 start/end 往前翻；"
                "统计/汇总请用 analyze_data，不要整篇读）")
        return head_line + "：\n\n" + "\n".join(chunk_lines) + note

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"[错误] 读取失败：{e}"
    lines = text.splitlines()
    total = len(lines)
    # 指定了 start/end 就按范围给——配合 read_files 的结构摘要用，
    # 先看摘要定位到第 40 行，再只读 40-80 行，省上下文。
    if end or start > 1:
        a = max(1, start) - 1
        b = min(total, end) if end else min(total, a + max_lines)
        chunk = "\n".join(lines[a:b])
        return f"{p}（第 {a + 1}-{b} 行，共 {total} 行）：\n\n{chunk}"
    head = "\n".join(lines[:max_lines])
    tail = f"\n\n…（共 {total} 行，只显示前 {max_lines} 行。要看别的范围用 start/end）" if total > max_lines else ""
    return f"{p}（{size} 字节，{total} 行）：\n\n{head}{tail}"


def list_dir(path: str = "") -> str:
    p = _resolve(path or str(ALLOW_ROOT))
    if not p.is_dir():
        return f"[错误] 不是目录：{p}"
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if child.name.startswith("."):
                continue
            # 列根目录时别把 /dev（352 项）、/Volumes（外接盘）这些倒给模型
            if str(child) in SKIP_DIRS_ABS:
                continue
            if child.is_dir():
                n = sum(1 for _ in child.iterdir()) if _safe_iter(child) else "?"
                entries.append(f"{child.name}/  （{n} 项）")
            else:
                entries.append(f"{child.name}  （{child.stat().st_size / 1024:.1f} KB）")
            if len(entries) >= MAX_LIST:
                entries.append(f"…（超过 {MAX_LIST} 项，已截断）")
                break
    except OSError as e:
        return f"[错误] 列目录失败：{e}"
    if not entries:
        return f"{p} 是空目录"
    return f"{p} 下有 {len(entries)} 项：\n" + "\n".join("  " + e for e in entries)


def _safe_iter(p: Path):
    try:
        next(p.iterdir())
        return True
    except (OSError, StopIteration):
        return False


def _goals_path():
    from pathlib import Path as _P

    return _P.home() / "Projects" / "model-studio" / "data" / "goals.json"


def set_goal(goal: str, done: bool = False) -> str:
    """记录一个长期目标。目标模式：跨多轮对话自动推进，直到标记完成。"""
    import json as _json
    import time as _t

    g = (goal or "").strip()
    if not g:
        return "[错误] 目标不能为空"
    p = _goals_path()
    try:
        data = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"goals": []}
    except (OSError, ValueError):
        data = {"goals": []}

    if done:
        # 标记完成：匹配包含关键字的第一个未完成目标
        hit = False
        for x in data["goals"]:
            if not x.get("done") and g in x["goal"]:
                x["done"] = True
                x["done_at"] = int(_t.time())
                hit = True
                break
        if not hit:
            return f"[提示] 没找到包含「{g[:20]}」的未完成目标"
    else:
        data["goals"].append({"goal": g, "done": False,
                              "created": int(_t.time())})

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    open_ = [x["goal"] for x in data["goals"] if not x.get("done")]
    return (f"目标已{'标记完成' if done else '记录'}。"
            f"\n当前未完成 {len(open_)} 个：\n" + "\n".join(f"- {x}" for x in open_))


def list_goals() -> str:
    """列出所有目标（含完成状态）。"""
    import json as _json

    p = _goals_path()
    if not p.exists():
        return "还没有目标。用 set_goal 记录一个。"
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "[错误] goals.json 读不了"
    gs = data.get("goals") or []
    if not gs:
        return "还没有目标。"
    out = []
    for i, x in enumerate(gs, 1):
        mark = "✓" if x.get("done") else "○"
        out.append(f"{i}. {mark} {x['goal']}")
    return "\n".join(out)


def spawn_agents(tasks: str, max_turns: int = 5) -> str:
    """并行派多个子代理，各自独立上下文，只回结果。

    tasks 是 JSON 字符串数组，如 '["查A文件","写B函数","统计C数据"]'。

    为什么用它：单个 agent 串行做三件事要 3 倍时间，而且上下文混在一起
    容易互相污染。并行派发后各管各的，最后汇总。
    """
    import asyncio
    import json as _json
    import sys as _sys

    from . import agent as _agent
    from . import persona as _persona

    try:
        tlist = _json.loads(tasks)
    except (ValueError, TypeError):
        return "[错误] tasks 不是合法 JSON 数组"
    if not isinstance(tlist, list) or not tlist:
        return "[错误] tasks 必须是任务字符串的数组"
    tlist = [str(x).strip() for x in tlist if str(x).strip()][:6]

    _sys_base = _persona.as_system(with_tools=True)
    _tools_mod = _sys.modules[__name__]

    async def _run_all():
        return await asyncio.gather(*[
            _agent.run_task(
                t, _sys_base, _tools_mod,
                model="default_model",
                url="http://127.0.0.1:8080/v1/chat/completions",
                max_turns=max(1, min(int(max_turns or 5), 8)),
            )
            for t in tlist
        ])

    try:
        results = asyncio.run(_run_all())
    except RuntimeError:
        # 已经在事件循环里（agent 流式时），用当前循环跑
        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(_run_all())

    out = [f"并行派了 {len(tlist)} 个子代理，结果如下：", ""]
    for i, (t, r) in enumerate(zip(tlist, results), 1):
        mark = "✓" if r.get("ok") else "✗"
        ans = (r.get("answer") or "").strip().replace("\n", " ")
        out.append(f"{i}. {mark} 「{t[:40]}」({r.get('turns')} 轮)")
        out.append(f"   {ans[:280]}")
        out.append("")
    return "\n".join(out)


def design_tool(action: str, path: str, width: int = 1440, selector: str = "") -> str:
    """界面设计检查：无头浏览器按真实尺寸量布局问题 / 截图。

    action:
      check —— 检测 7 类问题（截断/溢出/图片/点击区/固定栏/重叠/对比度）
      shot  —— 截图（整页或指定 CSS 选择器）
    """
    from . import design as _d

    p = _resolve(path)
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    w = max(320, min(int(width or 1440), 2560))

    if (action or "").strip().lower() == "check":
        try:
            r = _d.check(p, width=w)
        except Exception as e:                            # noqa: BLE001
            return f"[错误] 检测失败：{type(e).__name__}: {str(e)[:200]}"
        items = r.get("issues") or []
        if not items:
            return f"✓ 没发现问题（{w}×900，{r.get('title', '')}）"
        errs = sum(1 for x in items if x.get("level") == "error")
        out = [f"发现 {len(items)} 个问题（严重 {errs}），视口 {w}×900："]
        for i, it in enumerate(items[:30], 1):
            tag = "✗" if it.get("level") == "error" else "!"
            out.append(f"{i}. {tag} {it.get('type')}：{it.get('detail')}")
            if it.get("sel"):
                out.append(f"   选择器 {it['sel']}")
        return "\n".join(out)

    if (action or "").strip().lower() == "shot":
        try:
            r = _d.shot(p, WRITE_ROOT / "shots", selector=selector, width=w)
        except Exception as e:                            # noqa: BLE001
            return f"[错误] 截图失败：{type(e).__name__}: {str(e)[:200]}"
        if not r.get("ok"):
            return f"[错误] {r.get('error')}"
        from pathlib import Path as _P

        url = "/shots/" + _P(r["file"]).name
        return f"[已截图] {url}\n![](/{url.lstrip('/')})"

    return f"[错误] 不认识的 action：{action}，用 check / shot"


def office_doc(action: str, path: str, spec: str = "", pages: str = "1") -> str:
    """Office 文档：Word / Excel / PPT / PDF。

    action:
      make_docx / read_docx
      make_xlsx / read_xlsx
      make_pptx / read_pptx
      to_pdf      （Office → PDF，需要 LibreOffice）
      render_pdf  （PDF → PNG，检查排版）

    spec 是 JSON 字符串，结构见 app/office.py 顶部注释。
    """
    from . import office as _off

    act = (action or "").strip().lower()
    # 读操作不用写权限，写操作必须校验
    if act.startswith("read_") or act == "render_pdf":
        p = _resolve(path)
        if not p.exists():
            return f"[错误] 文件不存在：{p}"
        return _off.run(act, str(p), spec, pages=pages)

    p = _resolve_write(path)
    return _off.run(act, str(p), spec, pages=pages)


CHART_DIR = WRITE_ROOT / "generated"   # 和 generate_image 同目录：main.py 有 /generated 静态路由

# matplotlib 默认字体没有中文，标签会全变成方框（豆腐块）。
# 按可用性依次挑：Hiragino（macOS 自带，简繁都有）、Heiti、Arial Unicode。
_CJK_FONTS = ["Hiragino Sans GB", "Heiti SC", "Arial Unicode MS", "STHeiti"]


def make_chart(spec: str) -> str:
    """把数据画成 PNG 折线/柱状/饼图/散点。

    spec 是 JSON 字符串：
      {"type":"line|bar|pie|scatter", "title":"标题", "x":["1月","2月"],
       "series":[{"name":"价格","y":[100,120]}], "ylabel":"元",
       "note":"数据来源：ZOL 2026-10-25"}

    为什么要单独一个工具：本地模型不会自己写 matplotlib 脚本，
    直接给它 run_bash 让它拼代码，十次有八次因为字体/中文/basename 出错。
    这里把绘图固定成"喂数据 → 出图"，模型只负责凑数据。
    """
    import json as _json

    try:
        d = _loads_loose(spec or "{}")
    except _json.JSONDecodeError as e:
        return f"[错误] spec 不是合法 JSON：{e}"

    kind = str(d.get("type") or "line").lower()
    if kind not in {"line", "bar", "pie", "scatter"}:
        return f"[错误] type 只支持 line/bar/pie/scatter，收到 {kind}"

    x = [str(v) for v in (d.get("x") or [])]
    series = d.get("series") or []
    if not isinstance(series, list) or not series:
        return "[错误] series 不能为空，格式 [{\"name\":\"\",\"y\":[...]}]"

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    have = {f.name for f in font_manager.fontManager.ttflist}
    fonts = [f for f in _CJK_FONTS if f in have]
    if fonts:
        plt.rcParams["font.sans-serif"] = fonts + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False       # 负号不显示成方块

    fig, ax = plt.subplots(figsize=(8, 4.6), dpi=140)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#161b22")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#30363d")
    ax.tick_params(colors="#8b949e", labelsize=9)
    ax.grid(True, color="#21262d", linewidth=.8, axis="y")
    ax.set_axisbelow(True)

    palette = ["#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#f85149", "#39c5cf"]

    try:
        if kind == "pie":
            vals = [float(v) for v in (series[0].get("y") or [])]
            labels = x or [str(i + 1) for i in range(len(vals))]
            if len(labels) != len(vals):
                labels = [str(i + 1) for i in range(len(vals))]
            ax.clear()
            ax.set_facecolor("#161b22")
            wedges, texts, autotexts = ax.pie(
                vals, labels=labels, autopct="%1.1f%%", startangle=90,
                colors=palette[:max(1, len(vals))],
                textprops={"color": "#e6edf3", "fontsize": 10})
            for t in autotexts:
                t.set_color("#0d1117")
                t.set_fontsize(9)
        else:
            for i, s in enumerate(series):
                ys = [float(v) for v in (s.get("y") or [])]
                xs = x[:len(ys)] if x else list(range(len(ys)))
                color = palette[i % len(palette)]
                name = str(s.get("name") or f"系列{i + 1}")
                if kind == "line":
                    ax.plot(xs, ys, marker="o", markersize=4, linewidth=2,
                            color=color, label=name)
                elif kind == "bar":
                    # 多系列并排：按系列数错开
                    n = len(series)
                    idx = [j + (i - (n - 1) / 2) * (0.72 / max(n, 1))
                           for j in range(len(ys))]
                    ax.bar(idx, ys, width=0.72 / max(n, 1), color=color, label=name)
                    ax.set_xticks(range(len(xs)))
                    ax.set_xticklabels(xs)
                    ax.margins(x=0.03)
                    # 柱状图顶部留白：不留的话图例必然压住最后一根柱子
                    _all = [v for s2 in series for v in (s2.get("y") or [])]
                    if _all and min(float(v) for v in _all) >= 0:
                        ax.set_ylim(0, max(float(v) for v in _all) * 1.18)
                else:
                    ax.scatter(xs, ys, s=42, color=color, label=name)
            if len(series) > 1 or kind == "bar":
                # loc="best" 让 matplotlib 自己挑不压数据的位置；
                # 固定 upper right 会盖住最后一根柱子（踩过）
                leg = ax.legend(frameon=False, fontsize=9, loc="best")
                for t in leg.get_texts():
                    t.set_color("#8b949e")

        title = str(d.get("title") or "")
        if title:
            ax.set_title(title, color="#e6edf3", fontsize=13, pad=12)
        ylabel = str(d.get("ylabel") or "")
        if ylabel and kind != "pie":
            ax.set_ylabel(ylabel, color="#8b949e", fontsize=10)
        note = str(d.get("note") or "")
        if note:
            fig.text(0.01, 0.015, note, color="#6e7681", fontsize=8)

        CHART_DIR.mkdir(parents=True, exist_ok=True)
        out = CHART_DIR / f"chart_{time.strftime('%Y%m%d_%H%M%S')}.png"
        fig.tight_layout()
        fig.savefig(out, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
    except (TypeError, ValueError) as e:
        return f"[错误] 数据格式不对：{e}"

    url = f"/generated/{out.name}"
    return (f"[已生成图表] {url}（{len(series)} 个系列，{kind}）\n"
            f"文件：{out}\n"
            f"**回答里把下面这行原样放进去就能显示图**：\n![图表]({url})")


def analyze_data(path: str, sample_rows: int = 3, to_format: str = "") -> str:
    """分析结构化数据（CSV / JSON / YAML / XML），给结构 + 统计。

    to_format 非空时顺便转换格式（csv / json / yaml），存到同目录。

    为什么要它：CSV 几千行，read_file 读不完；模型自己写脚本又慢又容易错。
    这里一次性给出「多少行、有哪些列、每列大概什么类型、数值范围」，
    模型不用看原始数据就能回答"这份数据有多少条""平均值多少"。
    """
    import csv
    import json as _json
    import statistics

    p = _resolve(path)
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    if not p.is_file():
        return f"[错误] 不是文件：{p}"

    ext = p.suffix.lower()
    n_sample = max(1, min(int(sample_rows or 3), 10))
    out: list[str] = []

    # ---------------- CSV
    if ext in {".csv", ".tsv"}:
        delim = "\t" if ext == ".tsv" else ","
        try:
            with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                rdr = csv.DictReader(f, delimiter=delim)
                cols = rdr.fieldnames or []
                rows = []
                total = 0
                for row in rdr:
                    total += 1
                    if len(rows) < 5000:        # 统计只取前 5000 行，够用
                        rows.append(row)
        except OSError as e:
            return f"[错误] 读取失败：{e}"

        out.append(f"【CSV】{p.name}  共 {total} 行 × {len(cols)} 列")
        out.append(f"列名：{', '.join(cols)}")

        # 每列类型 + 数值统计
        out.append("")
        out.append("| 列 | 类型 | 样例 | 统计 |")
        out.append("|---|---|---|---|")
        for c in cols[:15]:
            vals = [r.get(c, "") for r in rows[:2000]]
            nums = []
            for v in vals:
                try:
                    nums.append(float(str(v).replace(",", "")))
                except (ValueError, TypeError):
                    pass
            is_num = len(nums) >= max(1, len(vals) * 0.8)
            sample = (vals[0] if vals else "")[:24]
            if is_num and nums:
                stat = (f"min {min(nums):g} / max {max(nums):g} / "
                        f"mean {statistics.mean(nums):.2f}")
                typ = "数值"
            else:
                uniq = len(set(vals))
                stat = f"{uniq} 种取值"
                typ = "文本"
            out.append(f"| {c} | {typ} | {sample} | {stat} |")

        out.append("")
        out.append(f"前 {min(n_sample, len(rows))} 行：")
        for r in rows[:n_sample]:
            out.append("  " + " | ".join(f"{k}={r.get(k, '')}" for k in cols[:8]))

    # ---------------- JSON
    elif ext == ".json":
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
            data = _json.loads(raw)
        except (OSError, ValueError) as e:
            return f"[错误] JSON 解析失败：{e}"
        out.append(f"【JSON】{p.name}  {p.stat().st_size / 1024:.1f} KB")
        if isinstance(data, list):
            out.append(f"顶层是数组，{len(data)} 项")
            if data and isinstance(data[0], dict):
                out.append(f"每项字段：{', '.join(list(data[0].keys())[:12])}")
            for i, item in enumerate(data[:n_sample], 1):
                out.append(f"  {i}. {_json.dumps(item, ensure_ascii=False)[:160]}")
        elif isinstance(data, dict):
            out.append(f"顶层是对象，{len(data)} 个键")
            for k, v in list(data.items())[:12]:
                if isinstance(v, list):
                    desc = f"数组（{len(v)} 项）"
                elif isinstance(v, dict):
                    desc = f"对象（{len(v)} 键）"
                else:
                    desc = repr(v)[:60]
                out.append(f"  {k}: {desc}")
        else:
            out.append(f"顶层是 {type(data).__name__}：{repr(data)[:200]}")

    # ---------------- YAML
    elif ext in {".yaml", ".yml"}:
        try:
            import yaml
            data = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        except ImportError:
            return "[错误] 没装 pyyaml（pip install pyyaml）"
        except Exception as e:                            # noqa: BLE001
            return f"[错误] YAML 解析失败：{e}"
        out.append(f"【YAML】{p.name}")
        if isinstance(data, dict):
            out.append(f"顶层对象，{len(data)} 个键：{', '.join(list(data)[:15])}")
        elif isinstance(data, list):
            out.append(f"顶层数组，{len(data)} 项")
        else:
            out.append(repr(data)[:200])

    # ---------------- XML
    elif ext == ".xml":
        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(p)
            root = tree.getroot()
        except ET.ParseError as e:
            return f"[错误] XML 解析失败：{e}"
        kids = list(root)
        out.append(f"【XML】{p.name}  根标签 <{root.tag}>，直接子元素 {len(kids)} 个")
        from collections import Counter
        c = Counter(k.tag for k in kids)
        out.append("子元素统计：" + ", ".join(f"{k}×{v}" for k, v in c.most_common(10)))
        for k in kids[:n_sample]:
            out.append(f"  <{k.tag}> attrs={list(k.attrib)[:5]} text={(k.text or '').strip()[:60]}")

    else:
        return f"[错误] analyze_data 不支持 {ext}，支持 .csv/.tsv/.json/.yaml/.yml/.xml"

    # ---------------- 格式转换
    fmt = (to_format or "").strip().lower()
    if fmt:
        dst = p.with_suffix("." + ("yaml" if fmt == "yml" else fmt))
        try:
            if fmt == "json":
                payload = data if ext != ".csv" else rows
                dst.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                out.append(f"\n[已转换] {dst}（JSON）")
            elif fmt in {"yaml", "yml"}:
                import yaml
                payload = data if ext != ".csv" else rows
                dst.write_text(yaml.safe_dump(payload, allow_unicode=True),
                               encoding="utf-8")
                out.append(f"\n[已转换] {dst}（YAML）")
            elif fmt == "csv":
                if not isinstance(data, list) or not data:
                    out.append("\n[跳过转换] 只有「对象数组」才能转 CSV")
                else:
                    keys = list(data[0].keys())
                    with open(dst, "w", encoding="utf-8", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=keys)
                        w.writeheader()
                        w.writerows(data)
                    out.append(f"\n[已转换] {dst}（CSV）")
            else:
                out.append(f"\n[跳过转换] 不支持 {fmt}")
        except Exception as e:                            # noqa: BLE001
            out.append(f"\n[转换失败] {type(e).__name__}: {e}")

    return "\n".join(out)


def grep_code(pattern: str, path: str = "", glob: str = "", limit: int = 60) -> str:
    """按正则搜文件内容，返回「文件:行号: 内容」。

    和 find_files 的区别：find_files 按**文件名**找，grep_code 按**内容**找。
    找"哪个文件里定义了 foo()""哪里用了 TODO"这类，用这个。
    """
    import re

    pat = (pattern or "").strip()
    if not pat:
        return "[错误] pattern 不能为空"
    try:
        rx = re.compile(pat)
    except re.error as e:
        return f"[错误] 正则不合法：{e}"

    root = _resolve(path or str(ALLOW_ROOT))
    if not root.exists():
        return f"[错误] 路径不存在：{root}"
    glob_pat = (glob or "").strip() or "*"

    if not _safe_iter(root):
        return f"[错误] 目录不可读：{root}"

    skip_dirs = {"node_modules", ".git", ".venv", "venv", "__pycache__",
                 ".ms_backup", "dist", "build", ".next", ".cache", "site-packages"}
    hits: list[str] = []
    n = 0
    files_scanned = 0
    for f in root.rglob("*"):
        if any(part in skip_dirs for part in f.parts):
            continue
        if not f.is_file():
            continue
        if glob_pat != "*" and not f.match(glob_pat):
            continue
        if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
                                ".zip", ".gz", ".tar", ".dmg", ".mp4", ".bin",
                                ".safetensors", ".gguf", ".pt", ".onnx"}:
            continue
        try:
            if f.stat().st_size > 2_000_000:      # 跳过 >2MB 的
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files_scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{f}:{i}: {line.strip()[:160]}")
                n += 1
                if n >= limit:
                    break
        if n >= limit:
            break

    if not hits:
        return f"没找到匹配「{pat}」的内容（扫了 {files_scanned} 个文件）。"
    return (f"「{pat}」命中 {len(hits)} 处（扫了 {files_scanned} 个文件）：\n"
            + "\n".join(hits))


def find_files(pattern: str, path: str = "", limit: int = 40) -> str:
    """按文件名 glob 找文件，不递归超过 4 层。"""
    root = _resolve(path or str(ALLOW_ROOT))
    if not root.is_dir():
        return f"[错误] 不是目录：{root}"
    hits: list[str] = []
    base_depth = len(root.parts)
    import time as _t
    deadline = _t.time() + 8.0
    for dirpath, dirnames, filenames in os.walk(root):
        if _t.time() > deadline:
            hits.append("…（搜索超时 8 秒，只找到这些）")
            break
        d = Path(dirpath)
        if len(d.parts) - base_depth > 4:
            dirnames[:] = []
            continue
        dirnames[:] = [
            x for x in dirnames
            if not x.startswith(".")
            and x not in ("node_modules", "__pycache__", ".venv", "site-packages")
            and str(d / x) not in SKIP_DIRS_ABS
        ]
        for fn in filenames:
            if fnmatch.fnmatch(fn, pattern or "*"):
                hits.append(str(d / fn))
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    if not hits:
        return f"在 {root} 下没找到匹配 {pattern} 的文件"
    return f"找到 {len(hits)} 个（最多 {limit}）：\n" + "\n".join("  " + h for h in hits)



# ---------------------------------------------------------------- 执行命令
BASH_TIMEOUT = 30
VENV_DIR = WRITE_ROOT / ".venv"


def _ensure_venv() -> Path | None:
    """首次执行命令时在 sandbox 里建 venv，之后复用。

    为什么不一开始就建：建一次要 3~5 秒，大部分任务不需要装包，
    先建等于每个任务都白付这个成本。

    为什么必须隔离：不隔离的话 pip install 会装进系统 Python 3.13，
    模型装十个包，你的环境就脏了，而且版本冲突查不出来。
    """
    py = VENV_DIR / "bin" / "python"
    if py.exists():
        return VENV_DIR
    try:
        import subprocess as _sp
        _sp.run(["python3", "-m", "venv", str(VENV_DIR)],
                capture_output=True, timeout=120)
    except Exception:                                 # noqa: BLE001
        return None
    return VENV_DIR if py.exists() else None
BASH_OUTPUT_LIMIT = 4000

# 危险模式：这些不是"精确黑名单"，是明显会造成不可逆破坏的。
# 真正的安全靠沙箱（cwd 固定在 sandbox / Desktop）+ 超时 + 输出截断。
_DANGER = [
    (r"rm\s+-rf\s+/(?:\s|$)", "递归删除根目录"),
    (r"rm\s+-rf\s+~(?:\s|$)", "递归删除家目录"),
    (r"\bsudo\b", "需要 sudo"),
    (r"\bsu\b\s", "切换用户"),
    (r">\s*/dev/(?:sd|disk)", "写裸设备"),
    (r"mkfs", "格式化"),
    (r"dd\s+if=", "dd 写盘"),
    (r"chmod\s+-R\s+777\s+/", "全盘改权限"),
    (r"curl[^|]*\|\s*(?:ba)?sh", "远程脚本直接执行"),
    (r"wget[^|]*\|\s*(?:ba)?sh", "远程脚本直接执行"),
    (r":\(\)\s*\{", "fork 炸弹"),
    (r"shutdown|reboot|halt", "关机重启"),
]


_BG_TASKS: dict[str, dict] = {}


def check_task(task_id: str) -> str:
    """查后台任务状态；完成了就返回输出。

    配合 run_bash(background=true) 用：长任务（npm build、pytest、爬虫）
    扔后台，agent 继续干别的，过一会儿回来收结果。
    """
    import time as _time

    t = _BG_TASKS.get(task_id)
    if not t:
        return f"[错误] 没有 task_id={task_id}（服务重启过的话任务表会清空）"
    proc = t["proc"]
    el = _time.time() - t["t0"]
    if proc.poll() is None:
        return (f"[运行中] 已跑 {el:.0f}s  命令：{t['cmd'][:80]}\n"
                f"日志：{t['out']}\n稍后再查，或先做别的事。")
    try:
        txt = t["out"].read_text(encoding="utf-8", errors="replace")
    except OSError:
        txt = ""
    status = "成功" if proc.returncode == 0 else f"失败（exit {proc.returncode}）"
    return f"[完成] {status}  用时 {el:.0f}s\n--- 输出尾部 ---\n{txt[-3000:]}"


def run_bash(command: str, cwd: str = "", timeout: int = 0,
             background: bool = False) -> str:
    """在沙箱里执行 shell 命令。这是 agent 的"手"。

    限制：
    - cwd 默认 WRITE_ROOT，也可以指定（但必须在可写根内）
    - 30 秒超时，超时直接 kill
    - 输出截断 4000 字符（stdout + stderr 合并）
    - 明确危险的命令直接拒绝（见 _DANGER）
    """
    import re as _re
    import subprocess

    cmd = (command or "").strip()
    if not cmd:
        return "[错误] 命令为空"
    # 模型爱写 open("~/x")、cd ~/x —— shell 不会展开引号内的 ~，python 更不会。
    # 实测 30B 因此连续 3 次跑同一条报错命令，白烧三轮。
    if "~/" in cmd:
        cmd = cmd.replace("~/", str(Path.home()) + "/")
    for wrong in ("/home/user", "/home/ubuntu", "/root"):
        if wrong in cmd:
            cmd = cmd.replace(wrong, str(Path.home()))
    low = cmd.lower()
    for pat, why in _DANGER:
        if _re.search(pat, low):
            return f"[拒绝] 检测到危险操作（{why}）：{cmd[:80]}"

    work = _resolve_write(cwd) if cwd else WRITE_ROOT
    if not work.is_dir():
        return f"[错误] 工作目录不存在：{work}"

    # 清掉代理变量：内网命令不需要走代理，带着反而会让 curl/wget 卡住
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and k.lower() != "all_proxy"}
    # 优先用 sandbox 里的 venv：pip install 装在这里，系统 Python 不受影响。
    # venv 的 python 是真实解释器（不是 wrapper），`python xxx.py` 直接能用。
    _venv = _ensure_venv()
    if _venv:
        env["PATH"] = f"{_venv / 'bin'}:" + env.get("PATH", "/usr/bin:/bin")
        env["VIRTUAL_ENV"] = str(_venv)
        # 去掉 PYTHONHOME，否则它会覆盖 venv 的解释器路径
        env.pop("PYTHONHOME", None)
    else:
        # venv 建失败（磁盘满/权限）时退回 wrapper
        _bin = WRITE_ROOT / ".bin"
        if _bin.is_dir():
            env["PATH"] = str(_bin) + ":" + env.get("PATH", "/usr/bin:/bin")

    # pip install 默认给 150 秒：装带依赖的包要下载好几个，30 秒必然超时。
    # 实测 `pip install pyshorteners` 30 秒装不完，模型会以为"装不上"。
    default_t = 150 if ("pip install" in low or "pip3 install" in low) else BASH_TIMEOUT
    t = min(max(timeout or default_t, 5), 300)

    # 后台模式：构建/测试/装依赖这类长任务扔后台，agent 可以接着干别的
    if background:
        import uuid
        tid = uuid.uuid4().hex[:8]
        out_dir = WRITE_ROOT / ".bg"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{tid}.log"
        with open(out, "w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=str(work), env=env,
                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
            )
        _BG_TASKS[tid] = {"proc": proc, "out": out, "cmd": cmd, "t0": time.time()}
        return (f"[后台运行] task_id={tid}\n"
                f"命令：{cmd[:100]}\n"
                f"用 check_task(\"{tid}\") 查状态；可以先做别的事。")

    try:
        r = subprocess.run(
            cmd, shell=True, cwd=str(work), env=env,
            capture_output=True, text=True, timeout=t,
        )
    except subprocess.TimeoutExpired:
        return (f"[超时] 命令超过 {t} 秒被终止：{cmd[:80]}\n"
                "如果是在装包，可以调大 timeout 参数（最大 300）重试。")
    except OSError as e:
        return f"[错误] 无法执行：{e}"

    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip()
    # 30B 反复写 python3 -c "import csv; with open(...)..." —— 分号后面不能接 with/for/if，
    # 它看不懂 SyntaxError，会原样再跑三遍。这里直接给可执行的做法。
    if "SyntaxError" in out and "python" in low:
        out += ("\n[提示] python3 -c 里不能用 `;` 串联 with/for/if 这类复合语句。"
                "把多行代码写进临时 .py 文件，再用 python3 文件路径 执行。")
    if len(out) > BASH_OUTPUT_LIMIT:
        out = out[:BASH_OUTPUT_LIMIT] + f"\n…（输出超 {BASH_OUTPUT_LIMIT} 字符，已截断）"
    if not out:
        out = "（无输出）"
    return f"$ {cmd}\n[cwd: {work.name}]\n[exit {r.returncode}]\n{out}"


# ---------------------------------------------------------------- 写操作

def _resolve_write(path: str) -> Path:
    """写路径校验：必须在 WRITE_ROOT 内，且不能碰 .git / 敏感文件。"""
    raw = _fix_linux_path((path or "").strip())
    if not raw:
        raise ValueError("路径不能为空")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        # 模型经常写 "sandbox/x.md"（它以为路径要带沙箱名），
        # 直接拼会变成 sandbox/sandbox/x.md。
        if p.parts and p.parts[0] == WRITE_ROOT.name:
            p = WRITE_ROOT.parent / p
        else:
            p = WRITE_ROOT / p
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"路径无效：{e}")
    roots = _write_roots()
    if not any(p.is_relative_to(r) for r in roots):
        allowed = " 或 ".join(str(r) for r in roots)
        raise ValueError(f"写操作只允许在 {allowed} 内（当前 {p}）")
    why = _denied(p)
    if why:
        raise ValueError(why)
    if ".git" in p.parts:
        raise ValueError("不允许改 .git 目录")
    return p


def _backup(p: Path) -> str:
    """覆盖前把原文件复制到 .ms_backup/<时间戳>/ 下。返回备份路径。"""
    if not p.exists() or not p.is_file():
        return ""
    import shutil
    import time as _t
    stamp = _t.strftime("%Y%m%d-%H%M%S")
    rel = Path(p.name)
    dst = BACKUP_DIR / stamp / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dst)
    return str(dst)


def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    """精确替换文件里的一段（不是整篇重写）。

    为什么需要它：write_file 只能整篇覆盖，改 10 行要重发 500 行，
    既慢又容易把其他改动冲掉。edit_file 只碰目标片段，其余原样保留。

    old_string 必须在文件里唯一（除非 replace_all=true），
    否则会误改到别处——这也是我自己的编辑工具的规则。
    """
    p = _resolve_write(path)
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    if not p.is_file():
        return f"[错误] 不是文件：{p}"

    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        return f"[错误] 读不了：{type(e).__name__}"

    if not old_string:
        return "[错误] old_string 不能为空"
    if old_string == new_string:
        return "[错误] old_string 和 new_string 相同，没有改动"

    cnt = text.count(old_string)
    if cnt == 0:
        # 给点线索：展示前 30 行，方便模型对齐缩进/空白
        head = "\n".join(text.splitlines()[:30])
        return ("[错误] 文件里没有这段内容（注意缩进和空行要完全一致）。"
                f"\n文件前 30 行供对照：\n{head[:1200]}")
    if cnt > 1 and not replace_all:
        return (f"[错误] old_string 在文件里出现 {cnt} 次，不唯一。"
                "请把上下文写长一点（包含前后几行），或者设 replace_all=true 全改。")

    bak = _backup(p)
    new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
    p.write_text(new_text, encoding="utf-8")
    n = cnt if replace_all else 1
    return f"[已修改] {p}  替换 {n} 处（备份：{bak}）"


def _loads_loose(raw: str) -> dict:
    """宽松解析模型生成的 JSON。

    本地 30B 写 JSON 常见三种错误，严格的 json.loads 会直接拒绝：
      1. 漏闭合括号   {"a":1,"b":[1,2]     ← 少一个 }
      2. 尾逗号       {"a":1,}             ← 多一个 ,
      3. 包了代码围栏  ```json ... ```

    依次尝试：去围栏 → 去尾逗号 → 补括号，全部失败才抛原始错误。
    实测 30B 在 make_chart 的 spec 上漏括号是高频失败（4/4 次），
    这一层兜底比让 agent 重试更有效——重试也是同样的错。
    """
    s = (raw or "").strip()

    # 去掉 ```json ... ``` 围栏
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 去尾逗号（只处理 } 和 ] 前的逗号，字符串内的不动）
    cleaned = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 补不配对的闭合括号。跳过字符串内部，避免把 "{" 当成结构括号。
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in cleaned:
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif in_str:
            continue
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if stack:
        repaired = cleaned + "".join(reversed(stack))
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    # 都失败：抛原始错误，调用方给可读提示
    return json.loads(s)


def write_file(path: str, content: str, mode: str = "overwrite") -> str:
    """写文件。mode=overwrite 会先备份原文件，mode=append 追加到末尾。"""
    p = _resolve_write(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append" and p.exists():
        old = p.read_text(encoding="utf-8", errors="replace")
        p.write_text(old + content, encoding="utf-8")
        return f"[已追加] {p}（新增 {len(content)} 字符，现在共 {p.stat().st_size} 字节）"

    if WRITE_PROTECT and p.exists() and mode != "append":
        return (f"[拒绝] 写保护开启：{p} 已存在。"
                "要么换一个新文件名，要么让用户关闭写保护。")
    bak = _backup(p)
    existed = bool(bak)
    p.write_text(content, encoding="utf-8")
    note = f"，原文件已备份到 {bak}" if bak else ""
    return (f"[{'已覆盖' if existed else '已创建'}] {p}"
            f"（{len(content)} 字符，{p.stat().st_size} 字节{note}）")


def make_dir(path: str) -> str:
    p = _resolve_write(path)
    if p.exists():
        return f"[已存在] {p}"
    p.mkdir(parents=True, exist_ok=True)
    return f"[已创建目录] {p}"


def move_file(src: str, dst: str) -> str:
    a = _resolve_write(src)
    b = _resolve_write(dst)
    if not a.exists():
        return f"[错误] 源文件不存在：{a}"
    if not a.is_file():
        return f"[错误] {a} 不是文件"
    if WRITE_PROTECT and b.exists():
        return f"[拒绝] 写保护开启：目标 {b} 已存在。"
    b.parent.mkdir(parents=True, exist_ok=True)
    bak = _backup(b)
    a.rename(b)
    note = f"，目标原文件已备份到 {bak}" if bak else ""
    return f"[已移动] {a} → {b}{note}"


# ---------------------------------------------------------------- OpenAI 工具定义

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读文件内容。大文件用 start/end 只读需要的行段——"
                "先用 read_files 看结构定位，再读具体范围。"
                "**总结/概括/找函数这类任务至少读 80 行或读到函数定义**，"
                "不要只给 max_lines=1 就下结论（只看 docstring 会漏掉函数体）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "max_lines": {"type": "integer", "description": "最多读多少行，默认 400"},
                    "start": {"type": "integer", "description": "起始行（1-based）"},
                    "end": {"type": "integer", "description": "结束行，含"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出一个目录里有什么（只列一层）。想知道有哪些文件就用它；要看文件内容用 read_file。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，留空为项目根"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按文件名模式搜索文件，例如 *.py 或 config*。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "文件名 glob，如 *.md"},
                    "path": {"type": "string", "description": "搜索起点目录，留空为项目根"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确替换文件片段。**改已有文件用这个，不要整篇重写**。old_string 要唯一、缩进一致。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old_string": {"type": "string", "description": "原文"},
                    "new_string": {"type": "string", "description": "新内容"},
                    "replace_all": {"type": "boolean", "description": "全改"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_goal",
            "description": "记录长期目标（done=true 标记完成）。目标会在后续对话里持续推进。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "目标内容"},
                    "done": {"type": "boolean", "description": "是否标记完成"},
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_goals",
            "description": "列出所有目标（含完成状态）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agents",
            "description": (
                "并行派多个子代理做独立小任务，各自独立上下文，只回结果。"
                "tasks 是 JSON 字符串数组。用于：多个独立问题一起查、批量分析、交叉验证。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {"type": "string", "description": "JSON 数组，如 [\"任务1\",\"任务2\"]"},
                    "max_turns": {"type": "integer", "description": "每个子代理最多几轮，默认 5"},
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "design",
            "description": "界面检查：check 检测 7 类布局问题（带选择器）；shot 截图。写完网页先 check。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["check", "shot"]},
                    "path": {"type": "string", "description": "HTML 文件路径"},
                    "width": {"type": "integer", "description": "宽，手机390"},
                    "selector": {"type": "string", "description": "CSS 选择器"},
                },
                "required": ["action", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "office",
            "description": (
                "Office 文档：make_docx/make_xlsx/make_pptx（spec 传 JSON）/ read_* / "
                "render_pdf / md2docx（Markdown 直接转 Word，path 给 .md）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["make_docx", "read_docx", "make_xlsx", "read_xlsx",
                                        "make_pptx", "read_pptx", "to_pdf", "render_pdf",
                                        "md2docx"]},
                    "path": {"type": "string", "description": "路径"},
                    "spec": {"type": "string", "description": "JSON 结构"},
                    "pages": {"type": "string", "description": "页码 1/1-3/all"},
                },
                "required": ["action", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_data",
            "description": "分析 CSV/JSON/YAML/XML：行数、列类型、min/max/mean。to_format 可转格式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "路径"},
                    "sample_rows": {"type": "integer", "description": "样例行数"},
                    "to_format": {"type": "string", "description": "转格式"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_task",
            "description": (
                "查后台任务状态。配合 `run_bash(background=true)` 用："
                "返回 task_id 后过一会儿用这个查，完成会给出输出尾部。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "run_bash 返回的 task_id"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_code",
            "description": "按正则搜文件内容，返回 文件:行号:内容。按文件名找用 find_files。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则"},
                    "path": {"type": "string", "description": "目录"},
                    "glob": {"type": "string", "description": "如 *.py"},
                    "limit": {"type": "integer", "description": "上限"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或覆盖文件。只能写 ~/Projects/sandbox 下。覆盖前会自动备份原文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对于 sandbox 或绝对路径）"},
                    "content": {"type": "string", "description": "完整文件内容"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"],
                             "description": "overwrite 覆盖（默认），append 追加"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "执行 shell（python/git/npm/pytest/pip），默认 30s 超时。"
                "长任务设 background=true，拿 task_id 用 check_task 收。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "命令"},
                    "cwd": {"type": "string", "description": "工作目录"},
                    "timeout": {"type": "integer",
                                "description": "超时秒"},
                    "background": {"type": "boolean",
                                   "description": "后台跑"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_exchange_rate",
            "description": (
                "查实时汇率（免费 API）。问「美元汇率」「人民币换日元多少」时用。"
                "base 是基准货币，targets 逗号分隔，如 USD,EUR,JPY。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "base": {"type": "string", "description": "基准货币代码，默认 CNY"},
                    "targets": {"type": "string", "description": "目标货币，逗号分隔"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "查城市天气，返回当前温度/湿度/风 + 未来 3 天预报。"
                "**问天气第一步就用它**，不要 web_search。支持中文城市名。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 中山、深圳"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "根据文字描述生成图像（在线服务，约 2 秒）。"
                "用户说「画一个 XX」「生成一张图」时用。"
                "生成的文件存到 sandbox/generated/，返回路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图像描述，越具体越好"},
                    "width": {"type": "integer", "description": "宽，默认 512，最大 1024"},
                    "height": {"type": "integer", "description": "高，默认 512，最大 1024"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": (
                "查本地知识库（OI Wiki：算法、数据结构、竞赛题解）。"
                "**这是第一步**——问技术/算法问题先查它，0.2 秒出结果。"
                "查不到再用 web_search 联网。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查的内容"},
                    "top_k": {"type": "integer", "description": "返回几条，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "搜索互联网（Bing）。查最新版本、文档、新闻、报错信息时用。"
                "搜到结果后可以用 fetch_url 抓具体页面看详情。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "count": {"type": "integer", "description": "结果条数，默认 6，最多 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "抓一个网页并用 readability 提取正文（去掉导航/广告）。"
                "搜索摘要里没有具体数字时，用它抓第 1~3 条结果的正文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 http(s) 地址"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 6000"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_chart",
            "description": (
                "把数据画成图表 PNG（line/bar/pie/scatter）。"
                "用户要「画个图 / 图表展示 / 趋势图」时用这个，不要自己拼 matplotlib 脚本。"
                "spec 是 JSON 字符串，series 必填；返回的 markdown 图片引用要原样放进回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": (
                            'JSON，例如 {"type":"bar","title":"标题","x":["A","B"],'
                            '"series":[{"name":"价格","y":[1,2]}],"ylabel":"元",'
                            '"note":"来源：ZOL 2026-10-25"}'
                        ),
                    },
                },
                "required": ["spec"],
            },
        },
    },
]

# ======================================================================
# 工具 schema 瘦身
#
# 为什么：20 个工具的 JSON 全量是 7400+ 字符，每个请求都要带上，还没开始
# 对话就先吃掉 5k~7k token。30B 跑在 24 GB 上，这些 token 直接换成 OOM 概率。
#
# 怎么瘦：
#   1. 工具描述只留第一句（≤40 字）——第二句以后是"什么时候用"的补充说明，
#      persona 里已经有同样的话
#   2. 参数描述只保留少数真正需要解释的（spec / steps / tasks / query …），
#      其余靠参数名本身（path / url / count / width）
#
# 效果：7438 → 约 5550 字符（省 25%），能力不变。
_VERBOSE_PARAMS = {"spec", "steps", "tasks", "query", "action", "command", "content", "goal"}

# 这几个工具靠描述里的"取值清单"才能正确调用（office 的 action 有哪些、
# design 的 check/shot），截太狠会变成乱试。给它们多留一点。
# read_file：评测 T7 里模型给 max_lines=1 只看了 docstring 就总结。
# compact 后描述只剩「读文件内容」5 个字，追加的「总结类至少读 80 行」
# 会被截掉，所以这个工具保留完整描述（多几十字，对 8k 上下文可忽略）。
_KEEP_FULL_DESC = {"office", "design", "make_chart", "spawn_agents", "read_file"}


def _compact_schemas(items: list[dict]) -> list[dict]:
    for t in items:
        fn = t.get("function") or {}
        desc = (fn.get("description") or "").strip()
        if fn.get("name") not in _KEEP_FULL_DESC:
            head = desc.split("。")[0].strip()
            fn["description"] = (head[:52] or desc[:52]).rstrip("，,；;")
        props = (fn.get("parameters") or {}).get("properties") or {}
        for pname, p in props.items():
            if pname not in _VERBOSE_PARAMS:
                p.pop("description", None)
    return items


TOOLS = _compact_schemas(TOOLS)

_HANDLERS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "find_files": find_files,
    "write_file": write_file,
    "edit_file": edit_file,
    "grep_code": grep_code,
    "check_task": check_task,
    "analyze_data": analyze_data,
    "office": office_doc,
    "design": design_tool,
    "make_chart": make_chart,
    "spawn_agents": spawn_agents,
    "set_goal": set_goal,
    "list_goals": list_goals,
    "run_bash": run_bash,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "knowledge_search": knowledge_search,
    "generate_image": generate_image,
    "get_weather": get_weather,
    "get_exchange_rate": get_exchange_rate,
}


# 远程访客白名单。只在 schema 层过滤工具是不够的：模型照样能生成
# 白名单外的 tool call，服务端照跑（评测 T10/C7：set_goal 写进了主人的
# 目标库）。这里在**执行前**再兜一层，覆盖所有调用 tools.call 的路径，
# 包括 main.py 里 asyncio.to_thread 的并行只读分支。
# ContextVar 按请求 task 隔离；to_thread 会 copy_context，所以并行分支同样生效。
_VISITOR_ALLOWED: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "ms_visitor_allowed", default=None)


def set_visitor_allowed(names: set[str] | None) -> None:
    """本次请求限定可用工具；None = 本机请求，不限制。"""
    _VISITOR_ALLOWED.set(frozenset(names) if names is not None else None)


def call(name: str, args: dict) -> str:
    """执行工具。异常一律转成字符串返回给模型，不让它中断整个流程。"""
    allowed = _VISITOR_ALLOWED.get()
    if allowed is not None and name not in allowed:
        return f"[拒绝] 工具 {name} 对远程访客不可用（只读白名单）"
    fn = _HANDLERS.get(name)
    if not fn:
        return f"[错误] 没有这个工具：{name}"
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return f"[错误] 参数不对：{e}"
    except ValueError as e:
        return f"[拒绝] {e}"
    except Exception as e:                                # noqa: BLE001
        return f"[错误] {type(e).__name__}: {e}"
