"""Markdown and LaTeX rendering for the GUI chat logs.

Markdown is rendered into a Tk Text widget. LaTeX is approximated with Unicode.
"""
import re

MD_LATEX_GREEK = {
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7", "theta": "\u03b8",
    "iota": "\u03b9", "kappa": "\u03ba", "lambda": "\u03bb", "mu": "\u03bc",
    "nu": "\u03bd", "xi": "\u03be", "omicron": "\u03bf", "pi": "\u03c0",
    "rho": "\u03c1", "sigma": "\u03c3", "tau": "\u03c4", "upsilon": "\u03c5",
    "phi": "\u03c6", "chi": "\u03c7", "psi": "\u03c8", "omega": "\u03c9",
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
    "infty": "\u221e", "partial": "\u2202", "nabla": "\u2207",
    "pm": "\u00b1", "mp": "\u2213", "times": "\u00d7", "div": "\u00f7",
    "cdot": "\u00b7", "leq": "\u2264", "geq": "\u2265", "neq": "\u2260",
    "approx": "\u2248", "equiv": "\u2261", "sim": "\u223c",
    "in": "\u2208", "notin": "\u2209", "subset": "\u2282", "subseteq": "\u2286",
    "cup": "\u222a", "cap": "\u2229", "emptyset": "\u2205",
    "forall": "\u2200", "exists": "\u2203", "sum": "\u2211", "prod": "\u220f",
    "int": "\u222b", "sqrt": "\u221a", "to": "\u2192", "rightarrow": "\u2192",
    "leftarrow": "\u2190", "leftrightarrow": "\u2194", "Rightarrow": "\u21d2",
    "Leftarrow": "\u21d0", "Leftrightarrow": "\u21d4",
    "cdots": "\u22ef", "ldots": "\u2026", "vdots": "\u22ee", "ddots": "\u22f1",
}
MD_SUPERSCRIPTS = {
    "0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3", "4": "\u2074",
    "5": "\u2075", "6": "\u2076", "7": "\u2077", "8": "\u2078", "9": "\u2079",
    "+": "\u207a", "-": "\u207b", "=": "\u207c", "(": "\u207d", ")": "\u207e",
    "n": "\u207f", "i": "\u2071",
    "a": "\u1d43", "b": "\u1d47", "c": "\u1d9c", "d": "\u1d48", "e": "\u1d49",
    "f": "\u1da0", "g": "\u1d4d", "h": "\u02b0", "j": "\u02b2", "k": "\u1d4f",
    "l": "\u02e1", "m": "\u1d50", "o": "\u1d52", "p": "\u1d56", "r": "\u02b3",
    "s": "\u02e2", "t": "\u1d57", "u": "\u1d58", "v": "\u1d5b", "w": "\u02b7",
    "x": "\u02e3", "y": "\u02b8", "z": "\u1dbb",
}
MD_SUBSCRIPTS = {
    "0": "\u2080", "1": "\u2081", "2": "\u2082", "3": "\u2083", "4": "\u2084",
    "5": "\u2085", "6": "\u2086", "7": "\u2087", "8": "\u2088", "9": "\u2089",
    "+": "\u208a", "-": "\u208b", "=": "\u208c", "(": "\u208d", ")": "\u208e",
    "a": "\u2090", "e": "\u2091", "o": "\u2092", "x": "\u2093", "h": "\u2095",
    "k": "\u2096", "l": "\u2097", "m": "\u2098", "n": "\u2099", "p": "\u209a",
    "s": "\u209b", "t": "\u209c", "i": "\u1d62", "j": "\u2c7c", "u": "\u1d64",
    "v": "\u1d65", "r": "\u1d63",
}


def _md_map_chars(s, table):
    out = []
    ok = True
    for ch in s:
        if ch in table:
            out.append(table[ch])
        else:
            out.append(ch)
            ok = False
    return "".join(out), ok


