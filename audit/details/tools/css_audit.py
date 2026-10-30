"""Read-only CSS audit helper. Usage: css_audit.py <styles.css> <index.html> <app.js> <out.json>"""
import json
import re
import sys
from collections import Counter, defaultdict

css_path, html_path, js_path, out_path = sys.argv[1:5]
src = open(css_path, encoding="utf-8").read()
html = open(html_path, encoding="utf-8").read()
js = open(js_path, encoding="utf-8").read()

# ---------- strip comments, keep newlines, collect column-0 headers ----------
headers = []  # (line, text)
masked = []
i = 0
n = len(src)
line = 1
while i < n:
    if src.startswith("/*", i):
        j = src.find("*/", i + 2)
        if j < 0:
            j = n - 2
        body = src[i + 2:j]
        # header = comment starting at column 0
        at_col0 = (i == 0) or src[i - 1] == "\n"
        if at_col0:
            first = body.strip().splitlines()[0].strip() if body.strip() else ""
            headers.append((line, first[:110]))
        seg = src[i:j + 2]
        masked.append(re.sub(r"[^\n]", " ", seg))
        line += seg.count("\n")
        i = j + 2
    else:
        c = src[i]
        masked.append(c)
        if c == "\n":
            line += 1
        i += 1
masked = "".join(masked)


def line_of(pos, _cache={}):
    return masked.count("\n", 0, pos) + 1


# precompute line starts for fast lookup
line_starts = [0]
for m in re.finditer("\n", masked):
    line_starts.append(m.end())
import bisect


def ln(pos):
    return bisect.bisect_right(line_starts, pos)


# ---------- parse rules with @media nesting ----------
rules = []  # dict(selector, start, end, media, decls=[(prop, val, important, line)])
atrules = []  # (name, prelude, line)
media_blocks = []  # (prelude, start_line, end_line)


