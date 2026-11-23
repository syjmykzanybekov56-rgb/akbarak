"""
АкБарак — лист «фото на документы» на фотобумаге 10×15 (как в фотобудках).

Одно фото студента → обрезка под нужный размер → 6 (или 8) копий сеткой,
линии реза, справа панель: логотип, размер, дата, номер заказа и QR на цифровую копию.

    from photo_sheet import make_sheet
    make_sheet("face.jpg", "out.jpg", size="35x45", order_id=123, qr_url="https://akbarak.vercel.app/...")
"""

from datetime import datetime

import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

DPI = 300
SHEET_MM = (152, 102)              # 10×15 (точнее 4×6″) — лист в горизонтальной ориентации
SIZES = {                          # мм, сетка (колонки, ряды)
    "35x45": ((35, 45), (3, 2)),   # паспорт, виза, большинство документов
    "30x40": ((30, 40), (3, 2)),   # личное дело, студенческий, справки
    "30x40x8": ((30, 40), (4, 2)), # 8 штук помельче
    "25x35": ((25, 35), (3, 2)),   # 6 штук, с QR
    "50x50": ((50, 50), (2, 1)),   # виза США — 2 штуки
}
INSTAGRAM_URL = "https://instagram.com/akbarak_kg"
QR_CAPTION = ("Мы в Instagram", "akbarak_kg")
INSTAGRAM_QR = (  # готовый QR на instagram.com/akbarak_kg — рисуем сами, библиотека qrcode не нужна
    "11111110010111001011001111111",
    "10000010100010111101101000001",
    "10111010011011101001001011101",
    "10111010010111110010001011101",
    "10111010101010100111101011101",
    "10000010011001000001101000001",
    "11111110101010101010101111111",
    "00000000010001101111100000000",
    "10101010000110110101100010010",
    "00110101111000110100111001001",
    "00111010001001000110001010111",
    "01111001001110010100100000010",
    "01111011011110001101111101011",
    "00101101111011011100111001001",
    "11110010000110111010011101011",
    "00100101000011101110001101010",
    "01010011100010111100111101011",
    "01101000101100110110101001101",
    "10100010000111000110101000011",
    "01111000110110001100111011010",
    "10100010101110001101111110000",
    "00000000101111001101100010111",
    "11111110010110100011101011011",
    "10000010000111100111100011010",
    "10111010100110111100111110001",
    "10111010001001101000000110111",
    "10111010111001000111000111001",
    "10000010011001101111110000010",
    "11111110111110110001111000011",
)
PINK = (193, 53, 132)          # фирменный розовый Instagram
GOLD, NAVY, GRID = (245, 166, 35), (13, 34, 64), (150, 150, 150)


WHITE = (255, 255, 255)
BRAND_NAVY, BRAND_GOLD = (13, 34, 64), (245, 166, 35)   # как на сайте: #0D2240, #F5A623