def latex_to_unicode(expr):
    """Approximates a LaTeX snippet with Unicode."""
    expr = expr.strip()

    def repl_frac(m):
        return f"({m.group(1)}/{m.group(2)})"
    for _ in range(4):
        new = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", repl_frac, expr)
        if new == expr:
            break
        expr = new

    def repl_sqrt(m):
        return "\u221a(" + m.group(1) + ")"
    for _ in range(4):
        new = re.sub(r"\\sqrt\{([^{}]*)\}", repl_sqrt, expr)
        if new == expr:
            break
        expr = new

    def repl_cmd(m):
        name = m.group(1)
        return MD_LATEX_GREEK.get(name, "\\" + name)
    expr = re.sub(r"\\([A-Za-z]+)", repl_cmd, expr)

    def repl_sup(m):
        body = m.group(1) if m.group(1) is not None else m.group(2)
        mapped, ok = _md_map_chars(body, MD_SUPERSCRIPTS)
        return mapped if ok else "^" + body
    expr = re.sub(r"\^\{([^{}]*)\}|\^(\w)", repl_sup, expr)

    def repl_sub(m):
        body = m.group(1) if m.group(1) is not None else m.group(2)
        mapped, ok = _md_map_chars(body, MD_SUBSCRIPTS)
        return mapped if ok else "_" + body
    expr = re.sub(r"_\{([^{}]*)\}|_(\w)", repl_sub, expr)

    expr = expr.replace("\\left", "").replace("\\right", "")
    expr = expr.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    expr = expr.replace("\\%", "%").replace("\\#", "#")
    return expr


_MD_INLINE_SPLIT_RE = re.compile(
    r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|__.+?__|\*[^*\n]+?\*|_[^_\n]+?_|"
    r"~~.+?~~|`[^`\n]+?`|\$\$.+?\$\$|\$[^$\n]+?\$|"
    r"!\[[^\]]*?\]\([^)\s]+?\)|\[[^\]]+?\]\([^)\s]+?\))"
)


def _md_tokenize_inline(text):
    return [p for p in _MD_INLINE_SPLIT_RE.split(text) if p != ""]


def _md_classify_inline(token):
    if token.startswith("$$") and token.endswith("$$") and len(token) >= 4:
        return ("math", token[2:-2])
    if token.startswith("$") and token.endswith("$") and len(token) >= 2:
        return ("math", token[1:-1])
    if token.startswith("***") and token.endswith("***") and len(token) >= 6:
        return ("bolditalic", token[3:-3])
    if token.startswith("**") and token.endswith("**") and len(token) >= 4:
        return ("bold", token[2:-2])
    if token.startswith("__") and token.endswith("__") and len(token) >= 4:
        return ("bold", token[2:-2])
    if token.startswith("~~") and token.endswith("~~") and len(token) >= 4:
        return ("strike", token[2:-2])
    if token.startswith("`") and token.endswith("`") and len(token) >= 2:
        return ("code", token[1:-1])
    if token.startswith("!["):
        m = re.match(r"!\[([^\]]*)\]\(([^)\s]+)\)", token)
        if m:
            return ("image", m.group(1), m.group(2))
    if token.startswith("["):
        m = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", token)
        if m:
            return ("link", m.group(1), m.group(2))
    if token.startswith("*") and token.endswith("*") and len(token) >= 2:
        return ("italic", token[1:-1])
    if token.startswith("_") and token.endswith("_") and len(token) >= 2:
        return ("italic", token[1:-1])
    return ("text", token)


_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_MD_ULIST_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_MD_OLIST_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_MD_TASK_RE = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_MD_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)(.*)$")

MD_TAG_NAMES = {
    "bold": "md_bold",
    "italic": "md_italic",
    "strike": "md_strike",
    "inline_code": "md_inline_code",
    "code_block": "md_code_block",
    "math": "md_math",
    "link": "md_link",
    "hr": "md_hr",
    "blockquote": "md_blockquote",
    "blockquote_bar": "md_blockquote_bar",
    "list_marker": "md_list_marker",
    "task_box": "md_task_box",
    "task_done": "md_task_done",
    "table_header": "md_table_header",
    "table_rule": "md_table_rule",
    "table_row": "md_table_row",
    "header": ["md_h1", "md_h2", "md_h3", "md_h4", "md_h5", "md_h6"],
}


