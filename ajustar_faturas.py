# -*- coding: utf-8 -*-
"""
AJUSTE_FATURA_DESTINO
----------------------
Automacao para ajustar as invoices exportadas do SGT antes do envio,
conforme regras por destino:

  - SUECIA, BELGICA, KOREA, USA, CHINA (e qualquer destino sem "INDIA"
    no texto da fatura): remove o campo "Country of Export" e o valor "BR".

  - INDIA: troca "Country of Export" -> "Country of Origin" e "BR" -> "SWEDEN",
    e inclui a observacao sobre a madeira da embalagem apos o paragrafo fixo
    "ITEM 2528000000 (G/L ACCOUNT 2461XX." na pagina 2.
"""

import io
import pdfplumber
from reportlab.pdfgen import canvas
from reportlab.lib.colors import white, black
from pypdf import PdfReader, PdfWriter

FONT_LABEL = "Courier-Bold"
FONT_VALUE = "Courier"
FONT_SIZE = 8

WOOD_NOTE_LINES = [
    "The wood used in the packaging is Pinus Silvestris and Picea Abies.",
    "The wood has been heat-treated according to the International",
    "Standard for Phytosanitarian Measures (ISPM)",
]

WOOD_NOTE_ANCHOR_TEXT = "2461XX."


def find_country_export_field(page):
    words = page.extract_words()
    for i in range(len(words) - 2):
        if (words[i]['text'] == 'Country' and words[i + 1]['text'] == 'of'
                and words[i + 2]['text'] == 'Export'):
            label = words[i:i + 3]
            label_x0 = min(w['x0'] for w in label)
            label_top = label[0]['top']
            label_bottom = label[0]['bottom']
            candidates = [
                w for w in words
                if label_top + 3 < w['top'] < label_bottom + 15
                and abs(w['x0'] - label_x0) < 15
            ]
            if candidates:
                value = candidates[0]
                return {
                    'label_x0': label_x0,
                    'label_top': label_top,
                    'label_bottom': label_bottom,
                    'value_x0': value['x0'],
                    'value_top': value['top'],
                    'value_bottom': value['bottom'],
                    'value_text': value['text'],
                }
    return None


def find_wood_note_anchor(page):
    for w in page.extract_words():
        if w['text'] == WOOD_NOTE_ANCHOR_TEXT:
            return w
    return None


def is_india(full_text):
    return 'INDIA' in (full_text or '').upper()


def whiteout(c, x0, y0, x1, y1):
    c.setFillColor(white)
    c.rect(x0, y0, x1 - x0, y1 - y0, fill=1, stroke=0)


def overlay_for_page(page_w, page_h, draw_fn):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    draw_fn(c)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def process(input_path, output_path):
    """Retorna uma string com o resumo do que foi feito no arquivo."""
    with pdfplumber.open(input_path) as pdf:
        page1 = pdf.pages[0]
        full_text_p1 = page1.extract_text() or ""
        field = find_country_export_field(page1)
        page_w, page_h = float(page1.width), float(page1.height)
        india = is_india(full_text_p1)

        total_paginas = len(pdf.pages)
        note_anchor = None
        note_page_index = None
        note_page_size = None
        if india:
            for idx in range(total_paginas):
                pg = pdf.pages[idx]
                anchor = find_wood_note_anchor(pg)
                if anchor:
                    note_anchor = anchor
                    note_page_index = idx
                    note_page_size = (float(pg.width), float(pg.height))

    reader = PdfReader(input_path)
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        overlays = []

        if i == 0 and field:
            def ops_page1(c, field=field, india=india, page_h=page_h):
                x0 = min(field['label_x0'], field['value_x0']) - 3
                x1 = max(field['label_x0'] + 95, field['value_x0'] + 95)
                y_top = page_h - field['label_top'] + 2
                y_bot = page_h - field['value_bottom'] - 2
                whiteout(c, x0, y_bot, x1, y_top)
                if india:
                    c.setFillColor(black)
                    c.setFont(FONT_LABEL, FONT_SIZE)
                    baseline_label = page_h - field['label_top'] - 0.7 * FONT_SIZE
                    c.drawString(field['label_x0'], baseline_label, "Country of Origin")
                    c.setFont(FONT_VALUE, FONT_SIZE)
                    baseline_value = page_h - field['value_top'] - 0.7 * FONT_SIZE
                    c.drawString(field['value_x0'], baseline_value, "SWEDEN")

            overlays.append((page_w, page_h, ops_page1))

        if india and i == note_page_index and note_anchor:
            pw, ph = note_page_size

            def ops_note(c, anchor=note_anchor, ph=ph):
                c.setFillColor(black)
                c.setFont(FONT_VALUE, FONT_SIZE)
                x = 53.2
                top0 = anchor['top'] + 22.3
                for idx2, line in enumerate(WOOD_NOTE_LINES):
                    baseline = ph - (top0 + idx2 * 9.6) - 0.7 * FONT_SIZE
                    c.drawString(x, baseline, line)

            overlays.append((pw, ph, ops_note))

        for pw, ph, ops in overlays:
            page.merge_page(overlay_for_page(pw, ph, ops))

        writer.add_page(page)

    note_added = note_page_index is not None

    with open(output_path, 'wb') as f:
        writer.write(f)

    destino = "INDIA" if india else "OUTRO (remocao simples)"
    partes = [f"destino: {destino}", f"{total_paginas} pagina(s)"]
    if not field:
        partes.append("!! campo 'Country of Export' NAO encontrado")
    else:
        if india:
            partes.append("campo alterado para 'Country of Origin' / 'SWEDEN'")
        else:
            partes.append("campo removido")
    if india:
        if note_added:
            partes.append("observacao da madeira incluida")
        else:
            partes.append("!! ancora da observacao NAO encontrada")
    return "; ".join(partes)
