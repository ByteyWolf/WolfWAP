import re
import requests
from html import escape
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from icu import Transliterator
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

_trans = Transliterator.createInstance(
    "Any-Latin; Latin-ASCII"
)

def transliterate(text):
    return _trans.transliterate(text)

REMOVED_TAGS = {
    "head", "script", "style", "noscript", "template", "header", "footer",
    "nav", "aside", "form", "input", "textarea", "button", "select",
    "option", "label", "iframe", "canvas", "svg", "object", "embed",
    "video", "audio", "source", "track", "picture", "meta", "link", "base"
}

INLINE_MAP = {
    "b": "b",
    "strong": "strong",
    "i": "i",
    "em": "em",
    "small": "small",
    "u": "u"
}

INLINEISH = set(INLINE_MAP) | {
    "a", "q", "quote", "span", "code", "mark", "del",
    "s", "sub", "sup", "br"
}

def prepare_source(html):
    soup = BeautifulSoup(html, "html.parser")

    title = ""

    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    for tag in list(soup.find_all(True)):
        if tag.attrs is None:
            continue

        style = tag.get("style", "")

        if (
            tag.has_attr("hidden")
            or tag.get("aria-hidden") == "true"
            or re.search(r"display\s*:\s*none", style, re.I)
        ):
            tag.decompose()

    for name in REMOVED_TAGS:
        for tag in list(soup.find_all(name)):
            if tag.parent is not None:
                tag.decompose()

    for image in list(soup.find_all("img")):
        if image.parent is None:
            continue

        alt = image.get("alt", "").strip()

        if alt:
            image.replace_with(alt)
        else:
            image.decompose()

    return  soup.body or soup, title

def truncate_link_text(text, limit=50):
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= limit:
        return text

    return text[:limit - 3].rstrip() + "..."

def text_token(text, wrappers=()):
    return "text", transliterate(text), tuple(wrappers), None

def link_token(text, href):
    return "link", transliterate(text), (), href

def br_token():
    return "br", "", (), None

def inline_tokens(node, base_url, wrappers=()):
    if isinstance(node, NavigableString):
        text = re.sub(r"\s+", " ", str(node))
        return [text_token(text, wrappers)] if text else []

    if not isinstance(node, Tag) or node.attrs is None:
        return []

    name = node.name.lower()

    if name == "br":
        return [br_token()]

    if name == "a":
        label = truncate_link_text(node.get_text(" ", strip=True))

        if not label:
            return []

        href = node.get("href", "").strip()

        if href and not href.lower().startswith(
            ("javascript:", "data:", "vbscript:")
        ):
            return [link_token(label, urljoin(base_url, href))]

        return [text_token(label, wrappers)]

    if name in {"q", "quote"}:
        result = [text_token('"', wrappers)]

        for child in node.children:
            result.extend(inline_tokens(child, base_url, wrappers))

        result.append(text_token('"', wrappers))
        return result

    mapped = INLINE_MAP.get(name)
    next_wrappers = wrappers + ((mapped,) if mapped else ())

    result = []

    for child in node.children:
        result.extend(inline_tokens(child, base_url, next_wrappers))

    return result

def normalize_tokens(tokens):
    result = []
    previous_ends_space = False

    for kind, text, wrappers, href in tokens:
        if kind != "text":
            result.append((kind, text, wrappers, href))
            previous_ends_space = False
            continue

        text = re.sub(r"\s+", " ", text)

        if previous_ends_space:
            text = text.lstrip()

        if not text or not text.strip():
            continue

        if (
            result
            and result[-1][0] == "text"
            and result[-1][2] == wrappers
        ):
            previous = result[-1]
            result[-1] = (
                "text",
                previous[1] + text,
                wrappers,
                None
            )
        else:
            result.append(("text", text, wrappers, None))

        previous_ends_space = text.endswith(" ")

    return result

def serialize_text(text, wrappers):
    markup = escape(text, quote=False)

    for name in reversed(wrappers):
        markup = f"<{name}>{markup}</{name}>"

    return markup

def serialize_token(token):
    kind, text, wrappers, href = token

    if kind == "text":
        return serialize_text(text, wrappers)

    if kind == "link":
        href = escape(href, quote=True)
        text = escape(text, quote=False)
        return f'<a href="{href}">{text}</a>'

    return "<br/>"

def fitting_prefix(text, wrappers, budget):
    if budget <= 0:
        return ""

    if len(serialize_text(text, wrappers)) <= budget:
        return text

    low = 0
    high = len(text)

    while low < high:
        middle = (low + high + 1) // 2

        if len(serialize_text(text[:middle], wrappers)) <= budget:
            low = middle
        else:
            high = middle - 1

    if low == 0:
        return ""

    prefix = text[:low]
    boundary = prefix.rfind(" ")

    if boundary > 0:
        prefix = prefix[:boundary + 1]

    return prefix