def parse_block(pos, end, media):
    """parse declarations/rules between pos and end (exclusive)."""
    while pos < end:
        # skip whitespace and ;
        while pos < end and masked[pos] in " \t\r\n;":
            pos += 1
        if pos >= end:
            break
        # find next '{' or '}' at this level
        brace = masked.find("{", pos, end)
        if brace < 0:
            break
        prelude = masked[pos:brace].strip()
        # find matching close
        depth = 1
        k = brace + 1
        while k < end and depth:
            ch = masked[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            k += 1
        close = k - 1
        start_line = ln(pos)
        if prelude.startswith("@"):
            name = prelude.split(None, 1)[0]
            rest = prelude[len(name):].strip()
            atrules.append((name, rest, start_line))
            if name in ("@media", "@supports", "@layer", "@container"):
                if name == "@media":
                    media_blocks.append((rest, start_line, ln(close)))
                new_media = media + [prelude] if name == "@media" else media
                parse_block(brace + 1, close, new_media)
            elif name == "@keyframes" or name.startswith("@-"):
                pass
            elif name == "@font-face":
                pass
        else:
            body = masked[brace + 1:close]
            decls = []
            # split by ; respecting parentheses
            dpos = 0
            parts = []
            depth_p = 0
            cur_start = 0
            for idx, ch in enumerate(body):
                if ch == "(":
                    depth_p += 1
                elif ch == ")":
                    depth_p -= 1
                elif ch == ";" and depth_p == 0:
                    parts.append((cur_start, body[cur_start:idx]))
                    cur_start = idx + 1
            parts.append((cur_start, body[cur_start:]))
            for off, part in parts:
                if ":" not in part or not part.strip():
                    continue
                prop, val = part.split(":", 1)
                prop = prop.strip()
                val = val.strip()
                imp = False
                if re.search(r"!\s*important\s*$", val):
                    imp = True
                    val = re.sub(r"\s*!\s*important\s*$", "", val)
                # line of declaration: position of first non-space char in part
                lead = len(part) - len(part.lstrip())
                decls.append((prop, val, imp, ln(brace + 1 + off + lead)))
            rules.append({
                "selector": re.sub(r"\s+", " ", prelude),
                "start": start_line,
                "end": ln(close),
                "media": " && ".join(media),
                "decls": decls,
            })
        pos = close + 1


parse_block(0, len(masked), [])

# ---------- sections ----------
# Treat column-0 comment headers as section starts. Merge sub-headers that are very close? Keep all.
headers_sorted = sorted(headers)


def section_for(line_no):
    # last header with line <= line_no
    idx = bisect.bisect_right([h[0] for h in headers_sorted], line_no) - 1
    if idx < 0:
        return "(top)"
    h = headers_sorted[idx]
    return f"{h[0]}: {h[1]}"


# ---------- 1. selectors defined >= 3 times ----------
def split_selectors(sel):
    # split on commas at depth 0
    out, depth, cur = [], 0, ""
    for ch in sel:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


sel_occ = defaultdict(list)  # (selector, media) -> [lines]
sel_occ_any_media = defaultdict(list)
for r in rules:
    for s in split_selectors(r["selector"]):
        sel_occ[(s, r["media"])].append(r["start"])
        sel_occ_any_media[s].append((r["start"], r["media"]))

dup_selectors = sorted(
    [(s, m, lines) for (s, m), lines in sel_occ.items() if len(lines) >= 3],
    key=lambda x: -len(x[2]),
)
dup_selectors_any = sorted(
    [(s, v) for s, v in sel_occ_any_media.items() if len(v) >= 3],
    key=lambda x: -len(x[1]),
)

# ---------- selector+property pairs re-declared across sections ----------
pair_occ = defaultdict(list)  # (selector, media, prop) -> [(line, value, section)]
for r in rules:
    sec = section_for(r["start"])
    for s in split_selectors(r["selector"]):
        for prop, val, imp, dl in r["decls"]:
            pair_occ[(s, r["media"], prop)].append((dl, val, imp, sec))
pair_cross = []
for key, occ in pair_occ.items():
    secs = {o[3] for o in occ}
    if len(secs) >= 2 and len(occ) >= 2:
        pair_cross.append((key, occ, len(secs)))
pair_cross.sort(key=lambda x: (-len(x[1]), -x[2]))
# all pairs (same selector+media+property declared at >=2 different places, regardless of section)
pair_all = [(k, occ) for k, occ in pair_occ.items() if len(occ) >= 2]
pair_all.sort(key=lambda x: -len(x[1]))
CHAT_RE = re.compile(r"(\.layout|\.sidebar|\.conversation|\.chat-|\.messages|\.message|\.composer|\.extra-|\.reply-template|\.ai-|\.customer-|\.note-|\.task-create-|\.task-form|\.task-list|\.task-comment-|\.stats|\.scope-|\.mobile-back-btn|\.mobile-chat-|mobile-chat-open|#chatsView|\.waiting-|\.unread-|\.pin-|#chatStatus|#chatSubtitle|#extraMenuBtn|#assignedUserSelect|#messageForm|#replyTemplates|#aiGenerate)")
pair_all_chat = sum(1 for k, occ in pair_all if CHAT_RE.search(k[0]))
pair_all_overridden_decls = sum(len(occ) - 1 for k, occ in pair_all)
pair_all_nonchat_top = [(list(k), [(o[0], o[1][:28], o[2]) for o in occ]) for k, occ in pair_all if not CHAT_RE.search(k[0])][:50]
# selectors with most redeclared props (non-chat)
sel_pair_counter = Counter()
for k, occ in pair_all:
    if not CHAT_RE.search(k[0]):
        sel_pair_counter[(k[0], k[1])] += len(occ) - 1

# ---------- !important ----------
imp_by_section = Counter()
imp_by_prop = Counter()
imp_total = 0
imp_theme = 0
for r in rules:
    sec = section_for(r["start"])
    for prop, val, imp, dl in r["decls"]:
        if imp:
            imp_total += 1
            imp_by_section[sec] += 1
            imp_by_prop[prop] += 1
            if r["selector"].startswith("html[data-theme"):
                imp_theme += 1

# ---------- html[data-theme] rules ----------
theme_rules = [r for r in rules if "html[data-theme" in r["selector"]]
theme_prefixed = [r for r in rules if r["selector"].startswith("html[data-theme")]
base_props = defaultdict(dict)  # selector(no theme) -> {prop: set(values)}
base_sel_set = set()
for r in rules:
    if "html[data-theme" in r["selector"] or r["selector"].startswith("html:not"):
        continue
    for s in split_selectors(r["selector"]):
        base_sel_set.add(s)
        for prop, val, imp, dl in r["decls"]:
            base_props[s].setdefault(prop, set()).add(val)

theme_prefix_re = re.compile(r"^html\[data-theme(?:=[\"']?(light|dark)[\"']?)?\]\s*")
restate_only = []  # theme rule whose every property already exists in a base rule for same selector
restate_same_value = []
no_base_counterpart = []
partial = []
for r in theme_prefixed:
    sels = split_selectors(r["selector"])
    stripped = [theme_prefix_re.sub("", s).strip() for s in sels]
    stripped = [s for s in stripped if s]
    if not stripped:
        continue
    props = [d[0] for d in r["decls"]]
    if not props:
        continue
    has_base = any(s in base_props for s in stripped)
    if not has_base:
        no_base_counterpart.append(r)
        continue
    all_exist = all(any(p in base_props.get(s, {}) for s in stripped) for p in props)
    same_val = all(any(d[1] in base_props.get(s, {}).get(d[0], set()) for s in stripped) for d in r["decls"])
    if all_exist:
        restate_only.append(r)
        if same_val:
            restate_same_value.append(r)
    else:
        partial.append(r)

# ---------- colour literals outside token blocks ----------
TOKEN_RANGES = [(4, 47), (3272, 3349)]


def in_token_block(l):
    return any(a <= l <= b for a, b in TOKEN_RANGES)


color_re = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b|rgba?\([^)]*\)|hsla?\([^)]*\)")
color_occ = Counter()
color_lines = defaultdict(list)
for r in rules:
    for prop, val, imp, dl in r["decls"]:
        if in_token_block(dl):
            continue
        if val.startswith("var(") and "#" not in val and "rgb" not in val:
            continue
        for m in color_re.finditer(val):
            lit = m.group(0).lower().replace(" ", "")
            color_occ[lit] += 1
            color_lines[lit].append(dl)

