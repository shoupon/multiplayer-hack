"""Who said, asked, decided, and did what, in order. Printed at the end and written as decision-log.html."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from demo.outside import TPE

Channel = Literal["fcl", "local", "owner", "outside"]
CHANNEL_LABEL = {"fcl": "via FCL", "local": "agent's own reasoning", "owner": "owner decision", "outside": "outside service"}


def say(who: str, text: str) -> None:
    print(f"  {who:<18} {text}", flush=True)


def heading(text: str) -> None:
    print(f"\n== {text}", flush=True)


@dataclass(frozen=True)
class Entry:
    at: datetime
    step: int
    actor: str
    channel: Channel
    text: str
    ref: str


@dataclass
class DecisionLog:
    steps: dict[int, str] = field(default_factory=dict)
    entries: list[Entry] = field(default_factory=list)
    step: int = 0

    def stage(self, n: int, title: str) -> None:
        self.step = n
        self.steps[n] = title
        heading(f"{n}. {title}")

    def note(self, actor: str, channel: Channel, text: str, ref: str = "", quiet: bool = False) -> None:
        self.entries.append(Entry(datetime.now(TPE), self.step, actor, channel, text, ref))
        if not quiet:
            say(actor, text)

    def print(self) -> None:
        heading("Decision log")
        for e in self.entries:
            print(f"  {e.at:%H:%M:%S}  {e.actor:<18} [{CHANNEL_LABEL[e.channel]}] {e.text}")

    def write_html(self, path: Path, title: str, summary: list[tuple[str, str]]) -> Path:
        rows = []
        for n, name in self.steps.items():
            rows.append(f'<h2><span class="n">{n}</span>{html.escape(name)}</h2><ol>')
            for e in (e for e in self.entries if e.step == n):
                ref = f'<code>{html.escape(e.ref)}</code>' if e.ref else ""
                rows.append(
                    f'<li class="{e.channel}"><time>{e.at:%H:%M:%S}</time><b>{html.escape(e.actor)}</b>'
                    f'<span class="chip">{CHANNEL_LABEL[e.channel]}</span><p>{html.escape(e.text)} {ref}</p></li>')
            rows.append("</ol>")
        facts = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in summary)
        path.write_text(PAGE.format(title=html.escape(title), facts=facts, rows="\n".join(rows)))
        return path


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Decision log</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a64; --line:#e4e1d8; --card:#ffffff;
  --fcl:#2f6fde; --local:#8a8a83; --owner:#c2410c; --outside:#15803d; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --fg:#ecebe6; --muted:#9c9b94; --line:#2c2b28; --card:#1d1c1a;
  --fcl:#7aa7ff; --local:#8f8e87; --owner:#fb923c; --outside:#4ade80; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, system-ui, sans-serif; }}
main {{ max-width:860px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:24px; margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 24px; }}
dl {{ display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:16px; margin:0 0 32px; }} dt {{ color:var(--muted); }} dd {{ margin:0; }}
h2 {{ font-size:16px; margin:28px 0 8px; display:flex; gap:10px; align-items:center; }}
h2 .n {{ display:inline-grid; place-items:center; width:24px; height:24px; border-radius:50%; background:var(--fg); color:var(--bg); font-size:13px; }}
ol {{ list-style:none; margin:0; padding:0 0 0 11px; border-left:2px solid var(--line); }}
li {{ position:relative; padding:6px 0 10px 18px; }}
li::before {{ content:""; position:absolute; left:-7px; top:12px; width:12px; height:12px; border-radius:50%; background:var(--c); }}
li.fcl {{ --c:var(--fcl); }} li.local {{ --c:var(--local); }} li.owner {{ --c:var(--owner); }} li.outside {{ --c:var(--outside); }}
time {{ color:var(--muted); font-variant-numeric:tabular-nums; margin-right:10px; font-size:13px; }}
.chip {{ margin-left:8px; font-size:12px; color:var(--c); border:1px solid var(--c); border-radius:999px; padding:0 8px; }}
li p {{ margin:2px 0 0; }} code {{ font-size:12px; color:var(--muted); overflow-wrap:anywhere; }}
</style></head>
<body><main>
<h1>{title}</h1><p class="sub">Every message the agents exchanged through FCL, every decision their owners made, and every outside call.</p>
<dl>{facts}</dl>
{rows}
</main></body></html>
"""
