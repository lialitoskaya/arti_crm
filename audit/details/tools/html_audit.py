"""Read-only HTML audit helper. Usage: html_audit.py <index.html> <app.js> <out.json>"""
import json
import re
import sys
from collections import Counter, defaultdict
from html.parser import HTMLParser

html_path, js_path, out_path = sys.argv[1:4]
html = open(html_path, encoding="utf-8").read()
js = open(js_path, encoding="utf-8").read()
lines = html.splitlines()


def ln(pos):
    return html.count("\n", 0, pos) + 1


out = {}

# ---- inline SVG duplication ----
svgs = []
for m in re.finditer(r"<svg\b[^>]*>.*?</svg>", html, re.S):
    body = m.group(0)
    ds = tuple(re.findall(r'\sd="([^"]+)"', body))
    other = tuple(re.findall(r"<(circle|rect|line|polyline|polygon|ellipse)\b[^>]*>", body))
    key = (ds, other)
    svgs.append((ln(m.start()), key, len(body)))
svg_groups = defaultdict(list)
for l, key, size in svgs:
    svg_groups[key].append(l)
out["svg_total"] = len(svgs)
out["svg_unique"] = len(svg_groups)
out["svg_bytes_total"] = sum(s[2] for s in svgs)
out["svg_groups"] = sorted([(len(v), v, (k[0][0][:50] if k[0] else str(k[1])[:50])) for k, v in svg_groups.items()], key=lambda x: -x[0])
# svg in app.js templates
out["svg_in_js"] = len(re.findall(r"<svg\b", js))
out["svg_in_js_lines"] = [js.count("\n", 0, m.start()) + 1 for m in re.finditer(r"<svg\b", js)][:40]

# ---- emoji in select#chatStatusColor ----
m = re.search(r'<select id="chatStatusColor"[^>]*>(.*?)</select>', html, re.S)
out["chatStatusColor"] = {"line": ln(m.start()), "options": re.findall(r"<option[^>]*>([^<]*)</option>", m.group(1))} if m else None
# emoji anywhere (basic ranges)
emoji_re = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF]")
out["emoji_lines_html"] = [(i + 1, l.strip()[:100]) for i, l in enumerate(lines) if emoji_re.search(l)]
out["emoji_count_js"] = len(emoji_re.findall(js))
out["emoji_lines_js"] = [(js.count("\n", 0, m.start()) + 1, js[max(0, m.start() - 40):m.start() + 20].replace("\n", " ")) for m in emoji_re.finditer(js)][:40]

# ---- unicode glyph buttons ----
glyphs = ["×", "←", "✕", "⋯", "＋", "✦", "✎", "→", "↻", "⟳", "✓", "✔", "…", "•", "▸", "▾", "▴", "⚙", "☰"]
gl = {}
for g in glyphs:
    hl = [i + 1 for i, l in enumerate(lines) if g in l]
    jl = [js.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(g), js)]
    if hl or jl:
        gl[g] = {"html": hl, "js": jl[:30], "js_count": len(jl)}
out["glyphs"] = gl

# ---- aria-hidden ----
out["aria_hidden"] = [(i + 1, l.strip()[:120]) for i, l in enumerate(lines) if "aria-hidden" in l]

# ---- logout buttons ----
out["logout"] = [(i + 1, l.strip()[:140]) for i, l in enumerate(lines) if re.search(r"logoutBtn|legacy-", l)]

# ---- hidden inputs ----
out["hidden_inputs"] = [(i + 1, l.strip()[:140]) for i, l in enumerate(lines) if re.search(r'type="hidden"', l)]

# ---- version strings ----
out["versions"] = [(i + 1, re.findall(r"\?v=[\w.-]+", l), l.strip()[:120]) for i, l in enumerate(lines) if "?v=" in l or "build-version" in l]

# ---- headings ----
heads = []
for m in re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", html, re.S):
    heads.append((ln(m.start()), int(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2)).strip()[:60]))
out["headings"] = heads
out["heading_counts"] = Counter(h[1] for h in heads)
# skips: next heading deeper by >1
skips = []
prev = None
for h in heads:
    if prev and h[1] > prev[1] + 1:
        skips.append((prev, h))
    prev = h