def draw_logo(width, with_sub=True):
    """Логотип АкБарак: лист с тюндюком + «АкБарак», на тёмно-синей плашке."""
    s = 4                                            # рисуем крупно и уменьшаем — края ровнее
    h = round(width * (0.42 if with_sub else 0.32)) * s
    w = width * s
    img = Image.new("RGB", (w, h), BRAND_NAVY)
    d = ImageDraw.Draw(img)
    mh = h * (0.70 if with_sub else 0.78)            # высота знака: лист с тюндюком
    k = mh / 202
    ox, oy = h * 0.14 - 58 * k, (h - mh) / 2 - 20 * k
    P = lambda pts: [(ox + x * k, oy + y * k) for x, y in pts]
    d.polygon(P([(58, 20), (152, 20), (192, 60), (192, 222), (58, 222)]), fill=WHITE)
    d.polygon(P([(152, 20), (152, 60), (192, 60)]), fill=BRAND_GOLD)
    cx, cy = P([(124, 146)])[0]
    d.ellipse([cx - 52 * k, cy - 52 * k, cx + 52 * k, cy + 52 * k], fill=BRAND_NAVY)
    lw = max(1, round(7 * k))
    d.ellipse([cx - 40 * k, cy - 40 * k, cx + 40 * k, cy + 40 * k], outline=BRAND_GOLD, width=lw)
    for y in (-10, 0, 10):                             # перекладины тюндюка
        d.line([(cx - 36 * k, cy + y * k), (cx + 36 * k, cy + y * k)], fill=BRAND_GOLD, width=lw)
    for x in (-10, 0, 10):
        d.line([(cx + x * k, cy - 36 * k), (cx + x * k, cy + 36 * k)], fill=BRAND_GOLD, width=lw)
    ox, mh_mark = ox + 58 * k, mh
    k = mh / 40                                        # дальше — разметка текста как раньше
    tx = ox + 134 * (mh_mark / 202) + h * 0.10
    tw = w - tx - h * 0.12
    name_px = int(mh * 0.62)
    while name_px > 8 and d.textlength("АкБарак", font=_font(name_px, True)) > tw:
        name_px -= 2
    fb = _font(name_px, True)
    ty = oy + (mh * 0.08 if with_sub else (mh - name_px) / 2)
    d.text((tx, ty), "Ак", font=fb, fill=BRAND_GOLD)
    d.text((tx + d.textlength("Ак", font=fb), ty), "Барак", font=fb, fill=WHITE)
    if with_sub:
        sub_px = int(name_px * 0.42)
        fs = _font(sub_px)
        while sub_px > 6 and d.textlength("печать 24/7", font=fs) > tw:
            sub_px -= 1; fs = _font(sub_px)
        d.text((tx + 2, ty + name_px * 1.12), "печать 24/7", font=fs, fill=(190, 200, 215))
    return img.resize((width, h // s), Image.LANCZOS)


def mm(v):
    return round(v / 25.4 * DPI)


def _font(px, bold=False):
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                 ("arialbd.ttf" if bold else "arial.ttf")):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


# Стандарты: какую долю высоты фото занимает голова (от подбородка до макушки)
# и сколько места над макушкой. 35×45 — паспорт/шенген (голова 32–36 мм), 50×50 — виза США.
HEAD = {(35, 45): (0.75, 0.07), (30, 40): (0.70, 0.08), (25, 35): (0.68, 0.08), (50, 50): (0.60, 0.10)}


def print_boost(img):
    """Струйная печать на глянце бледнее экрана телефона. Усиливаем только то, чего фото не хватает:
    измеряем контраст и насыщенность и тянем их к норме обычного «хорошего» фото, не дальше.
    Нормальное фото почти не меняется, бледное — оживает, тени не «слипаются» в чёрное.
    Сила — AKBARAK_PHOTO_BOOST (0 — выкл., 1 — обычно)."""
    from PIL import ImageStat
    try:
        k = float(os.getenv("AKBARAK_PHOTO_BOOST", "1"))
    except ValueError:
        k = 1.0
    if k <= 0:
        return img
    clamp = lambda v, lo, hi: max(lo, min(hi, v))
    L = ImageStat.Stat(img.convert("L"))
    std, mean = L.stddev[0], L.mean[0]
    sat = ImageStat.Stat(img.convert("HSV").split()[1]).mean[0]
    c = 1 + (clamp(78 / max(std, 1), 1.0, 1.35) - 1) * k
    s = 1 + (clamp(52 / max(sat, 1), 1.0, 1.3) - 1) * k
    img = ImageEnhance.Contrast(img).enhance(c)
    img = ImageEnhance.Color(img).enhance(s)
    if mean > 150:                                       # засвеченное — уплотняем полутона, белый остаётся белым
        g = 1 + 0.08 * k
        img = img.point([round(255 * (i / 255) ** g) for i in range(256)] * 3)
    return img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=45, threshold=3))


