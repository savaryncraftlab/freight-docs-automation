"""
pdf_reader.py — витягує текст з PDF файлів.

Використовує pdfplumber як основний інструмент,
pymupdf (fitz) як fallback якщо pdfplumber не спрацьовує.

pip install pdfplumber pymupdf
"""

from pathlib import Path


def extract_pdf_text(pdf_path: str, max_chars: int = 4000) -> str:
    """
    Витягує текст з PDF.
    Повертає рядок (порожній якщо не вдалось).
    """
    path = Path(pdf_path)
    if not path.exists():
        return ""

    # Спробуємо pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            parts = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
            result = "\n".join(parts)
            if result.strip():
                return result[:max_chars]
    except Exception:
        pass

    # Fallback: pymupdf
    try:
        import fitz
        doc = fitz.open(str(path))
        parts = []
        for page in doc:
            parts.append(page.get_text())
        doc.close()
        result = "\n".join(parts)
        return result[:max_chars]
    except Exception:
        pass

    return ""