# token values
tokens = {}
for r in rules:
    if r["selector"] in (":root",) or r["selector"].startswith("html[data-theme"):
        for prop, val, imp, dl in r["decls"]:
            if prop.startswith("--") and in_token_block(dl):
                key = f"{prop}@{r['selector']}"
                tokens[key] = val


def parse_color(lit):
    lit = lit.strip().lower()
    m = re.match(r"^#([0-9a-f]{3,8})$", lit)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        if len(h) in (6, 8):
            rr, gg, bb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
            return (rr, gg, bb, a)
        return None
    m = re.match(r"^rgba?\(([^)]*)\)$", lit)
    if m:
        parts = re.split(r"[,/\s]+", m.group(1).strip())
        parts = [p for p in parts if p]
        try:
            rr, gg, bb = [float(p.rstrip("%")) for p in parts[:3]]
            a = float(parts[3]) if len(parts) > 3 else 1.0
            return (rr, gg, bb, a)
        except Exception:
            return None
    return None


token_colors = []
for k, v in tokens.items():
    c = parse_color(v)
    if c:
        token_colors.append((k, v, c))


def nearest_token(lit):
    c = parse_color(lit)
    if not c:
        return ""
    best = None
    for k, v, tc in token_colors:
        d = sum((a - b) ** 2 for a, b in zip(c[:3], tc[:3])) ** 0.5 + abs(c[3] - tc[3]) * 60
        if best is None or d < best[0]:
            best = (d, k, v)
    if best and best[0] < 40:
        return f"{best[1]} = {best[2]} (Δ{best[0]:.0f})"
    return "-"


top_colors = [(lit, cnt, nearest_token(lit), sorted(color_lines[lit])[:6]) for lit, cnt in color_occ.most_common(40)]

# ---------- radius / shadow / font-size ----------
radius_vals = Counter()
radius_lines = defaultdict(list)
shadow_vals = Counter()
font_vals = Counter()
font_lines = defaultdict(list)
for r in rules:
    for prop, val, imp, dl in r["decls"]:
        if prop == "border-radius" or prop.endswith("-radius"):
            radius_vals[val] += 1
            radius_lines[val].append(dl)
        if prop == "box-shadow":
            shadow_vals[val] += 1
        if prop == "font-size":
            font_vals[val] += 1
            font_lines[val].append(dl)

# ---------- dead CSS ----------
class_re = re.compile(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)")
css_classes = defaultdict(list)
for r in rules:
    for s in split_selectors(r["selector"]):
        # skip anything inside :not( ... ) still counts as a reference; fine
        for m in class_re.finditer(s):
            css_classes[m.group(1)].append(r["start"])

html_classes = set()
for m in re.finditer(r'class="([^"]*)"', html):
    html_classes.update(m.group(1).split())
html_classes_any = html  # for raw search


def used_in(name):
    # whole-word match in html or js
    pat = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
    in_html = bool(pat.search(html))
    in_js = bool(pat.search(js))
    return in_html, in_js


dead = []
dynamic = []
for name, lines in sorted(css_classes.items()):
    in_html, in_js = used_in(name)
    if in_html or in_js:
        continue
    # dynamic? check a prefix (up to last '-') appears followed by ${ or concatenation in js
    parts = name.split("-")
    dyn = False
    for k in range(len(parts) - 1, 0, -1):
        prefix = "-".join(parts[:k]) + "-"
        if re.search(re.escape(prefix) + r"(\$\{|'\s*\+|\"\s*\+)", js):
            dyn = True
            break
    # also classList.add(name) with variables and `is-` / `has-` patterns
    (dynamic if dyn else dead).append((name, sorted(set(lines))[:8], len(lines)))

# named legacy/versioned classes
versioned = [(name, lines) for name, lines in css_classes.items() if re.search(r"(^legacy-|-v\d+$|-v\d+-|-v\d+\b)", name)]
# removed file references in comments
removed_refs = [(l, t) for l, t in headers if "mobile_chat_fix" in t]
removed_refs_all = [(ln(m.start()), src[m.start():m.start() + 80]) for m in re.finditer(r"mobile_chat_fix_v10\.js", src)]

