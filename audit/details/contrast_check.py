"""WCAG 2.x contrast ratios for Arti CRM token pairs (light and dark theme layers from styles.css:3272-3349
and the v56 dark button tokens at 4818+). Pure computation, no project imports."""
from __future__ import annotations

import re


def parse(color: str) -> tuple[float, float, float, float]:
    color = color.strip()
    if color.startswith("#"):
        h = color[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return r, g, b, a
    m = re.match(r"rgba?\(([^)]+)\)", color)
    parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
    r, g, b = (float(parts[0]), float(parts[1]), float(parts[2]))
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return r, g, b, a


def composite(fg: str, bg: str) -> tuple[float, float, float]:
    r, g, b, a = parse(fg)
    br, bgc, bb, _ = parse(bg)
    return (r * a + br * (1 - a), g * a + bgc * (1 - a), b * a + bb * (1 - a))


def luminance(rgb: tuple[float, float, float]) -> float:
    def ch(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(fg: str, bg: str, base: str = "#ffffff") -> float:
    bg_rgb = composite(bg, base)
    bg_hex = "#%02x%02x%02x" % tuple(round(c) for c in bg_rgb)
    fg_rgb = composite(fg, bg_hex)
    l1, l2 = luminance(fg_rgb), luminance(bg_rgb)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


LIGHT = {
    "text": "#162033", "text-soft": "#3d4b63", "muted": "#7a879b", "muted-2": "#a0aabb",
    "primary": "#2563eb", "panel": "rgba(255,255,255,.92)", "panel-solid": "#ffffff", "panel-soft": "#f8fafd",
    "panel-muted": "#eef2f8", "card-active": "#f0f6ff", "nav": "rgba(255,255,255,.64)", "line": "rgba(130,145,170,.22)",
    "blue": "#e7f0ff", "blue-text": "#2458c6", "green": "#e9f8ef", "green-text": "#1f7a3d", "red": "#fdecee",
    "red-text": "#b42336", "orange": "#fff2df", "orange-text": "#a45512", "purple": "#f2e9ff", "purple-text": "#6c3bbd",
    "yellow": "#fff7d9", "yellow-text": "#886500",
}
DARK = {
    "text": "#edf3ff", "text-soft": "#f0f0f0", "muted": "#8b98ab", "muted-2": "#687487", "primary": "#63a2ff",
    "bg": "#0b0f17", "panel": "rgb(54 64 92)", "panel-solid": "#121826", "panel-soft": "#171f2e", "panel-muted": "#1e2738",
    "card": "rgba(18,24,38,.92)", "nav": "rgba(12,18,31,.62)", "button-bg": "#182234", "button-primary-bg": "#233b5f",
    "button-disabled-bg": "#161e2d", "button-disabled-text": "#738198",
    "blue": "rgba(69,124,255,.18)", "blue-text": "#9cc6ff", "green": "rgba(51,202,112,.16)", "green-text": "#87e6a8",
    "red": "rgba(255,96,110,.16)", "red-text": "#ff9ea8", "orange": "rgba(251,146,60,.16)", "orange-text": "#ffc08a",
    "purple": "rgba(168,85,247,.18)", "purple-text": "#d7b3ff", "yellow": "rgba(250,204,21,.16)", "yellow-text": "#fde68a",
}


def report(title: str, rows: list[tuple[str, str, str, str, str]]) -> None:
    print(f"\n## {title}")
    print("| Пара | fg | bg | Контраст | AA текст (4.5) | AA крупный/UI (3.0) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, fg, bg, base, note in rows:
        r = ratio(fg, bg, base)
        print(f"| {name}{(' — ' + note) if note else ''} | `{fg}` | `{bg}` | **{r:.2f}:1** | {'✅' if r >= 4.5 else '❌'} | {'✅' if r >= 3 else '❌'} |")


L = LIGHT
light_body = "#eef5ff"  # middle of the light gradient; panels are 92% white over it
report("Светлая тема (html[data-theme=light])", [
    ("Основной текст на панели", L["text"], L["panel"], light_body, ""),
    ("Вторичный текст (text-soft) на панели", L["text-soft"], L["panel"], light_body, ""),
    ("Muted-текст на панели", L["muted"], L["panel"], light_body, "подписи, метаданные, описания"),
    ("Muted-текст на panel-muted", L["muted"], L["panel-muted"], light_body, ""),
    ("Muted-2 (placeholder) на панели", L["muted-2"], L["panel"], light_body, "input::placeholder"),
    ("Текст на кнопке по умолчанию (#fff на --crm-text)", "#ffffff", L["text"], light_body, ""),
    ("Текст на primary-кнопке (#fff на --crm-primary)", "#ffffff", L["primary"], light_body, ""),
    ("Disabled кнопка opacity .55 (#fff на text@55%)", "#ffffff", "rgba(22,32,51,.55)", "#ffffff", "button:disabled"),
    ("Primary как текст/ссылка на панели", L["primary"], L["panel"], light_body, ""),
    ("Бейдж blue", L["blue-text"], L["blue"], "#ffffff", ""),
    ("Бейдж green", L["green-text"], L["green"], "#ffffff", ""),
    ("Бейдж red", L["red-text"], L["red"], "#ffffff", ""),
    ("Бейдж orange", L["orange-text"], L["orange"], "#ffffff", ""),
    ("Бейдж purple", L["purple-text"], L["purple"], "#ffffff", ""),
    ("Бейдж yellow", L["yellow-text"], L["yellow"], "#ffffff", ""),
    ("Граница поля ввода (line) на панели", L["line"], L["panel"], light_body, "UI-компонент: нужно ≥3:1"),
    ("Muted-текст в навигации (nav .64 над градиентом)", L["muted"], L["nav"], "#d9e7ff", ""),
])

D = DARK
report("Тёмная тема (html[data-theme=dark])", [
    ("Основной текст на --crm-panel (rgb(54 64 92))", D["text"], D["panel"], D["bg"], ""),
    ("Основной текст на --crm-card", D["text"], D["card"], D["bg"], ""),
    ("Muted-текст на --crm-panel", D["muted"], D["panel"], D["bg"], ""),
    ("Muted-текст на --crm-card", D["muted"], D["card"], D["bg"], ""),
    ("Muted-текст на panel-muted", D["muted"], D["panel-muted"], D["bg"], ""),
    ("Muted-2 (placeholder) на --crm-card", D["muted-2"], D["card"], D["bg"], "input::placeholder"),
    ("Muted-2 (placeholder) на --crm-panel", D["muted-2"], D["panel"], D["bg"], ""),
    ("Текст кнопки (#fff) на button-bg", "#ffffff", D["button-bg"], D["bg"], ""),
    ("Текст кнопки (#fff) на button-primary-bg", "#ffffff", D["button-primary-bg"], D["bg"], ""),
    ("Disabled: button-disabled-text на button-disabled-bg", D["button-disabled-text"], D["button-disabled-bg"], D["bg"], ""),
    ("Primary (#63a2ff) как текст на card", D["primary"], D["card"], D["bg"], ""),
    ("Бейдж blue над card", D["blue-text"], D["blue"], "#121826", ""),
    ("Бейдж green над card", D["green-text"], D["green"], "#121826", ""),
    ("Бейдж red над card", D["red-text"], D["red"], "#121826", ""),
    ("Бейдж orange над card", D["orange-text"], D["orange"], "#121826", ""),
    ("Бейдж purple над card", D["purple-text"], D["purple"], "#121826", ""),
    ("Бейдж yellow над card", D["yellow-text"], D["yellow"], "#121826", ""),
    ("Граница (line .14) на card", "rgba(148,163,184,.14)", D["card"], D["bg"], "UI-компонент: нужно ≥3:1"),
])

print("\n## Базовый :root (строки 4–47) — фактически перекрыт слоем v52 для всех цветов")
report("Базовый :root, если бы применялся", [
    ("muted #777 на #fff", "#777777", "#ffffff", "#ffffff", ""),
    ("muted-2 #a0a0a0 на #fff", "#a0a0a0", "#ffffff", "#ffffff", ""),
    ("text-soft #454545 на #fff", "#454545", "#ffffff", "#ffffff", ""),
])
