"""
АкБарак — лист «фото на документы» на фотобумаге 10×15 (как в фотобудках).

Одно фото студента → обрезка под нужный размер → 6 (или 8) копий сеткой,
линии реза, справа панель: логотип, размер, дата, номер заказа и QR на цифровую копию.

    from photo_sheet import make_sheet
    make_sheet("face.jpg", "out.jpg", size="35x45", order_id=123, qr_url="https://akbarak.vercel.app/...")
"""

from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

DPI = 300
SHEET_MM = (152, 102)              # 10×15 (точнее 4×6″) — лист в горизонтальной ориентации
SIZES = {                          # мм, сетка (колонки, ряды)
    "35x45": ((35, 45), (3, 2)),   # паспорт, виза, большинство документов
    "30x40": ((30, 40), (3, 2)),   # личное дело, студенческий, справки
    "30x40x8": ((30, 40), (4, 2)), # 8 штук помельче
    "25x35": ((25, 35), (4, 2)),
}
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


def crop_to(img, w_mm, h_mm, focus=(0.5, 0.42)):
    """Обрезает фото под пропорцию документа. focus — где на кадре лицо (доли ширины/высоты).
    На сайте студент двигает рамку сам и передаёт focus; по умолчанию — чуть выше центра."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    target = w_mm / h_mm
    w, h = img.size
    if w / h > target:                       # кадр шире нужного — режем бока
        nw = round(h * target)
        x = min(max(round(focus[0] * w - nw / 2), 0), w - nw)
        img = img.crop((x, 0, x + nw, h))
    else:                                    # кадр выше — режем верх/низ
        nh = round(w / target)
        y = min(max(round(focus[1] * h - nh / 2), 0), h - nh)
        img = img.crop((0, y, w, y + nh))
    return img.resize((mm(w_mm), mm(h_mm)), Image.LANCZOS)


def make_sheet(photo_path, out_path, size="35x45", order_id=None, qr_url=None, focus=(0.5, 0.42)):
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
            import qrcode
            q = qrcode.QRCode(border=1, box_size=10)
            q.add_data(qr_url); q.make(fit=True)
            side = min(pwid - mm(2), H - y - mm(14))
            qi = q.make_image(fill_color=NAVY, back_color="white").convert("RGB").resize((side, side), Image.NEAREST)
            sheet.paste(qi, (cx - side // 2, H - side - mm(11)))
            for i, t in enumerate(("Цифровая копия —", "скачать по QR")):
                d.text((cx, H - mm(9.5) + i * mm(3)), t, font=fit(t, mm(2.4)), fill=NAVY, anchor="mt")

    sheet.save(out_path, quality=95, dpi=(DPI, DPI))
    return out_path
