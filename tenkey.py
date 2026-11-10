#!/usr/bin/env python3
"""tenkey —— 终端里的数字键打字练习。

纯标准库实现（curses），无需安装依赖：
    python3 tenkey.py            # 打开菜单
    python3 tenkey.py -m timed   # 直接开始限时挑战
    python3 tenkey.py --help     # 查看全部参数
成绩保存在 ~/.tenkey/stats.json（可用环境变量 TENKEY_HOME 改目录）。
"""
import argparse
import curses
import json
import locale
import os
import random
import sys
import time
import unicodedata
from pathlib import Path

DATA_FILE = Path(os.environ.get("TENKEY_HOME", Path.home() / ".tenkey")) / "stats.json"
DIGITS = "1234567890"
SEP = "  "          # 组与组之间的分隔，会被自动跳过，不需要按空格
VIS_LINES = 4       # 练习区同时显示的行数
TICK_MS = 100

ROW_FINGER = {"1": "左手小指", "2": "左手无名指", "3": "左手中指", "4": "左手食指", "5": "左手食指",
              "6": "右手食指", "7": "右手食指", "8": "右手中指", "9": "右手无名指", "0": "右手小指",
              "-": "右手小指", ".": "右手无名指", ":": "右手小指 + 左手小指按 Shift"}
PAD_FINGER = {"7": "右手食指", "4": "右手食指", "1": "右手食指", "8": "右手中指", "5": "右手中指",
              "2": "右手中指", "9": "右手无名指", "6": "右手无名指", "3": "右手无名指", "0": "右手拇指",
              ".": "右手无名指", "-": "右手小指", ":": "主键盘：右手小指 + Shift"}
PAD_ROWS = [["7", "8", "9"], ["4", "5", "6"], ["1", "2", "3"], ["0", "."]]

MODES = [  # 菜单键, 模式 id, 名称, 说明
    ("1", "basic", "基础练习", "0-9 随机数字组"),
    ("2", "custom", "专项练习", "只练你挑的几个数字"),
    ("3", "weak", "弱项强化", "按历史错误率和反应速度加权出题"),
    ("4", "scene", "实战数据", "手机号 / 金额 / 日期 / IP / 身份证…"),
    ("5", "timed", "限时挑战", "{t} 秒内能打多少"),
    ("6", "stats", "统计数据", "历史成绩与键位热力图"),
]
MODE_NAME = {m: n for _, m, n, _ in MODES}

# 小键盘在“应用模式”下会发送 ESC O x 序列，这里翻译回普通字符
PAD_SEQ = {"p": "0", "q": "1", "r": "2", "s": "3", "t": "4", "u": "5", "v": "6", "w": "7",
           "x": "8", "y": "9", "n": ".", "M": "\n", "k": "+", "m": "-", "j": "*", "o": "/", "X": "="}
ARROWS = {"A": curses.KEY_UP, "B": curses.KEY_DOWN, "C": curses.KEY_RIGHT, "D": curses.KEY_LEFT,
          "H": curses.KEY_HOME, "F": curses.KEY_END}
KEYMAP = {curses.KEY_A1: "7", curses.KEY_A3: "9", curses.KEY_B2: "5", curses.KEY_C1: "1",
          curses.KEY_C3: "3", curses.KEY_ENTER: "\n", curses.KEY_BACKSPACE: "\b"}

C = {}  # 颜色/属性表，init_colors() 里填充


# ───────────────────────── 数据 ─────────────────────────