def find_face(img):
    """Самое крупное лицо на фото: (x, y, w, h) или None. Нужен opencv-python-headless."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    small = img.copy()
    k = 1.0
    if max(small.size) > 1200:
        k = 1200 / max(small.size)
        small = small.resize((round(small.width * k), round(small.height * k)))
    g0 = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2GRAY)
    found = []
    m = max(40, min(small.size) // 10)
    for g in (g0, cv2.equalizeHist(g0)):     # сначала как есть, затем с выравниванием яркости (тёмные фото)
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml"):
            cc = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if cc.empty():                   # в OpenCV 5 файлов каскадов нет — нужен opencv-python-headless<5
                continue
            found = cc.detectMultiScale(g, scaleFactor=1.06, minNeighbors=7, minSize=(m, m))
            if len(found):
                break
        if len(found):
            break
    if not len(found):
        return None
    x, y, w, h = max(found, key=lambda f: f[2] * f[3])
    W, H = small.size
    # Защита от ложных «лиц» (узор, ухо, складки): лицо должно быть не меньше 15 % кадра
    # и его центр — в средней части снимка. Иначе режем по центру, как раньше.
    cx, cy = (x + w / 2) / W, (y + h / 2) / H
    if w < 0.15 * min(W, H) or not (0.15 < cx < 0.85 and 0.1 < cy < 0.8):
        return None
    return x / k, y / k, w / k, h / k


def crop_to(img, w_mm, h_mm, focus=(0.5, 0.42)):
    """Обрезает фото под документ. Если нашли лицо — как в фотосалоне: голова нужного размера
    по стандарту, по центру, над макушкой небольшой отступ (не хватает фона — добавляем белый).
    Лицо не нашли — режем по пропорции вокруг точки focus (доли ширины/высоты)."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    target = w_mm / h_mm
    w, h = img.size
    face = find_face(img)
    if face:
        fx, fy, fw, fh = face
        head_k, top_k = HEAD.get((w_mm, h_mm), (0.70, 0.08))
        head = fh * 1.45                     # рамка детектора — брови…подбородок; с волосами голова ≈ в 1.45 раза выше
        chin = fy + fh * 1.04
        crown = chin - head
        ch = head / head_k                   # высота кадра
        cw = ch * target
        top = crown - ch * top_k
        left = fx + fw / 2 - cw / 2
        box = tuple(round(v) for v in (left, top, left + cw, top + ch))
        bg = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), "white")
        bg.paste(img, (-box[0], -box[1]))
        img = bg
    elif w / h > target:                     # кадр шире нужного — режем бока
        nw = round(h * target)
        x = min(max(round(focus[0] * w - nw / 2), 0), w - nw)
        img = img.crop((x, 0, x + nw, h))
    else:                                    # кадр выше — режем верх/низ
        nh = round(w / target)
        y = min(max(round(focus[1] * h - nh / 2), 0), h - nh)
        img = img.crop((0, y, w, y + nh))
    img = img.resize((mm(w_mm), mm(h_mm)), Image.LANCZOS)
    return print_boost(img)


def qr_image(url, side):
    """QR розовым. Instagram — из готовой матрицы; другой адрес — через библиотеку qrcode, если она есть."""
    rows = INSTAGRAM_QR if url == INSTAGRAM_URL else None
    if rows is None:
        try:
            import qrcode
            q = qrcode.QRCode(border=0)
            q.add_data(url); q.make(fit=True)
            rows = ["".join("1" if v else "0" for v in r) for r in q.get_matrix()]
        except Exception:
            return None
    n = len(rows) + 2                                  # +1 модуль белой рамки с каждой стороны
    small = Image.new("RGB", (n, n), "white")
    px = small.load()
    for yy, r in enumerate(rows):
        for xx, ch in enumerate(r):
            if ch == "1":
                px[xx + 1, yy + 1] = PINK
    return small.resize((side, side), Image.NEAREST)


