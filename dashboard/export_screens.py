"""Export one PNG per dashboard section to ``reports/screens/``.

Streamlit paints its content over a websocket *after* the page's ``load`` event, so a naive
headless ``--screenshot`` (which fires at load) captures a blank shell, and ``--virtual-time
-budget`` is worse - it fast-forwards virtual time past the render while the real websocket is
still delivering it. The reliable approach is the Chrome DevTools Protocol: launch a headless
Edge/Chrome with a debugging port, navigate, **poll the live DOM until the section's own content
has actually rendered**, and only then capture. No time guessing.

Both Edge and Chrome ship on Windows and speak CDP, so this needs no browser download - just the
``websockets`` client (already a transitive dependency). Output, via the app's ``?section=N``
single-section mode:

    1_headline.png · 2_race_chart.png · 3_comfort.png · 4_journal.png · 5_llmops.png

Usage - with ``streamlit run dashboard/app.py`` already serving::

    python -m dashboard.export_screens                        # http://localhost:8501
    python -m dashboard.export_screens --url http://localhost:8501 --prefix demo_

``--prefix`` names synthetic-data captures (``demo_*``, gitignored) apart from real-run ones.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

from common.log import get_logger

log = get_logger("dashboard.export_screens")

SECTIONS = {
    1: "headline",
    2: "race_chart",
    3: "comfort",
    4: "journal",
    5: "llmops",
}

BROWSER_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

MIN_PNG_BYTES = 6_000
HEIGHTS = {1: 460, 2: 640, 3: 560, 4: 720, 5: 900}

#: A section has rendered when the DOM carries both the numbered chip (".hive-sec .n") and its
#: real content: a metric card, a plot, or a dataframe. Polled until true (or timeout).
_READY_JS = (
    "(function(){"
    " var sec=document.querySelector('.hive-sec');"
    " var body=document.querySelector("
    "'[data-testid=\"stMetric\"],.stPlotlyChart,[data-testid=\"stDataFrame\"]');"
    " return !!(sec && body);"
    "})()"
)


def find_browser() -> Path:
    for candidate in BROWSER_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No Edge/Chrome found for headless capture; checked: "
        + ", ".join(str(c) for c in BROWSER_CANDIDATES)
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """A headless Edge/Chrome with a CDP endpoint, torn down on exit."""

    def __init__(self, browser: Path, width: int) -> None:
        self.browser = browser
        self.width = width
        self.port = _free_port()
        self._profile = tempfile.mkdtemp(prefix="hive_screens_")
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> Browser:
        self._proc = subprocess.Popen(
            [
                str(self.browser), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--no-first-run", "--no-default-browser-check",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self._profile}",
                f"--window-size={self.width},1000", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._await_ready()
        return self

    def __exit__(self, *exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _await_ready(self, timeout: float = 20.0) -> None:
        """Wait until the CDP HTTP endpoint answers - then per-capture we open page targets."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version",
                                            timeout=1) as resp:
                    json.loads(resp.read())
                    return
            except (OSError, json.JSONDecodeError):
                time.sleep(0.25)
        raise TimeoutError(f"CDP endpoint on :{self.port} never came up")

    def _open_page(self, url: str) -> tuple[str, str]:
        """Open a fresh tab AT ``url`` and return its ``(page_ws_url, target_id)``.

        The page-level websocket - not the browser-level ``/json/version`` one - is where
        ``Page.*`` and ``Runtime.evaluate`` actually work.
        """
        endpoint = f"http://127.0.0.1:{self.port}/json/new?{url}"
        request = urllib.request.Request(endpoint, method="PUT")  # newer Chrome needs PUT
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                info = json.loads(resp.read())
        except urllib.error.HTTPError:
            with urllib.request.urlopen(endpoint, timeout=5) as resp:  # older accepts GET
                info = json.loads(resp.read())
        return info["webSocketDebuggerUrl"], info["id"]

    def _close_page(self, target_id: str) -> None:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/close/{target_id}", timeout=5).read()
        except OSError:
            pass

    async def _capture(self, url: str, out_png: Path, *, height: int,
                       settle_timeout: float) -> bool:
        page_ws, target_id = self._open_page(url)
        try:
            return await self._drive(page_ws, out_png, height=height,
                                     settle_timeout=settle_timeout)
        finally:
            self._close_page(target_id)

    async def _drive(self, page_ws: str, out_png: Path, *, height: int,
                     settle_timeout: float) -> bool:
        async with websockets.connect(page_ws, max_size=64 * 1024 * 1024) as ws:
            msg_id = 0

            async def cmd(method: str, params: dict | None = None) -> dict:
                nonlocal msg_id
                msg_id += 1
                await ws.send(json.dumps({"id": msg_id, "method": method,
                                          "params": params or {}}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == msg_id:
                        return reply.get("result", {})

            await cmd("Page.enable")
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": self.width, "height": height, "deviceScaleFactor": 1,
                       "mobile": False})
            # The tab was already opened AT the target URL via /json/new; no navigate needed.

            # Poll the DOM until the section's real content exists (deterministic, not timed).
            deadline = time.monotonic() + settle_timeout
            ready = False
            while time.monotonic() < deadline:
                result = await cmd("Runtime.evaluate",
                                   {"expression": _READY_JS, "returnByValue": True})
                if result.get("result", {}).get("value") is True:
                    ready = True
                    break
                await asyncio.sleep(0.4)
            await asyncio.sleep(1.2)  # let plots finish their paint after content mounts

            shot = await cmd("Page.captureScreenshot",
                             {"format": "png", "captureBeyondViewport": True})
            data = shot.get("data")
            if not data:
                return False
            out_png.parent.mkdir(parents=True, exist_ok=True)
            out_png.write_bytes(base64.b64decode(data))
            return ready and out_png.stat().st_size >= MIN_PNG_BYTES

    def capture(self, url: str, out_png: Path, *, height: int, settle_timeout: float) -> bool:
        return asyncio.run(self._capture(url, out_png, height=height,
                                         settle_timeout=settle_timeout))