# ---------- vendor / compat ----------
vendor = Counter()
vendor_lines = defaultdict(list)
transition_all = []
outline_none = []
backdrop = []
for r in rules:
    for prop, val, imp, dl in r["decls"]:
        if prop.startswith(("-webkit-", "-moz-", "-ms-", "-o-")):
            vendor[prop] += 1
            vendor_lines[prop].append(dl)
        if prop == "transition" and re.match(r"^all\b", val):
            transition_all.append((dl, r["selector"][:80]))
        if prop == "outline" and val in ("none", "0"):
            props_here = {d[0] for d in r["decls"]}
            repl = bool(props_here & {"box-shadow", "border", "border-color", "background", "background-color"})
            outline_none.append((dl, r["selector"][:90], repl))
        if prop in ("backdrop-filter", "-webkit-backdrop-filter"):
            backdrop.append((dl, r["selector"][:80], val))
    # prefixed selectors
    if "::-webkit-" in r["selector"] or ":-moz-" in r["selector"] or "::-moz-" in r["selector"] or ":-ms-" in r["selector"]:
        vendor["(selector) " + re.search(r"::?-(webkit|moz|ms)-[\w-]+", r["selector"]).group(0)] += 1

# ---------- @media breakpoints ----------
bp = Counter()
bp_lines = defaultdict(list)
for prelude, s, e in media_blocks:
    key = re.sub(r"\s+", " ", prelude)
    bp[key] += 1
    bp_lines[key].append(s)

# ---------- totals ----------
total_decls = sum(len(r["decls"]) for r in rules)

out = {
    "totals": {
        "rules": len(rules),
        "declarations": total_decls,
        "headers": len(headers),
        "selectors_distinct": len(sel_occ_any_media),
        "class_names_distinct": len(css_classes),
        "media_blocks": len(media_blocks),
        "important_total": imp_total,
        "important_in_theme_rules": imp_theme,
        "theme_rules_containing_prefix": len(theme_rules),
        "theme_rules_prefixed": len(theme_prefixed),
        "theme_restate_only": len(restate_only),
        "theme_restate_same_value": len(restate_same_value),
        "theme_no_base_counterpart": len(no_base_counterpart),
        "theme_partial": len(partial),
        "colour_literal_occurrences": sum(color_occ.values()),
        "colour_literal_distinct": len(color_occ),
        "class_dead_likely": len(dead),
        "class_dynamic": len(dynamic),
    },
    "headers": headers,
    "dup_selectors": [(s, m, lines) for s, m, lines in dup_selectors[:60]],
    "dup_selectors_any_media": [(s, v) for s, v in dup_selectors_any[:60]],
    "dup_count_ge3": len(dup_selectors),
    "dup_count_ge3_any_media": len(dup_selectors_any),
    "pair_cross": [(list(k), occ, ns) for k, occ, ns in pair_cross[:60]],
    "pair_cross_total": len(pair_cross),
    "pair_all_total": len(pair_all),
    "pair_all_chat": pair_all_chat,
    "pair_all_overridden_decls": pair_all_overridden_decls,
    "pair_all_nonchat_top": pair_all_nonchat_top,
    "sel_pair_nonchat_top": [(list(k), n) for k, n in sel_pair_counter.most_common(40)],
    "important_by_section": imp_by_section.most_common(),
    "important_by_prop": imp_by_prop.most_common(30),
    "theme_restate_same_value_examples": [(r["start"], r["selector"][:100], [d[0] for d in r["decls"]]) for r in restate_same_value[:40]],
    "theme_restate_only_examples": [(r["start"], r["selector"][:100], [d[0] for d in r["decls"]]) for r in restate_only[:40]],
    "theme_no_base_examples": [(r["start"], r["selector"][:100]) for r in no_base_counterpart[:30]],
    "top_colors": top_colors,
    "token_colors": [(k, v) for k, v, c in token_colors],
    "radius": [(v, c, radius_lines[v][:5]) for v, c in radius_vals.most_common()],
    "shadow": shadow_vals.most_common(40),
    "shadow_distinct": len(shadow_vals),
    "font_size": [(v, c, font_lines[v][:4]) for v, c in font_vals.most_common()],
    "dead": dead,
    "dynamic": dynamic,
    "versioned": versioned,
    "removed_refs": removed_refs_all,
    "vendor": [(p, c, vendor_lines[p][:6]) for p, c in vendor.most_common()],
    "transition_all": transition_all,
    "outline_none": outline_none,
    "backdrop": backdrop,
    "breakpoints": [(k, c, bp_lines[k][:12]) for k, c in bp.most_common()],
    "atrules": Counter(a[0] for a in atrules).most_common(),
}
json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(out["totals"], ensure_ascii=False, indent=1))