def _md_split_table_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells = []
    current = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current).strip())
    return cells


def render_inline(widget, text, base_tags, extra_tag, tags):
    """Renders one line of inline markdown into the widget."""
    base = tuple(base_tags) if base_tags else ()
    extra = (extra_tag,) if extra_tag else ()

    def insert(run_text, *style_tags):
        if run_text == "":
            return
        widget.insert("end", run_text, base + extra + tuple(t for t in style_tags if t))

    for token in _md_tokenize_inline(text):
        kind, *rest = _md_classify_inline(token)
        if kind == "text":
            insert(rest[0])
        elif kind == "bold":
            insert(rest[0], tags["bold"])
        elif kind == "italic":
            insert(rest[0], tags["italic"])
        elif kind == "bolditalic":
            insert(rest[0], tags["bold"], tags["italic"])
        elif kind == "strike":
            insert(rest[0], tags["strike"])
        elif kind == "code":
            insert(rest[0], tags["inline_code"])
        elif kind == "math":
            insert(latex_to_unicode(rest[0]), tags["math"])
        elif kind == "link":
            label, _url = rest
            insert(label, tags["link"])
        elif kind == "image":
            label, url = rest
            insert(f"[image: {label or url}]", tags["link"])


def render_markdown_into(widget, text, base_tags=(), md_tags=None):
    """Renders markdown (with $math$) into the widget. base_tags are applied to every run."""
    tags = md_tags or MD_TAG_NAMES
    base = tuple(base_tags) if base_tags else ()

    def insert(run_text, *extra):
        if run_text == "":
            return
        widget.insert("end", run_text, base + tuple(extra))

    lines = text.split("\n")
    i = 0
    n = len(lines)
    in_code_fence = False
    fence_buffer = []

    def flush_code_block():
        insert("\n".join(fence_buffer) + "\n", tags["code_block"])

    while i < n:
        line = lines[i]

        fence_m = _MD_FENCE_RE.match(line)
        if fence_m:
            if not in_code_fence:
                in_code_fence = True
                fence_buffer = []
            else:
                in_code_fence = False
                flush_code_block()
                fence_buffer = []
            i += 1
            continue
        if in_code_fence:
            fence_buffer.append(line)
            i += 1
            continue

        if line.strip() == "":
            insert("\n")
            i += 1
            continue

        if _MD_HR_RE.match(line):
            insert("\u2500" * 40 + "\n", tags["hr"])
            i += 1
            continue

        header_m = _MD_HEADER_RE.match(line)
        if header_m:
            level = len(header_m.group(1))
            content = header_m.group(2)
            render_inline(widget, content, base, tags["header"][min(level - 1, 5)], tags)
            insert("\n")
            i += 1
            continue

        bq_m = _MD_BLOCKQUOTE_RE.match(line)
        if bq_m and line.strip().startswith(">"):
            quote_lines = []
            while i < n and _MD_BLOCKQUOTE_RE.match(lines[i]) and lines[i].strip().startswith(">"):
                quote_lines.append(_MD_BLOCKQUOTE_RE.match(lines[i]).group(1))
                i += 1
            insert("\u2503 ", tags["blockquote_bar"])
            render_inline(widget, " ".join(quote_lines), base, tags["blockquote"], tags)
            insert("\n")
            continue

        if (i + 1 < n) and _MD_TABLE_SEP_RE.match(lines[i + 1]) and "|" in line:
            header_cells = _md_split_table_row(line)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip() != "":
                rows.append(_md_split_table_row(lines[i]))
                i += 1
            widths = [len(c) for c in header_cells]
            for row in rows:
                for idx, cell in enumerate(row):
                    if idx < len(widths):
                        widths[idx] = max(widths[idx], len(cell))
                    else:
                        widths.append(len(cell))

            def fmt_row(cells):
                padded = []
                for idx, w in enumerate(widths):
                    val = cells[idx] if idx < len(cells) else ""
                    padded.append(val.ljust(w))
                return " \u2502 ".join(padded)

            insert(fmt_row(header_cells) + "\n", tags["table_header"])
            insert("\u2500" * (sum(widths) + 3 * (len(widths) - 1)) + "\n", tags["table_rule"])
            for row in rows:
                insert(fmt_row(row) + "\n", tags["table_row"])
            continue

        ul_m = _MD_ULIST_RE.match(line)
        ol_m = _MD_OLIST_RE.match(line)
        if ul_m or ol_m:
            if ul_m:
                indent, _marker, content = ul_m.groups()
            else:
                indent, num, content = ol_m.groups()
            depth = len(indent) // 2
            task_m = _MD_TASK_RE.match(content)
            pad = "  " * depth
            if task_m:
                checked = task_m.group(1).lower() == "x"
                box = "\u2611" if checked else "\u2610"
                insert(f"{pad}{box} ", tags["task_box"])
                render_inline(widget, task_m.group(2), base,
                              tags["task_done"] if checked else None, tags)
            else:
                bullet = "\u2022" if ul_m else f"{num}."
                insert(f"{pad}{bullet} ", tags["list_marker"])
                render_inline(widget, content, base, None, tags)
            insert("\n")
            i += 1
            continue

        render_inline(widget, line, base, None, tags)
        insert("\n")
        i += 1

    if in_code_fence and fence_buffer:
        flush_code_block()