class Stats:
    def __init__(self):
        self.data = {"sessions": [], "keys": {}, "custom": "456", "pad": False}
        try:
            self.data.update(json.loads(DATA_FILE.read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(DATA_FILE)

    def key(self, k):
        s = self.data["keys"].setdefault(k, {})
        for f in ("hit", "miss", "ms", "tn"):
            s.setdefault(f, 0)
        return s

    def record(self, mode, res):
        for k, (hit, miss, ms, tn) in res["keys"].items():
            s = self.key(k)
            s["hit"] += hit
            s["miss"] += miss
            s["ms"] += round(ms)
            s["tn"] += tn
        self.data["sessions"].append({
            "t": time.strftime("%Y-%m-%d %H:%M"), "mode": mode, "cpm": round(res["cpm"], 1),
            "acc": round(res["acc"], 1), "n": res["done"], "sec": round(res["sec"], 1)})
        self.data["sessions"] = self.data["sessions"][-1000:]
        self.save()

    def avg_ms(self, k):
        s = self.data["keys"].get(k, {})
        return s["ms"] / s["tn"] if s.get("tn") else None

    def err_rate(self, k):
        s = self.data["keys"].get(k, {})
        tot = s.get("hit", 0) + s.get("miss", 0)
        return (s.get("miss", 0) / tot, tot) if tot else (None, 0)

    def weights(self):
        """错误率越高、反应越慢的数字，出现得越频繁。"""
        avgs = {d: self.avg_ms(d) for d in DIGITS if self.avg_ms(d)}
        mean = sum(avgs.values()) / len(avgs) if avgs else 0
        w = {}
        for d in DIGITS:
            err, tot = self.err_rate(d)
            slow = max(0.0, avgs[d] / mean - 1) if d in avgs and mean else 0
            w[d] = 1 + (err or 0) * 30 + slow * 4 + (1 if tot < 20 else 0)
        return w

    def best(self, mode):
        return max((s["cpm"] for s in self.data["sessions"] if s["mode"] == mode and s["acc"] >= 90),
                   default=0)


# ───────────────────────── 出题 ─────────────────────────

def rd(n):
    return "".join(random.choices(DIGITS, k=n))


def gen_groups(chars, n, size, weights=None):
    pool = list(chars)
    wts = [weights.get(c, 1) for c in pool] if weights else None
    return ["".join(random.choices(pool, wts, k=size)) for _ in range(n)]


SCENES = [
    ("手机号", lambda: "1" + random.choice("3456789") + rd(9)),
    ("金额", lambda: f"{random.randint(1, 99999)}.{rd(2)}"),
    ("日期", lambda: f"{random.randint(1990, 2035)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"),
    ("时间", lambda: f"{random.randint(0, 23):02d}:{random.randint(0, 59):02d}"),
    ("IP", lambda: ".".join(str(random.randint(1, 254)) for _ in range(4))),
    ("验证码", lambda: rd(6)),
    ("身份证", lambda: random.choice(["110", "310", "440", "330", "510", "420"]) + rd(3)
     + f"{random.randint(1960, 2012)}{random.randint(1, 12):02d}{random.randint(1, 28):02d}" + rd(4)),
    ("银行卡", lambda: "62" + rd(2) + " " + rd(4) + " " + rd(4) + " " + rd(4)),
    ("小数", lambda: f"{random.randint(0, 999)}.{rd(random.randint(1, 3))}"),
]


def make_items(ctx, mode, keys=None):
    size, n = ctx["size"], ctx["groups"]
    st = ctx["st"]
    if mode == "basic":
        return "基础练习", gen_groups(DIGITS, n, size), None
    if mode == "custom":
        return f"专项练习 · {' '.join(keys)}", gen_groups(keys, n, size), None
    if mode == "weak":
        return "弱项强化", gen_groups(DIGITS, n, size, st.weights()), None
    if mode == "scene":
        pool = [s for s in SCENES if not (ctx["pad"] and s[0] == "时间")]  # 小键盘没有冒号
        return "实战数据", [f() for _, f in random.choices(pool, k=max(5, n * size // 9))], None
    if mode == "timed":
        return f"限时挑战 · {ctx['time']} 秒", gen_groups(DIGITS, 400, size), ctx["time"]
    raise ValueError(mode)


def layout(items, width):
    """为 SEP.join(items) 的每个字符计算 (行, 列)，按组整体换行。"""
    pos, line, col = [], 0, 0
    for i, it in enumerate(items):
        if i:
            pos += [(line, col + j) for j in range(len(SEP))]
            col += len(SEP)
            if col + len(it) > width:
                line, col = line + 1, 0
        for _ in it:
            pos.append((line, col))
            col += 1
    return pos


# ───────────────────────── 绘制工具 ─────────────────────────

def dw(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def put(scr, y, x, s, attr=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            scr.addstr(y, x, s, attr)
        except curses.error:
            pass


def center(scr, y, s, attr=0):
    put(scr, y, max(0, (scr.getmaxyx()[1] - dw(s)) // 2), s, attr)


def header(scr, title, right=""):
    w = scr.getmaxyx()[1]
    put(scr, 0, 0, " " * (w - 1), C["title"])
    put(scr, 0, 1, f"tenkey · {title}", C["title"])
    if right:
        put(scr, 0, max(0, w - dw(right) - 2), right, C["title"])


def footer(scr, s):
    center(scr, scr.getmaxyx()[0] - 2, s, C["dim"])


def too_small(scr):
    h, w = scr.getmaxyx()
    if h >= 22 and w >= 50:
        return False
    scr.erase()
    center(scr, h // 2, f"终端太小（{w}x{h}），请放大到至少 50x22")
    scr.refresh()
    return True


def init_colors():
    C.update(ok=curses.A_BOLD, err=curses.A_REVERSE | curses.A_BOLD, fix=curses.A_UNDERLINE,
             cur=curses.A_REVERSE, dim=curses.A_DIM, key=curses.A_REVERSE, h0=curses.A_REVERSE,
             h1=curses.A_REVERSE | curses.A_UNDERLINE, h2=curses.A_REVERSE | curses.A_BOLD,
             title=curses.A_REVERSE, acc=curses.A_BOLD, zcur=curses.A_BOLD | curses.A_UNDERLINE)
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    gray = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
    spec = {"ok": (curses.COLOR_GREEN, bg), "err": (curses.COLOR_WHITE, curses.COLOR_RED),
            "fix": (curses.COLOR_YELLOW, bg), "cur": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "dim": (gray, bg), "key": (curses.COLOR_BLACK, curses.COLOR_WHITE),
            "h0": (curses.COLOR_BLACK, curses.COLOR_GREEN), "h1": (curses.COLOR_BLACK, curses.COLOR_YELLOW),
            "h2": (curses.COLOR_WHITE, curses.COLOR_RED), "title": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "acc": (curses.COLOR_CYAN, bg), "zcur": (curses.COLOR_CYAN, bg)}
    bold = {"ok", "err", "cur", "acc", "h2", "zcur"}
    for i, (k, (f, b)) in enumerate(spec.items(), 1):
        curses.init_pair(i, f, b)
        C[k] = curses.color_pair(i) | (curses.A_BOLD if k in bold else 0)
    C["zcur"] |= curses.A_UNDERLINE


def heat_level(miss, total, warn, bad):
    if not total:
        return None
    r = miss / total
    return "h0" if r <= warn else ("h1" if r <= bad else "h2")


def draw_kb(scr, y, pad, hi=None, heat=None, sel=None):
    """画键盘，返回占用的行数。hi=高亮的下一个键；heat={键: h0/h1/h2}；sel=已选中的键集合。"""
    w = scr.getmaxyx()[1]

    def attr(k):
        if k == hi:
            return C["cur"]
        if sel is not None:
            return C["h0"] if k in sel else C["key"] | curses.A_DIM
        if heat and heat.get(k):
            return C[heat[k]]
        return C["key"]

    if pad:
        for r, row in enumerate(PAD_ROWS):
            x = (w - 17) // 2
            for k in row:
                kw = 11 if k == "0" else 5
                put(scr, y + r, x, k.center(kw), attr(k))
                x += kw + 1
        return len(PAD_ROWS)
    x = (w - 39) // 2
    for k in DIGITS:
        put(scr, y, x, f" {k} ", attr(k))
        x += 4
    return 1


def read_key(scr):
    try:
        k = scr.get_wch()
    except curses.error:
        return None
    if isinstance(k, int):
        return KEYMAP.get(k, k)
    if k == "\r":
        return "\n"
    if k != "\x1b":
        return k
    scr.timeout(0)
    try:
        try:
            nxt = scr.get_wch()
        except curses.error:
            return "ESC"      # 单独的 ESC
        try:
            if nxt in ("O", "["):
                c = scr.get_wch()
                if c in ARROWS:   # 少数终端不按 terminfo 发送方向键，这里兜底识别
                    return ARROWS[c]
                if nxt == "O":
                    return PAD_SEQ.get(c)
            while True:       # 其它未识别的转义序列：读完丢弃
                scr.get_wch()
        except curses.error:
            return None
    finally:
        scr.timeout(TICK_MS)


def wait_choice(scr, draw, keys):
    while True:
        if not too_small(scr):
            scr.erase()
            draw()
            scr.refresh()
        k = read_key(scr)
        if isinstance(k, str) and k.lower() in keys:
            return k.lower()


# ───────────────────────── 练习 ─────────────────────────

def run_session(scr, ctx, title, items, limit=None):
    text = SEP.join(items)
    finger = PAD_FINGER if ctx["pad"] else ROW_FINGER
    n_total = sum(c != " " for c in text)
    err_pos = set()
    sk = {}                     # 键 -> [正确次数, 按错次数, 累计反应毫秒, 计时次数]
    strokes = errors = done = 0
    start = last = None
    flash = (0.0, "")
    cache_w, pmap = None, None
    timed_out = False

    def skip(p):
        while p < len(text) and text[p] == " ":
            p += 1
        return p

    pos = skip(0)
    while True:
        now = time.time()
        if pos >= len(text):
            break
        if limit and start and now - start >= limit:
            timed_out = True
            break
        if not too_small(scr):
            h, w = scr.getmaxyx()
            tw = min(w - 6, 60)
            if tw != cache_w:
                cache_w, pmap = tw, layout(items, tw)
            x0 = (w - tw) // 2
            zen = ctx["zen"]
            scr.erase()
            cl = pmap[pos][0]
            first = max(0, cl - 1)
            erring = now - flash[0] < 0.35
            el = now - start if start else 0
            # 禅模式：只留下文字本身，整体垂直居中；详细模式保留原有布局
            vis = 3 if zen else VIS_LINES
            ty = max(1, (h - vis * 2) // 2 - 1) if zen else 3
            if not zen:
                header(scr, title, "ESC 放弃 · Tab 禅模式")
            for i, ch in enumerate(text):
                if ch == " ":
                    continue
                ln, col = pmap[i]
                if ln < first:
                    continue
                if ln >= first + vis:
                    break
                if i < pos:
                    if zen:
                        a = C["fix"] | curses.A_DIM if i in err_pos else C["dim"]
                    else:
                        a = C["fix"] if i in err_pos else C["ok"]
                elif i == pos:
                    a = C["err"] if erring else (C["zcur"] if zen else C["cur"])
                else:
                    a = 0 if ln == cl else C["dim"]
                put(scr, ty + (ln - first) * 2, x0 + col, ch, a)

            if zen:
                # 一条极淡的进度线：限时模式按时间，其它模式按完成字数
                frac = min(1.0, el / limit) if limit else done / n_total
                fill = round(tw * frac)
                py = ty + vis * 2
                put(scr, py, x0, "─" * fill, C["dim"])
                put(scr, py, x0 + fill, "·" * (tw - fill), C["dim"] | curses.A_DIM)
                if not start:
                    center(scr, h - 2, "敲下第一个数字开始  ·  Tab 显示提示  ·  Esc 返回", C["dim"])
                scr.refresh()
            else:
                cpm = done * 60 / el if el > 1 else 0
                acc = (strokes - errors) / strokes * 100 if strokes else 100
                prog = f"剩余 {max(0, limit - el):4.1f} 秒" if limit else f"进度 {done}/{n_total}"
                center(scr, 4 + VIS_LINES * 2, f"速度 {cpm:4.0f} 字/分    准确率 {acc:5.1f}%    {prog}", C["acc"])

                nxt = text[pos]
                ky = 6 + VIS_LINES * 2
                rows = draw_kb(scr, ky, ctx["pad"], hi=nxt)
                if erring:
                    center(scr, ky + rows + 1, f"按错了：你按的是 {flash[1]!r}，应为 {nxt}", C["err"])
                else:
                    center(scr, ky + rows + 1, f"下一个  {nxt}   ·   {finger.get(nxt, '')}", C["dim"])
                if not start:
                    footer(scr, "开始敲第一个数字即开始计时；分隔空格会自动跳过，按错不会前进")
                scr.refresh()

        k = read_key(scr)
        if k is None:
            continue
        if k == "ESC":
            return None
        if k == "\t":           # 切换禅模式 / 详细模式，并记住选择
            ctx["zen"] = not ctx["zen"]
            ctx["st"].data["zen"] = ctx["zen"]
            ctx["st"].save()
            continue
        if not (isinstance(k, str) and len(k) == 1 and k.isprintable()) or k == " ":
            continue
        now = time.time()
        if start is None:
            start = last = now
        strokes += 1
        exp = text[pos]
        s = sk.setdefault(exp, [0, 0, 0.0, 0])
        if k == exp:
            s[0] += 1
            if done:  # 第一个键没有“上一键”，不计反应时间；超过 3 秒的停顿按 3 秒算
                s[2] += min(now - last, 3.0) * 1000
                s[3] += 1
            last = now
            done += 1
            pos = skip(pos + 1)
        else:
            errors += 1
            s[1] += 1
            err_pos.add(pos)
            flash = (now, k)

    if not done:
        return None
    sec = limit if timed_out else last - start
    return {"cpm": done * 60 / sec if sec > 0.2 else 0, "acc": (strokes - errors) / strokes * 100,
            "done": done, "errors": errors, "sec": sec, "keys": sk}


def show_result(scr, ctx, mode, res):
    st = ctx["st"]
    hist = [s for s in st.data["sessions"] if s["mode"] == mode]
    best_prev = st.best(mode)
    recent = hist[-10:]
    avg_prev = sum(s["cpm"] for s in recent) / len(recent) if recent else None
    st.record(mode, res)
    new_best = res["acc"] >= 90 and res["cpm"] > best_prev and hist
    heat = {k: heat_level(v[1], v[0] + v[1], 0, 0.15) for k, v in res["keys"].items()}
    slow = sorted(((v[2] / v[3], k) for k, v in res["keys"].items() if v[3]), reverse=True)[:3]
    errs = sorted(((v[1], k) for k, v in res["keys"].items() if v[1]), reverse=True)[:3]

    def draw():
        header(scr, f"{MODE_NAME[mode]} · 本轮成绩")
        cmp = []
        if best_prev:
            cmp.append(f"本模式最佳 {best_prev:.0f}")
        if avg_prev:
            cmp.append(f"近 {len(recent)} 轮平均 {avg_prev:.0f}")
        lines = [(f"速度    {res['cpm']:6.0f} 字/分", "   " + " · ".join(cmp)),
                 (f"准确率  {res['acc']:6.1f} %", f"   按错 {res['errors']} 次"),
                 (f"用时    {res['sec']:6.1f} 秒", f"   共 {res['done']} 个字符")]
        x = max(2, (scr.getmaxyx()[1] - 52) // 2)
        for i, (a, b) in enumerate(lines):
            put(scr, 3 + i, x, a, C["acc"])
            put(scr, 3 + i, x + dw(a), b, C["dim"])
        if new_best:
            center(scr, 7, "★ 新纪录！ ★", C["ok"])
        elif res["acc"] < 90:
            center(scr, 7, "准确率低于 90%，先求准再求快", C["fix"])
        center(scr, 9, "本轮键位：绿=全对  黄=偶有失误  红=错得多  白=未出现", C["dim"])
        rows = draw_kb(scr, 11, ctx["pad"], heat=heat)
        y = 12 + rows
        if errs:
            center(scr, y, "错得最多：" + "   ".join(f"{k} ×{n}" for n, k in errs))
            y += 1
        if slow:
            center(scr, y, "反应最慢：" + "   ".join(f"{k} {ms:.0f}ms" for ms, k in slow))
        footer(scr, "Enter 再来一轮    M 返回菜单    Q 退出")

    k = wait_choice(scr, draw, {"\n", "m", "q", "esc"})
    return {"\n": "again", "m": "menu", "esc": "menu", "q": "quit"}[k]


def pick_keys(scr, ctx):
    st = ctx["st"]
    sel = set(st.data.get("custom", "456")) & set(DIGITS)
    while True:
        if not too_small(scr):
            scr.erase()
            header(scr, "专项练习 · 选择数字", "ESC 返回")
            center(scr, 3, "按数字键选中 / 取消，选好后按 Enter 开始")
            center(scr, 4, "建议每次 2~4 个键，熟练后再逐步加入相邻键", C["dim"])
            draw_kb(scr, 7, ctx["pad"], sel=sel)
            chosen = "".join(d for d in DIGITS if d in sel)
            center(scr, 13, f"已选：{' '.join(chosen) or '（无）'}", C["acc"])
            scr.refresh()
        k = read_key(scr)
        if k == "ESC":
            return None
        if isinstance(k, str) and k in DIGITS:
            sel ^= {k}
        elif k == "\n" and sel:
            st.data["custom"] = "".join(d for d in DIGITS if d in sel)
            st.save()
            return st.data["custom"]


def play(scr, ctx, mode):
    keys = None
    if mode == "custom":
        keys = pick_keys(scr, ctx)
        if not keys:
            return "menu"
    while True:
        title, items, limit = make_items(ctx, mode, keys)
        res = run_session(scr, ctx, title, items, limit)
        if res is None:
            return "menu"
        act = show_result(scr, ctx, mode, res)
        if act != "again":
            return act


def show_stats(scr, ctx):
    st = ctx["st"]
    ss = st.data["sessions"]

    def draw():
        header(scr, "统计数据", "任意键返回")
        if not ss:
            center(scr, 5, "还没有练习记录，先去练一轮吧")
            return
        tot_n = sum(s["n"] for s in ss)
        tot_t = sum(s["sec"] for s in ss) / 60
        r = ss[-10:]
        center(scr, 2, f"累计 {len(ss)} 轮 · {tot_n} 个字符 · {tot_t:.0f} 分钟")
        center(scr, 3, f"近 {len(r)} 轮平均  {sum(s['cpm'] for s in r) / len(r):.0f} 字/分 · "
                       f"准确率 {sum(s['acc'] for s in r) / len(r):.1f}%", C["acc"])
        bests = [f"{MODE_NAME[m]} {st.best(m):.0f}" for _, m, _, _ in MODES if m != "stats" and st.best(m)]
        if bests:
            center(scr, 4, "最佳：" + "  ".join(bests), C["dim"])

        heat = {}
        for k in DIGITS + ".":
            err, tot = st.err_rate(k)
            heat[k] = heat_level(err * tot, tot, 0.02, 0.06) if tot else None
        center(scr, 6, "历史错误率：绿 ≤2%  黄 ≤6%  红 >6%", C["dim"])
        y = 8 + draw_kb(scr, 8, ctx["pad"], heat=heat)

        w = scr.getmaxyx()[1]
        cols = [k for k in DIGITS + ".-:" if st.data["keys"].get(k)][:max(1, (w - 12) // 6)]
        x = max(1, (w - 10 - 6 * len(cols)) // 2)
        for i, (lab, fn) in enumerate([
                ("键", lambda k: k),
                ("次数", lambda k: str(st.key(k)["hit"] + st.key(k)["miss"])),
                ("错误率", lambda k: f"{st.err_rate(k)[0] * 100:.1f}"),
                ("反应ms", lambda k: f"{st.avg_ms(k):.0f}" if st.avg_ms(k) else "-")]):
            put(scr, y + 1 + i, x, lab, C["dim"] if i else C["acc"])
            for j, k in enumerate(cols):
                put(scr, y + 1 + i, x + 10 + j * 6, fn(k).rjust(5), C["acc"] if i == 0 else 0)

        y += 7
        put(scr, y, x, "最近几轮", C["dim"])
        for i, s in enumerate(reversed(ss[-min(6, scr.getmaxyx()[0] - y - 4):])):
            put(scr, y + 1 + i, x, f"{s['t']}  {MODE_NAME.get(s['mode'], s['mode']):<6}"
                                   f"{s['cpm']:6.0f} 字/分  {s['acc']:5.1f}%  {s['n']:4d} 字")

    scr.erase()
    draw()
    scr.refresh()
    while read_key(scr) is None:
        if not too_small(scr):
            scr.erase()
            draw()
            scr.refresh()


MENU_DOWN = {"j", curses.KEY_DOWN, "\t", "\x0e"}       # j / ↓ / Tab / Ctrl-N
MENU_UP = {"k", curses.KEY_UP, curses.KEY_BTAB, "\x10"}  # k / ↑ / Shift-Tab / Ctrl-P
MENU_TOP = {"g", curses.KEY_HOME}
MENU_BOTTOM = {"G", curses.KEY_END}
MENU_OK = {"\n", "l", " ", curses.KEY_RIGHT}


def menu(scr, ctx):
    """主菜单。支持 vim 式 j/k 浏览、g/G 跳到首尾、l/Enter 确认，也可直接按菜单键。"""
    n_items = len(MODES) + 2  # + 键位提示切换 + 退出
    sel = ctx.get("menu_sel", 0)
    while True:
        if not too_small(scr):
            scr.erase()
            h, w = scr.getmaxyx()
            header(scr, "数字打字练习")
            center(scr, 2, "练 熟 数 字 键 · 盲 打 0 - 9", C["acc"])
            ss = ctx["st"].data["sessions"]
            if ss:
                r = ss[-10:]
                center(scr, 3, f"已练 {len(ss)} 轮 · 近期平均 {sum(s['cpm'] for s in r) / len(r):.0f} 字/分",
                       C["dim"])
            x = max(2, (w - 52) // 2)
            rows = [(k, n, d.format(t=ctx["time"])) for k, _, n, d in MODES]
            rows.append(("P", "键位提示", ("小键盘" if ctx["pad"] else "主键盘数字行") + " · 按 P 切换"))
            rows.append(("Q", "退出", ""))
            for i, (k, n, d) in enumerate(rows):
                y = 5 + i * 2
                on = i == sel
                put(scr, y, x, "▶" if on else " ", C["acc"])
                put(scr, y, x + 2, f" {k} ", C["cur"] if on else C["key"])
                put(scr, y, x + 7, n, C["acc"] if on else 0)
                put(scr, y, x + 17, d, 0 if on else C["dim"])
            footer(scr, "j/k 或 ↑↓ 移动   g/G 首/尾   l 或 Enter 确认   也可直接按菜单键")
            scr.refresh()

        k = read_key(scr)
        if k is None:
            continue
        if k in MENU_DOWN:
            sel = min(sel + 1, n_items - 1)
        elif k in MENU_UP:
            sel = max(sel - 1, 0)
        elif k in MENU_TOP:
            sel = 0
        elif k in MENU_BOTTOM:
            sel = n_items - 1
        elif isinstance(k, str) and len(k) == 1 and k in "123456":
            sel = int(k) - 1
            k = "\n"
        elif k in ("p", "P"):
            sel = len(MODES)
            k = "\n"
        elif k in ("ESC", "q", "Q"):
            return "quit"
        if k not in MENU_OK:
            continue
        ctx["menu_sel"] = sel      # 回到菜单时光标停在上次的位置
        if sel < len(MODES):
            return MODES[sel][1]
        if sel == len(MODES):      # 切换键位提示布局
            ctx["pad"] = not ctx["pad"]
            ctx["st"].data["pad"] = ctx["pad"]
            ctx["st"].save()
        else:
            return "quit"


def app(scr, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    sys.stdout.write("\033>")  # 让小键盘发送普通数字（numeric keypad mode）
    sys.stdout.flush()
    scr.timeout(TICK_MS)
    init_colors()
    st = Stats()
    ctx = {"st": st, "size": args.size, "groups": args.groups, "time": args.time,
           "pad": args.pad or bool(st.data.get("pad")),
           "zen": st.data.get("zen", True) if args.zen is None else args.zen}
    mode = args.mode
    while True:
        if mode is None:
            mode = menu(scr, ctx)
        if mode == "quit":
            return
        if mode == "stats":
            show_stats(scr, ctx)
            act = "menu"
        else:
            act = play(scr, ctx, mode)
        if act == "quit":
            return
        mode = None


def main():
    ap = argparse.ArgumentParser(
        prog="tenkey", description="终端数字键打字练习", add_help=False,
        usage="%(prog)s [-m MODE] [-s N] [-n N] [-t SEC] [--pad] [--zen | --detail]",
        epilog="练习中按 Tab 切换禅模式 / 详细模式，Esc 放弃本轮。")
    ap._optionals.title = "参数"
    ap.add_argument("-h", "--help", action="help", help="显示帮助")
    ap.add_argument("-m", "--mode", metavar="MODE", choices=[m for _, m, _, _ in MODES],
                    help="直接进入：" + " / ".join(m for _, m, _, _ in MODES))
    ap.add_argument("-s", "--size", metavar="N", type=int, default=5, help="每组位数（默认 5）")
    ap.add_argument("-n", "--groups", metavar="N", type=int, default=12, help="每轮组数（默认 12）")
    ap.add_argument("-t", "--time", metavar="SEC", type=int, default=60, help="限时秒数（默认 60）")
    ap.add_argument("--pad", action="store_true", help="小键盘布局")
    view = ap.add_mutually_exclusive_group()
    view.add_argument("--zen", dest="zen", action="store_true", default=None, help="禅模式：只显示数字（默认）")
    view.add_argument("--detail", dest="zen", action="store_false", help="详细模式：显示速度、键盘和指法")
    args = ap.parse_args()
    if not 1 <= args.size <= 20 or not 1 <= args.groups <= 200 or not 5 <= args.time <= 3600:
        ap.error("参数超出范围：size 1-20，groups 1-200，time 5-3600")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(app, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