out["heading_skips"] = skips
# per main view: first heading level
views = [(ln(m.start()), m.group(1)) for m in re.finditer(r'<main id="(\w+)"', html)]
out["views"] = views


# ---- inputs without labels ----
class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.controls = []  # (line, tag, attrs, inside_label)
        self.label_for = set()
        self.ids = Counter()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids[a["id"]] += 1
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            self.controls.append((self.getpos()[0], tag, a, "label" in self.stack, "button" in self.stack))
        if tag not in ("input", "br", "img", "meta", "link", "hr", "source", "path", "circle", "rect", "line", "polyline", "polygon", "use"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            if self.stack:
                self.stack.pop()


p = P()
p.feed(html)
unlabeled = []
labeled = 0
for line, tag, a, in_label, _ in p.controls:
    if a.get("type") in ("hidden", "submit", "button", "checkbox") and tag == "input" and a.get("type") != "checkbox":
        continue
    has = in_label or a.get("aria-label") or a.get("aria-labelledby") or (a.get("id") in p.label_for) or a.get("title")
    if has:
        labeled += 1
    else:
        unlabeled.append((line, tag, a.get("id", ""), a.get("placeholder", "")[:40], a.get("type", "")))
out["controls_total"] = len([c for c in p.controls if not (c[1] == "input" and c[2].get("type") in ("hidden", "submit", "button"))])
out["controls_labeled"] = labeled
out["controls_unlabeled"] = unlabeled
out["duplicate_ids"] = [(k, v) for k, v in p.ids.items() if v > 1]
out["ids_total"] = sum(p.ids.values())

# ---- misc counts ----
out["inline_style_attrs"] = [(i + 1, l.strip()[:100]) for i, l in enumerate(lines) if 'style="' in l]
out["onclick_attrs"] = len(re.findall(r"\son\w+=", html))
out["buttons_total"] = len(re.findall(r"<button\b", html))
out["buttons_without_type"] = len([m for m in re.finditer(r"<button\b[^>]*>", html) if "type=" not in m.group(0)])
out["img_without_alt"] = [(ln(m.start()), m.group(0)[:100]) for m in re.finditer(r"<img\b[^>]*>", html) if "alt=" not in m.group(0)]
out["external_links"] = [(ln(m.start()), m.group(0)[:160]) for m in re.finditer(r"<a\b[^>]*target=\"_blank\"[^>]*>", html)]
out["script_tags"] = [(ln(m.start()), m.group(0)[:160]) for m in re.finditer(r"<script\b[^>]*>", html)]
out["link_tags"] = [(ln(m.start()), m.group(0)[:160]) for m in re.finditer(r"<link\b[^>]*>", html)]
out["meta_tags"] = [(ln(m.start()), m.group(0)[:160]) for m in re.finditer(r"<meta\b[^>]*>", html)]
out["hidden_class_elements"] = len(re.findall(r'class="[^"]*\bhidden\b', html))
out["tables"] = len(re.findall(r"<table\b", html))
out["role_attrs"] = Counter(re.findall(r'role="([^"]+)"', html)).most_common()
out["bytes"] = len(html.encode("utf-8"))
out["main_sizes"] = []
mains = [(m.start(), m.group(1)) for m in re.finditer(r'<main id="(\w+)"', html)]
for idx, (pos, name) in enumerate(mains):
    end = mains[idx + 1][0] if idx + 1 < len(mains) else len(html)
    seg = html[pos:end]
    close = seg.find("</main>")
    seg = seg[: close + 7] if close >= 0 else seg
    out["main_sizes"].append((name, ln(pos), ln(pos + len(seg)), len(seg.encode("utf-8"))))

json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
print(json.dumps({k: out[k] for k in ("svg_total", "svg_unique", "svg_bytes_total", "svg_in_js", "controls_total", "controls_labeled", "buttons_total", "buttons_without_type", "onclick_attrs", "ids_total", "bytes")}, ensure_ascii=False))