def configure_markdown_tags(widget, font_family, base_size, colors):
    """Registers the markdown tags on the widget using the given theme colours."""
    fg = colors["fg"]
    dim = colors["dim"]
    accent = colors["accent"]
    panel = colors["panel"]

    widget.tag_configure(MD_TAG_NAMES["bold"], font=(font_family, base_size, "bold"))
    widget.tag_configure(MD_TAG_NAMES["italic"], font=(font_family, base_size, "italic"))
    widget.tag_configure(MD_TAG_NAMES["strike"], font=(font_family, base_size, "overstrike"))
    widget.tag_configure(MD_TAG_NAMES["inline_code"], font=(font_family, base_size),
                          background=panel, foreground=accent)
    widget.tag_configure(MD_TAG_NAMES["code_block"], font=(font_family, base_size),
                          background=panel, foreground=fg, lmargin1=14, lmargin2=14)
    widget.tag_configure(MD_TAG_NAMES["math"], font=(font_family, base_size, "italic"),
                          foreground=accent)
    widget.tag_configure(MD_TAG_NAMES["link"], font=(font_family, base_size, "underline"),
                          foreground=accent)
    widget.tag_configure(MD_TAG_NAMES["hr"], foreground=dim)
    widget.tag_configure(MD_TAG_NAMES["blockquote"], font=(font_family, base_size, "italic"),
                          foreground=dim, lmargin1=14, lmargin2=14)
    widget.tag_configure(MD_TAG_NAMES["blockquote_bar"], foreground=dim)
    widget.tag_configure(MD_TAG_NAMES["list_marker"], foreground=accent)
    widget.tag_configure(MD_TAG_NAMES["task_box"], foreground=accent)
    widget.tag_configure(MD_TAG_NAMES["task_done"], font=(font_family, base_size, "overstrike"),
                          foreground=dim)
    widget.tag_configure(MD_TAG_NAMES["table_header"], font=(font_family, base_size, "bold"))
    widget.tag_configure(MD_TAG_NAMES["table_rule"], foreground=dim)
    widget.tag_configure(MD_TAG_NAMES["table_row"], font=(font_family, base_size))
    sizes = [base_size + 8, base_size + 6, base_size + 4, base_size + 2, base_size + 1, base_size]
    for idx, tagname in enumerate(MD_TAG_NAMES["header"]):
        widget.tag_configure(tagname, font=(font_family, sizes[idx], "bold"), foreground=fg,
                              spacing1=4, spacing3=2)

