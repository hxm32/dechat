"""Markdown and LaTeX rendering for chat lines, producing escaped HTML.

LaTeX is approximated with Unicode."""
import html
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



_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _esc(text):
    return html.escape(text, quote=False)


def _span(css_class, inner):
    return f'<span class="{css_class}">{inner}</span>' if inner else ""


def render_inline(text, css_class=None):
    """Renders one line of inline markdown. All text is escaped."""
    out = []
    for token in _md_tokenize_inline(text):
        kind, *rest = _md_classify_inline(token)
        if kind == "text":
            out.append(_esc(rest[0]))
        elif kind == "bold":
            out.append(f"<b>{_esc(rest[0])}</b>")
        elif kind == "italic":
            out.append(f"<i>{_esc(rest[0])}</i>")
        elif kind == "bolditalic":
            out.append(f"<b><i>{_esc(rest[0])}</i></b>")
        elif kind == "strike":
            out.append(f"<s>{_esc(rest[0])}</s>")
        elif kind == "code":
            out.append(_span("md-code", _esc(rest[0])))
        elif kind == "math":
            out.append(_span("md-math", _esc(latex_to_unicode(rest[0]))))
        elif kind == "link":
            label, url = rest
            if _SAFE_URL_RE.match(url):
                attr = html.escape(url, quote=True)
                out.append(
                    f'<a class="md-link" href="{attr}" title="{attr}" '
                    f'target="_blank" rel="noopener noreferrer">{_esc(label)}</a>'
                )
            else:
                out.append(_span("md-link", _esc(label)))
        elif kind == "image":
            # Never load remote images, that would leak the viewer's IP to the sender
            label, url = rest
            out.append(_span("md-link", _esc(f"[image: {label or url}]")))
    inner = "".join(out)
    return _span(css_class, inner) if css_class else inner


def render_markdown(text):
    """Renders markdown (with $math$) to HTML meant for a pre-wrap container."""
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    in_code_fence = False
    fence_buffer = []

    def flush_code_block():
        out.append(_span("md-code-block", _esc("\n".join(fence_buffer))))

    while i < n:
        line = lines[i]

        if _MD_FENCE_RE.match(line):
            if in_code_fence:
                flush_code_block()
                fence_buffer = []
            in_code_fence = not in_code_fence
            i += 1
            continue
        if in_code_fence:
            fence_buffer.append(line)
            i += 1
            continue

        if line.strip() == "":
            out.append("\n")
            i += 1
            continue

        if _MD_HR_RE.match(line):
            out.append(_span("md-hr", "\u2500" * 40) + "\n")
            i += 1
            continue

        header_m = _MD_HEADER_RE.match(line)
        if header_m:
            level = min(len(header_m.group(1)), 6)
            out.append(_span(f"md-h md-h{level}", render_inline(header_m.group(2))) + "\n")
            i += 1
            continue

        if _MD_BLOCKQUOTE_RE.match(line) and line.strip().startswith(">"):
            quote_lines = []
            while i < n and _MD_BLOCKQUOTE_RE.match(lines[i]) and lines[i].strip().startswith(">"):
                quote_lines.append(_MD_BLOCKQUOTE_RE.match(lines[i]).group(1))
                i += 1
            out.append(_span("md-bq-bar", "\u2503 ") + render_inline(" ".join(quote_lines), "md-bq") + "\n")
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
                padded = [(cells[idx] if idx < len(cells) else "").ljust(w) for idx, w in enumerate(widths)]
                return _esc(" \u2502 ".join(padded))

            out.append(_span("md-th", fmt_row(header_cells)) + "\n")
            out.append(_span("md-rule", "\u2500" * (sum(widths) + 3 * (len(widths) - 1))) + "\n")
            for row in rows:
                out.append(fmt_row(row) + "\n")
            continue

        ul_m = _MD_ULIST_RE.match(line)
        ol_m = _MD_OLIST_RE.match(line)
        if ul_m or ol_m:
            if ul_m:
                indent, _marker, content = ul_m.groups()
            else:
                indent, num, content = ol_m.groups()
            pad = "  " * (len(indent) // 2)
            task_m = _MD_TASK_RE.match(content)
            if task_m:
                checked = task_m.group(1).lower() == "x"
                box = "\u2611" if checked else "\u2610"
                out.append(pad + _span("md-marker", box + " "))
                out.append(render_inline(task_m.group(2), "md-done" if checked else None) + "\n")
            else:
                bullet = "\u2022" if ul_m else f"{num}."
                out.append(pad + _span("md-marker", bullet + " "))
                out.append(render_inline(content) + "\n")
            i += 1
            continue

        out.append(render_inline(line) + "\n")
        i += 1

    if in_code_fence and fence_buffer:
        flush_code_block()

    result = "".join(out)
    return result[:-1] if result.endswith("\n") else result