def make_sheet(photo_path, out_path, size="35x45", order_id=None, qr_url=INSTAGRAM_URL, focus=(0.5, 0.42),
               qr_caption=QR_CAPTION):
    (pw, ph), (cols, rows) = SIZES[size]
    W, H = mm(SHEET_MM[0]), mm(SHEET_MM[1])
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)

    photo = crop_to(Image.open(photo_path), pw, ph, focus)
    gap, margin = mm(2.5), mm(3)
    grid_w = cols * mm(pw) + (cols - 1) * gap
    grid_h = rows * mm(ph) + (rows - 1) * gap
    x0, y0 = margin, (H - grid_h) // 2

    for r in range(rows):
        for c in range(cols):
            sheet.paste(photo, (x0 + c * (mm(pw) + gap), y0 + r * (mm(ph) + gap)))

    # линии реза — посередине зазоров и по краям сетки
    cut = []
    for c in range(cols + 1):
        cut.append(("v", x0 + c * (mm(pw) + gap) - gap // 2 if c else x0 - gap // 2))
    for r in range(rows + 1):
        cut.append(("h", y0 + r * (mm(ph) + gap) - gap // 2 if r else y0 - gap // 2))
    for kind, p in cut:
        if kind == "v":
            for y in range(y0 - gap, y0 + grid_h + gap, 18):
                d.line([(p, y), (p, y + 9)], fill=GRID, width=2)
        else:
            for x in range(x0 - gap, x0 + grid_w + gap, 18):
                d.line([(x, p), (x + 9, p)], fill=GRID, width=2)

    # правая панель
    px0 = x0 + grid_w + mm(4)
    pwid = W - px0 - mm(2)
    cx = px0 + pwid // 2
    for yy in range(0, H, 18):                      # линия отреза панели
        d.line([(px0 - mm(2), yy), (px0 - mm(2), yy + 9)], fill=GRID, width=2)

    def fit(text, px, bold=False, width=pwid):
        while px > 10 and d.textlength(text, font=_font(px, bold)) > width:
            px -= 2
        return _font(px, bold)

    date = datetime.now().strftime("%d.%m.%Y")
    if pwid < mm(24):
        # узкая полоска (8 фото на листе) — пишем вертикально, без QR
        line = f"{pw}×{ph} мм · {cols * rows} шт   {date}" + (f"   #{order_id}" if order_id else "")
        strip = Image.new("RGB", (H - mm(6), pwid), "white")
        logo = draw_logo(round(pwid / 0.32), with_sub=False)
        logo = logo.resize((round(logo.width * pwid / logo.height), pwid), Image.LANCZOS)
        strip.paste(logo, (0, 0))
        sd = ImageDraw.Draw(strip)
        room = strip.width - logo.width - mm(3)
        f = _font(min(mm(3.6), pwid - mm(4)), True)
        while f.size > 10 and sd.textlength(line, font=f) > room:
            f = _font(f.size - 2, True)
        sd.text((logo.width + mm(3) + room // 2, pwid // 2), line, font=f, fill=NAVY, anchor="mm")
        sheet.paste(strip.rotate(90, expand=True), (px0, mm(3)))
    else:
        logo = draw_logo(pwid)
        sheet.paste(logo, (px0, mm(3)))
        y = mm(3) + logo.height + mm(4)
        for t, px, b in (("Фото на", mm(3.4), True), ("документы", mm(3.4), True),
                         (f"{pw}×{ph} мм · {cols * rows} шт", mm(2.9), False), (date, mm(2.9), False),
                         (f"Заказ #{order_id}" if order_id else "", mm(2.9), False)):
            if t:
                d.text((cx, y), t, font=fit(t, px, b), fill=NAVY, anchor="mt"); y += mm(4.6)
        if qr_url:
            side = min(pwid - mm(2), H - y - mm(14))
            qi = qr_image(qr_url, side)
            if qi is not None:
                sheet.paste(qi, (cx - side // 2, H - side - mm(11)))
                for i, t in enumerate(qr_caption):
                    d.text((cx, H - mm(9.5) + i * mm(3)), t, font=fit(t, mm(2.4), i == 1), fill=PINK, anchor="mt")

    sheet.save(out_path, quality=95, dpi=(DPI, DPI))
    return out_path
