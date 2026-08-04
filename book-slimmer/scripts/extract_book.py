#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
book-slimmer 文本提取器
支持 EPUB（纯标准库）与 PDF（pypdf / pdfplumber / PyMuPDF 依次尝试）。

用法:
  python extract_book.py <文件路径> [--out <输出.json>]

输出 JSON 结构:
  {
    "format": "epub" | "pdf",
    "title": "...", "author": "...",
    "chapters": [
      {"file": "ch3.xhtml", "title": "...", "blocks": [
        {"type": "heading", "text": "...", "loc": "ch3.xhtml#h1"},
        {"type": "para",    "text": "...", "loc": "ch3.xhtml#p12"}
      ]}
    ]
  }
PDF 版 chapters 退化为单章: {"file": "pdf", "title": null, "blocks": [{"type":"page","text":"...","loc":"p.135"}]}
"""
import argparse
import json
import os
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def local(tag):
    """去掉 XML 命名空间前缀，如 '{http://www.w3.org/1999/xhtml}p' -> 'p'"""
    return tag.rsplit("}", 1)[-1]


# ---------------- EPUB ----------------

def _read_opf(zf, opf_path):
    """解析 OPF：返回 (manifest{id: (href, media_type)}, spine_order[href...], dc_title, dc_creator)"""
    root = ET.fromstring(zf.read(opf_path))
    manifest = {}
    spine_order = []
    title = author = ""
    for el in root.iter():
        ln = local(el.tag)
        if ln == "item":
            manifest[el.get("id")] = (el.get("href"), el.get("media-type", ""))
        elif ln == "itemref":
            idref = el.get("idref")
            if idref in manifest:
                spine_order.append(manifest[idref][0])
        elif ln == "title":
            title = (el.text or "").strip()
        elif ln == "creator":
            author = (el.text or "").strip()
    return manifest, spine_order, title, author


def _resolve_path(opf_path, href):
    base = os.path.dirname(opf_path)
    return os.path.normpath(os.path.join(base, href)).replace("\\", "/")


def _parse_xhtml(text):
    """从 XHTML 文本提取 (标题, 段落列表)。容错处理命名空间与实体。"""
    headings, paras = [], []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # 容错：去掉非法字符后重试
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return headings, paras
    for el in root.iter():
        ln = local(el.tag)
        if ln in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = "".join(el.itertext()).strip()
            if t:
                headings.append(t)
        elif ln in ("p", "blockquote", "li"):
            t = " ".join("".join(el.itertext()).split())
            if t:
                paras.append(t)
    return headings, paras


def extract_epub(path):
    result = {"format": "epub", "title": "", "author": "", "chapters": []}
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            raise ValueError("EPUB container.xml 中未找到 rootfile")
        opf_path = m.group(1)
        manifest, spine_order, title, author = _read_opf(zf, opf_path)
        result["title"], result["author"] = title, author
        # 正文按 spine 顺序读取（跳过 css/图片等）
        content_hrefs = [h for h in spine_order
                         if h.lower().endswith((".xhtml", ".html", ".htm", ".xml"))]
        if not content_hrefs:
            content_hrefs = [h for h, mt in manifest.values()
                             if "html" in mt and h.lower().endswith((".xhtml", ".html", ".htm"))]
        seen = set()
        for href in content_hrefs:
            full = _resolve_path(opf_path, href)
            if full in seen:
                continue
            seen.add(full)
            try:
                raw = zf.read(full)
            except KeyError:
                continue
            text = raw.decode("utf-8", "replace")
            headings, paras = _parse_xhtml(text)
            base = os.path.basename(full)
            blocks = []
            for i, h in enumerate(headings, 1):
                blocks.append({"type": "heading", "text": h, "loc": f"{base}#h{i}"})
            for i, p in enumerate(paras, 1):
                blocks.append({"type": "para", "text": p, "loc": f"{base}#p{i}"})
            if blocks:
                result["chapters"].append({
                    "file": base,
                    "title": headings[0] if headings else base,
                    "blocks": blocks,
                })
    if not result["chapters"]:
        raise ValueError("EPUB 未提取到任何正文内容（可能为 DRM 加密或结构异常）")
    return result


# ---------------- PDF ----------------

def _try_pypdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages, 1):
        txt = page.extract_text() or ""
        if txt.strip():
            pages.append({"type": "page", "text": txt.strip(), "loc": f"p.{i}"})
    return pages


def _try_pdfplumber(path):
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append({"type": "page", "text": txt.strip(), "loc": f"p.{i}"})
    return pages


def _try_fitz(path):
    import fitz
    doc = fitz.open(path)
    pages = []
    for i in range(len(doc)):
        txt = doc[i].get_text() or ""
        if txt.strip():
            pages.append({"type": "page", "text": txt.strip(), "loc": f"p.{i + 1}"})
    return pages


def extract_pdf(path):
    last_err = None
    for fn in (_try_pypdf, _try_pdfplumber, _try_fitz):
        try:
            pages = fn(path)
            if pages:
                return {"format": "pdf", "title": "", "author": "", "chapters": [
                    {"file": "pdf", "title": None, "blocks": pages}
                ]}
        except ImportError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            raise ValueError(f"PDF 解析失败（{fn.__name__}）: {e}") from e
    raise ValueError(
        "未找到可用的 PDF 库。请先安装：pip install pypdf（若失败可试 pdfplumber / PyMuPDF）"
    ) from last_err


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="book-slimmer 书籍文本提取器")
    ap.add_argument("file", help="PDF 或 EPUB 文件路径")
    ap.add_argument("--out", help="输出 JSON 路径（缺省输出到 stdout）")
    args = ap.parse_args()

    src = args.file
    if not os.path.exists(src):
        sys.exit(f"文件不存在: {src}")
    ext = os.path.splitext(src)[1].lower()
    if ext == ".epub":
        data = extract_epub(src)
    elif ext in (".pdf",):
        data = extract_pdf(src)
    elif ext in (".mobi", ".azw3"):
        sys.exit(f"{ext} 格式暂不支持，请转换为 EPUB 或 PDF 后再试")
    else:
        sys.exit(f"不支持的格式: {ext}（支持 .epub / .pdf）")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"OK: {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False))

    n_blocks = sum(len(c["blocks"]) for c in data["chapters"])
    print(f"summary: format={data['format']} chapters={len(data['chapters'])} blocks={n_blocks}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
