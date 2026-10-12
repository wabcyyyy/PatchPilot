"""Build docs/interview-guide/site/index.html from the Markdown files in that folder.

Markdown 是唯一事实源;本脚本只做阅读体验(导航/搜索/折叠/标签着色),不产生新内容。
用法: python scripts/build_interview_site.py
"""

from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "interview-guide"
OUT = GUIDE / "site" / "index.html"

TAGS = {
    "代码事实": "fact",
    "合理推断": "infer",
    "实际测试": "test",
    "面试知识": "know",
    "生产化扩展": "prod",
    "风险点": "risk",
    "面试口径": "script",
}
ORDER = [
    "README",
    "00-project-audit",
    "doc-cleanup-report",
    "01-project-overview",
    "02-architecture",
    "03-risk-register",
    "04-code-walkthrough",
    "05-evaluation",
    "06-production-gap",
    "07-ai-knowledge-map",
    "08-interview-questions",
    "09-interview-script",
    "10-claim-check",
    "11-cheatsheet",
    "12-3day-plan",
    "13-mock-interview",
]
CARD_DOCS = {"08-interview-questions", "13-mock-interview"}
FOLLOWUP = re.compile(r"^(?:\*\*)?(?:追问|Follow-up|继续追问)")
FU_SPLIT = re.compile(
    r"^\*{0,2}\s*(追问[^：*]*|Follow-up[^：*]*|继续追问[^：*]*)\s*\*{0,2}：\s*(.*)$", re.S
)


def plain(text: str) -> str:
    """Plain-text label for navigation (markdown markers removed)."""
    return html.escape(MDLINK.sub(r"\1", text.replace("`", "").replace("**", "")))


HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
UL = re.compile(r"^(\s*)[-*]\s+(.*)$")
OL = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
MDLINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\[(?P<label>[^\]]*)\]\((?P<url>[^)\s]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|(?P<pri>\bP[012]\b)",
    re.S,
)


def inline(text: str) -> str:
    """Render the inline subset.

    Spans are matched in one left-to-right pass, so emphasis, links and priority badges
    can never rewrite markdown that sits inside a code span; each leaf is escaped once.
    """
    out: list[str] = []
    pos = 0
    for m in INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        pos = m.end()
        if m.group("code") is not None:
            body = m.group("code")
            cls = TAGS.get(body)
            esc = html.escape(body)
            out.append(f'<span class="tag t-{cls}">{esc}</span>' if cls else f"<code>{esc}</code>")
        elif m.group("url") is not None:
            url = m.group("url")
            stem = url.rsplit("/", 1)[-1]
            # intra-guide markdown links become in-page anchors; a raw .md href is unreadable in a browser
            if stem.endswith(".md") and stem[:-3] in ORDER:
                url = f"#{stem[:-3]}"
            else:
                # Source links are relative to GUIDE, while output lives one level below it.
                if not url.startswith(("#", "/")) and not re.match(r"^[\w+.-]+:", url):
                    url = f"../{url}"
                url = html.escape(url, quote=True)
            out.append(f'<a href="{url}">{inline(m.group("label"))}</a>')
        elif m.group("bold") is not None:
            out.append(f"<strong>{inline(m.group('bold'))}</strong>")
        else:
            pri = m.group("pri")
            out.append(f'<span class="pri p-{pri}">{pri}</span>')
    out.append(html.escape(text[pos:]))
    return "".join(out)