def split_paragraph_tokens(tokens, target=250):
    tokens = normalize_tokens(tokens)
    paragraphs = []
    pieces = []
    content_length = 0
    content_limit = max(1, target - len("<p></p>"))

    def flush():
        nonlocal pieces, content_length

        if pieces:
            paragraphs.append("<p>" + "".join(pieces) + "</p>")

        pieces = []
        content_length = 0

    for token in tokens:
        kind, text, wrappers, href = token

        if kind == "text":
            remaining = text

            while remaining:
                if not pieces:
                    remaining = remaining.lstrip()

                    if not remaining:
                        break

                room = content_limit - content_length
                prefix = fitting_prefix(remaining, wrappers, room)

                if prefix:
                    markup = serialize_text(prefix, wrappers)
                    pieces.append(markup)
                    content_length += len(markup)
                    remaining = remaining[len(prefix):]

                    if remaining:
                        flush()

                    continue

                if pieces:
                    flush()
                    continue

                prefix = fitting_prefix(
                    remaining,
                    wrappers,
                    content_limit
                )

                if not prefix:
                    prefix = remaining[0]

                markup = serialize_text(prefix, wrappers)
                pieces.append(markup)
                content_length += len(markup)
                remaining = remaining[len(prefix):]
                flush()

            continue

        markup = serialize_token(token)

        if pieces and content_length + len(markup) > content_limit:
            flush()

        pieces.append(markup)
        content_length += len(markup)

        if content_length >= content_limit:
            flush()

    flush()
    return paragraphs

def children_to_blocks(parent, base_url):
    result = []
    buffer = []

    def flush_buffer():
        nonlocal buffer

        if buffer:
            result.extend(split_paragraph_tokens(buffer))
            buffer = []

    for child in parent.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                buffer.extend(inline_tokens(child, base_url))

            continue

        if not isinstance(child, Tag) or child.attrs is None:
            continue

        if child.name.lower() in INLINEISH:
            buffer.extend(inline_tokens(child, base_url))
        else:
            flush_buffer()
            result.extend(block_elements(child, base_url))

    flush_buffer()
    return result

def block_elements(node, base_url):
    if isinstance(node, NavigableString):
        text = re.sub(r"\s+", " ", str(node)).strip()

        if not text:
            return []

        return split_paragraph_tokens([text_token(text)])

    if not isinstance(node, Tag) or node.attrs is None:
        return []

    name = node.name.lower()

    if name in {"ul", "ol"}:
        result = []
        index = 1

        for child in node.children:
            if (
                not isinstance(child, Tag)
                or child.attrs is None
                or child.name.lower() != "li"
            ):
                continue

            prefix = f"{index}. " if name == "ol" else "* "
            tokens = [text_token(prefix)]
            tokens.extend(inline_tokens(child, base_url))
            result.extend(split_paragraph_tokens(tokens))
            index += 1

        return result

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return split_paragraph_tokens(
            inline_tokens(node, base_url, ("b",))
        )

    if name in {"p", "blockquote", "pre", "li"}:
        return split_paragraph_tokens(
            inline_tokens(node, base_url)
        )

    if name == "hr":
        return ["<p>-----</p>"]

    if name in INLINEISH:
        return split_paragraph_tokens(
            inline_tokens(node, base_url)
        )

    return children_to_blocks(node, base_url)

def translate_to_wml_elements(response):
    source, title = prepare_source(response.text)
    return children_to_blocks(source, response.url), title

def page_href(page_url, startfrom):
    parts = urlsplit(page_url)

    query = [
        (key, value)
        for key, value in parse_qsl(
            parts.query,
            keep_blank_values=True
        )
        if key != "startfrom"
    ]

    query.append(("startfrom", str(startfrom)))

    return urlunsplit((
        "",
        "",
        parts.path or "/",
        urlencode(query),
        ""
    ))

def paginate_wml(elements, page_url, startfrom=1, limit=500):
    try:
        startfrom = max(1, int(startfrom))
    except (TypeError, ValueError):
        startfrom = 1

    index = startfrom - 1
    selected = []
    used = 0

    while index < len(elements):
        element = elements[index]

        if used + len(element) > limit:
            break

        selected.append(element)
        used += len(element)
        index += 1

    if index < len(elements):
        while True:
            href = escape(
                page_href(page_url, index + 1),
                quote=True
            )

            next_link = (
                f'<p><a href="{href}">'
                f'Next page...</a></p>'
            )

            if used + len(next_link) <= limit:
                selected.append(next_link)
                break

            if not selected:
                selected.append(next_link)
                break

            removed = selected.pop()
            used -= len(removed)
            index -= 1

    return "".join(selected)

def build_wml_deck(inner, title="WolfWAP"):
    title = escape(title, quote=True)

    return (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE wml PUBLIC "-//WAPFORUM//DTD WML 1.1//EN" '
        '"http://www.wapforum.org/DTD/wml_1.1.xml">\n'
        f'<wml><card id="main" title="{title}">'
        f'{inner}'
        f'</card></wml>'
    )

def clean_page(
    response,
    page_url,
    startfrom=1,
    limit=500,
    title="WolfWAP"
):
    elements = translate_to_wml_elements(response)
    inner = paginate_wml(
        elements,
        page_url,
        startfrom,
        limit
    )
    return build_wml_deck(inner, title)

def extract_startfrom(url):
    parts = urlsplit(url)

    startfrom = 1

    filtered = []

    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "startfrom":
            try:
                startfrom = int(value)
            except ValueError:
                pass
        else:
            filtered.append((key, value))

    clean_url = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(filtered),
        parts.fragment
    ))

    return startfrom, clean_url

def resolve_page(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
    }
    # gotta extract the startfrom parameter and obliterate it from the url
    startfrom, clean_url = extract_startfrom(url)
    response = requests.get(clean_url, headers=headers)
    response.encoding = 'utf-8'
    ctype = response.headers.get("Content-Type", "").lower()
    if "text/vnd.wap.wml" in ctype or url.endswith(".wml"):
        return response.text

    elements, title = translate_to_wml_elements(response)
    if title == "":
        title = urlsplit(clean_url).netloc

    print("ELEMENT COUNT:", len(elements))

    page1 = paginate_wml(
        elements,
        clean_url,
        startfrom,
        750
    )

    return build_wml_deck(page1, transliterate(title)[:20])