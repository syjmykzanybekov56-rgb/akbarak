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
GOLD, NAVY, GRID = (226, 176, 74), (20, 40, 61), (150, 150, 150)


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
        line = f"АкБарак   {pw}×{ph} мм · {cols * rows} шт   {date}" + (f"   #{order_id}" if order_id else "")
        strip = Image.new("RGB", (H - mm(6), pwid), "white")
        sd = ImageDraw.Draw(strip)
        f = _font(min(mm(4), pwid - mm(3)), True)
        while f.size > 10 and sd.textlength(line, font=f) > strip.width:
            f = _font(f.size - 2, True)
        sd.text((strip.width // 2, pwid // 2), line, font=f, fill=NAVY, anchor="mm")
        sheet.paste(strip.rotate(90, expand=True), (px0, mm(3)))
    else:
        y = mm(5)
        d.text((cx, y), "АкБарак", font=fit("АкБарак", mm(7), True), fill=NAVY, anchor="mt")
        y += mm(9)
        d.rectangle([px0 + mm(1), y, px0 + pwid - mm(1), y + mm(1)], fill=GOLD)
        y += mm(4)
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