def render_table(rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        """Split on pipes that sit outside code spans (``a|b`` stays one cell)."""
        out: list[str] = []
        buf: list[str] = []
        in_code = False
        for ch in line.strip().strip("|"):
            if ch == "`":
                in_code = not in_code
            if ch == "|" and not in_code:
                out.append("".join(buf).strip())
                buf = []
                continue
            buf.append(ch)
        out.append("".join(buf).strip())
        return out

    head = "".join(f"<th>{inline(c)}</th>" for c in cells(rows[0]))
    body = "".join(
        "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells(r)) + "</tr>" for r in rows[2:]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_list(items: list[tuple[int, bool, str]]) -> str:
    """items: (depth, ordered, text). Nest by depth; wrap follow-up lines in <details>."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for depth, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and stack[-1][0] > depth:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and stack[-1][0] == depth:
            out.append("</li>")
        else:
            out.append(f"<{tag}>")
            stack.append((depth, tag))
        m = FU_SPLIT.match(text) if FOLLOWUP.match(text) else None
        if m:
            head, rest = m.group(1), m.group(2)
            if rest.count("**") % 2:
                rest = rest.replace("**", "", 1)
            out.append(
                f'<li class="fu"><details><summary>{inline(head)}</summary>'
                f"<p>{inline(rest)}</p></details></li>"
            )
        else:
            out.append(f"<li>{inline(text)}</li>")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    out.append("")
    return "".join(out[:-1])


def render(md: str, slug: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Markdown subset -> HTML body plus the heading list used for navigation."""
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    items: list[tuple[int, bool, str]] = []
    rows: list[str] = []
    fence: list[str] | None = None
    card = False
    hindex = 0

    def flush_list() -> None:
        if items:
            out.append(render_list(list(items)))
            items.clear()

    def flush_table() -> None:
        if rows:
            out.append(render_table(list(rows)))
            rows.clear()

    def close_card() -> None:
        nonlocal card
        if card:
            out.append("</details>")
            card = False

    def heading(level: int, title: str) -> None:
        nonlocal hindex, card
        hindex += 1
        anchor = f"{slug}-h{hindex}"
        toc.append((level, title, anchor))
        flush_list()
        flush_table()
        if level == 3 and slug in CARD_DOCS:
            close_card()
            out.append(f'<details class="qa" id="{anchor}"><summary>{inline(title)}</summary>')
            card = True
            return
        close_card()
        out.append(
            f'<h{level} id="{anchor}">{inline(title)}'
            f' <a class="anchor" href="#{anchor}">#</a></h{level}>'
        )

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        raw, line = lines[i], lines[i].rstrip()
        if fence is not None:
            if line.startswith("```"):
                out.append(f"<pre><code>{html.escape(chr(10).join(fence))}</code></pre>")
                fence = None
            else:
                fence.append(raw)
            i += 1
            continue
        if line.startswith("```"):
            flush_list()
            flush_table()
            fence = []
            i += 1
            continue
        if line.startswith("|"):
            flush_list()
            rows.append(line)
            i += 1
            continue
        flush_table()
        if m := HEADING.match(line):
            heading(len(m.group(1)), m.group(2).strip())
        elif re.fullmatch(r"-{3,}", line.strip()):
            flush_list()
            close_card()
            out.append("<hr>")
        elif line.lstrip().startswith(">"):
            flush_list()
            quote: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{inline(' '.join(filter(None, quote)))}</blockquote>")
            continue
        elif m := UL.match(line):
            items.append((len(m.group(1)) // 2, False, m.group(2)))
        elif m := OL.match(line):
            items.append((len(m.group(1)) // 2, True, m.group(3)))
        elif line.strip():
            flush_list()
            out.append(f"<p>{inline(line)}</p>")
        else:
            flush_list()
        i += 1
    flush_list()
    flush_table()
    close_card()
    if fence is not None:
        out.append("<pre><code>(unterminated fence)</code></pre>")
    return "\n".join(out), toc


CSS = """
:root{--bg:#f6f8fb;--panel:#fff;--ink:#1b2027;--dim:#5b6472;--line:#dbe2ea;
--accent:#0b6bcb;--fact:#157f3c;--infer:#8a5a00;--test:#0a5cc0;--know:#6f42c1;
--prod:#b0257a;--risk:#c03024;--script:#4a5462;--soft:#eef2f7;--hover:#e8f1fb;
--even:#fafbfd;--quote:#f2f7fd}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.75 ui-sans-serif,-apple-system,"Segoe UI","Noto Sans CJK SC",sans-serif}
nav{position:fixed;inset:0 auto 0 0;width:300px;overflow:auto;background:var(--panel);
border-right:1px solid var(--line);padding:14px 12px}
nav a{display:block;color:var(--dim);text-decoration:none;padding:3px 8px;
border-radius:6px;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
nav a.l1{color:var(--ink);font-weight:600;margin-top:10px}
nav a.l2{padding-left:16px}nav a.l3{padding-left:26px}
nav a:hover,nav a.on{background:var(--hover);color:var(--accent)}
main{margin-left:300px;padding:24px 40px 120px;max-width:1010px}
article{margin-bottom:56px;padding-bottom:18px;border-bottom:1px solid var(--line)}
h1{font-size:24px;margin:6px 0 18px}h2{font-size:19px;margin:30px 0 12px;color:var(--accent)}
h3{font-size:16px;margin:22px 0 8px}h4{font-size:14px;margin:16px 0 6px;color:var(--dim)}
a{color:var(--accent)}.anchor{opacity:.35;text-decoration:none;font-weight:400}
code{background:var(--soft);padding:1px 5px;border-radius:4px;font-size:12.5px;
font-family:ui-monospace,Consolas,monospace;color:#25303e}
pre{background:#fbfcfe;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
pre code{background:none;padding:0;font-size:12.5px;line-height:1.6;color:#25303e}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13.5px}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--soft)}tr:nth-child(even) td{background:var(--even)}
blockquote{margin:14px 0;padding:8px 14px;border-left:3px solid var(--accent);
background:var(--quote);color:#33404f}
hr{border:0;border-top:1px solid var(--line);margin:24px 0}
.tag{display:inline-block;padding:0 7px;border-radius:999px;font-size:11.5px;
border:1px solid currentColor;font-weight:600;background:#fff}
.t-fact{color:var(--fact)}.t-infer{color:var(--infer)}.t-test{color:var(--test)}
.t-know{color:var(--know)}.t-prod{color:var(--prod)}.t-risk{color:var(--risk)}
.t-script{color:var(--script)}
.pri{font-weight:700;padding:0 5px;border-radius:4px;font-size:12px}
.p-P0{background:#fdebea;color:#a4262c}.p-P1{background:#fff4d9;color:#8a5a00}
.p-P2{background:#e8f5ec;color:#157f3c}
details.qa{border:1px solid var(--line);border-radius:10px;background:var(--panel);
margin:12px 0;padding:0 14px;box-shadow:0 1px 2px rgba(20,30,45,.05)}
details.qa>summary{cursor:pointer;font-weight:600;font-size:15px;padding:10px 0;
list-style:none;color:var(--accent)}
details.qa>summary::before{content:"▸ "}
details.qa[open]>summary::before{content:"▾ "}
li.fu>details>summary{cursor:pointer;color:var(--infer);font-size:13.5px;list-style:none}
li.fu>details>summary::before{content:"↳ "}
li.fu{margin:4px 0}
#bar{position:sticky;top:0;z-index:5;margin:0 0 18px;padding:10px 12px;background:#fffffff2;
border:1px solid var(--line);border-radius:10px;display:flex;gap:8px;align-items:center;
flex-wrap:wrap;box-shadow:0 2px 6px rgba(20,30,45,.06)}
#q{flex:1;min-width:200px;background:#fff;border:1px solid #ccd6e2;color:var(--ink);
padding:7px 10px;border-radius:8px;font-size:14px}
#q:focus{outline:2px solid var(--accent);outline-offset:1px}
button{background:#fff;border:1px solid #ccd6e2;color:#22303f;padding:6px 10px;
border-radius:8px;cursor:pointer;font-size:13px}
button:hover{background:var(--hover)}
.hide{display:none!important}
#count{color:var(--dim);font-size:12.5px}
#menu{display:none}
@media(max-width:760px){
#menu{display:inline-block}nav{transform:translateX(-101%);transition:transform .15s;
box-shadow:2px 0 10px rgba(20,30,45,.12)}
body.nav-open nav{transform:none}main{margin:0;padding:18px}}
@media print{nav,#bar,#menu{display:none}main{margin:0;max-width:none}
article{border:0;page-break-after:auto}}
"""

JS = """
const q=document.getElementById('q'),count=document.getElementById('count');
const arts=[...document.querySelectorAll('article')];
const cards=[...document.querySelectorAll('details')];
let auto=[];
function search(){
  const t=q.value.trim().toLowerCase();let n=0;
  arts.forEach(a=>{const hit=!t||a.textContent.toLowerCase().includes(t);
    a.classList.toggle('hide',!hit);if(hit)n++;});
  count.textContent=t?`可见 ${n} / ${arts.length} 篇`:'';
  if(t){auto=cards.filter(d=>!d.open&&d.textContent.toLowerCase().includes(t));
    auto.forEach(d=>d.open=true);}
  else{auto.forEach(d=>d.open=false);auto=[];}
}
q.addEventListener('input',search);
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}});
const set=v=>cards.forEach(d=>d.open=v);
document.getElementById('open').onclick=()=>set(true);
document.getElementById('close').onclick=()=>set(false);
document.getElementById('p0').onclick=()=>{
  set(false);
  document.querySelectorAll('details.qa').forEach(d=>{
    if(/P0|最重要|生死|核心/.test(d.querySelector('summary').textContent))d.open=true;});
};
document.getElementById('menu').onclick=()=>document.body.classList.toggle('nav-open');
const links=[...document.querySelectorAll('nav a')];
const byId=new Map(links.map(a=>[a.getAttribute('href').slice(1),a]));
const seen=new Set();
const io=new IntersectionObserver(es=>{
  es.forEach(e=>e.isIntersecting?seen.add(e.target.id):seen.delete(e.target.id));
  const cur=arts.find(a=>seen.has(a.id));
  if(!cur)return;
  links.forEach(a=>a.classList.remove('on'));
  byId.get(cur.id)?.classList.add('on');
},{rootMargin:'0px 0px -70% 0px'});
arts.forEach(a=>io.observe(a));
"""


def build() -> int:
    nav: list[str] = ['<a class="l1" href="#top">↑ 顶部</a>']
    arts: list[str] = []
    for slug in ORDER:
        path = GUIDE / f"{slug}.md"
        if not path.exists():
            print(f"skip missing {path.name}")
            continue
        body, toc = render(path.read_text(encoding="utf-8"), slug)
        title = toc[0][1] if toc else slug
        nav.append(f'<a class="l1" href="#{slug}">{plain(title[:44])}</a>')
        for level, text, anchor in toc[1:]:
            if level > 3:
                continue
            nav.append(f'<a class="l{level}" href="#{anchor}">{plain(text[:42])}</a>')
        arts.append(f'<article id="{slug}">{body}</article>')
    legend = " ".join(f'<span class="tag t-{v}">{k}</span>' for k, v in TAGS.items())
    doc = (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>PatchPilot 面试学习资料</title><style>{CSS}</style></head>\n"
        f'<body><nav>{"".join(nav)}</nav>\n<main id="top">'
        '<div id="bar"><input id="q" placeholder="全文搜索（按 / 聚焦）">'
        '<button id="menu">目录</button>'
        '<button id="open">全部展开</button><button id="close">全部折叠</button>'
        '<button id="p0">只展开关键题</button><span id="count"></span></div>\n'
        f'<p class="hint" style="color:var(--dim);font-size:13px">{legend} · 本页由 '
        "<code>scripts/build_interview_site.py</code> 从 "
        "<code>docs/interview-guide/*.md</code> 生成，Markdown 为站点内容源，"
        "产品事实按源码、测试与有效 SPEC/ADR 核对，请勿直接编辑本文件。</p>\n"
        f"{''.join(arts)}</main>\n<script>{JS}</script></body></html>\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(doc):,} bytes, {len(arts)} docs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