def export_all(url: str, out_dir: Path, *, prefix: str = "", width: int = 1440,
               settle_timeout: float = 25.0) -> dict[int, Path | None]:
    """Capture every section via one shared headless browser. ``{section: path or None}``."""
    browser = find_browser()
    log.info("capturing via %s", browser)
    results: dict[int, Path | None] = {}
    with Browser(browser, width) as chrome:
        for number, name in SECTIONS.items():
            target = out_dir / f"{prefix}{number}_{name}.png"
            section_url = f"{url}/?section={number}&embed=true&static=1"
            ok = False
            for attempt in range(3):
                try:
                    ok = chrome.capture(section_url, target, height=HEIGHTS[number],
                                        settle_timeout=settle_timeout)
                except Exception as exc:  # noqa: BLE001 - report, retry
                    log.warning("capture error on section %d: %s", number, exc)
                    ok = False
                if ok:
                    break
                time.sleep(1.5 * (attempt + 1))
            results[number] = target if ok else None
            status = f"{target.stat().st_size // 1024} kB" if ok else "FAILED (blank/missing)"
            print(f"  section {number} ({name}): {status}")
    return results


def main(argv: list[str] | None = None) -> int:
    from common.config import Settings

    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Export dashboard section screenshots (CDP).")
    parser.add_argument("--url", default="http://localhost:8501")
    parser.add_argument("--out", type=Path, default=settings.repo_root / "reports" / "screens")
    parser.add_argument("--prefix", default="", help="filename prefix (demo_ for synthetic)")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--settle", type=float, default=25.0,
                        help="max seconds to wait for a section to render before capturing")
    args = parser.parse_args(argv)

    print(f"exporting {len(SECTIONS)} sections from {args.url} -> {args.out}")
    results = export_all(args.url, args.out, prefix=args.prefix, width=args.width,
                         settle_timeout=args.settle)
    failed = [n for n, path in results.items() if path is None]
    if failed:
        print(f"FAILED sections: {failed} - is the dashboard running at {args.url}?",
              file=sys.stderr)
        return 1
    print("all sections captured")
    return 0


__all__ = ["SECTIONS", "Browser", "export_all", "find_browser"]


if __name__ == "__main__":
    raise SystemExit(main())
