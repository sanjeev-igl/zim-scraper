import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Response,
    async_playwright,
)
from playwright_stealth import Stealth

from models import BreakdownSection, LineItem, PriceBreakdown, Sailing

load_dotenv()

COOKIES_FILE = Path("session_cookies.json")
OUTPUT_DIR = Path("output")
ZIM_BASE = "https://my.zim.com"
LOGIN_URL = f"{ZIM_BASE}/app/login"
SPOT_URL = f"{ZIM_BASE}/app/booking/spot"

BREAKDOWN_SECTIONS = [
    "Ocean Freight",
    "Freight Charges",
    "Export Charges",
    "Import Charges",
]

CARD_SELECTORS = [
    'li.kontainers-booking-option',   # ✅ Exact ZIM selector from real HTML
    '[class*="booking-option"]',
    '[class*="sailing-result"]',
    '[class*="result-row"]',
    "table tbody tr",
]

# ✅ FIX 1: Correct panel selector — ZIM uses price-breakdown-wrapper, not modal-global
PANEL_SELECTOR = '.price-breakdown-wrapper'

# ✅ FIX 2: Correct close button — ZIM uses span.close-icon
CLOSE_SELECTORS = [
    '.close-icon',
    'span.fa-times',
    'button[aria-label*="close" i]',
    'button[aria-label*="dismiss" i]',
    '[class*="modal"] button[class*="close"]',
    'button:has-text("×")',
    'button:has-text("✕")',
    '[class*="close-btn"]',
]


class ZimScraper:
    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 2000,
        debug: bool = False,
        manual_mode: bool = True,
        max_pages: int = 5,
        timeout_seconds: float = 600.0,
    ):
        self.email: str = os.getenv("ZIM_EMAIL", "")
        self.password: str = os.getenv("ZIM_PASSWORD", "")
        self.headless = headless
        self.slow_mo = slow_mo
        self.debug = debug
        self.manual_mode = manual_mode
        self.max_pages = max_pages
        self.timeout_seconds = timeout_seconds
        self._api_cache: dict[str, Any] = {}

    _STEALTH_SCRIPT = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'permissions', {
            get: () => ({ query: () => Promise.resolve({ state: 'granted' }) })
        });
    """

    async def scrape(self, config: dict) -> list[dict]:
        OUTPUT_DIR.mkdir(exist_ok=True)

        async with Stealth().use_async(async_playwright()) as pw:
            browser = await self._launch_browser(pw)
            context = await self._load_context(browser)
            page = await context.new_page()
            page.on("response", self._on_response)

            if self.manual_mode:
                await self._manual_navigate(page)
            else:
                await self._ensure_logged_in(page)
                await self._navigate_to_results(page, config)

            all_sailings: list[Sailing] = []
            page_num = 1
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                elapsed = time.monotonic() - (deadline - self.timeout_seconds)
                remaining = deadline - time.monotonic()
                print(f"\n--- Page {page_num} --- ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)")
                batch = await self._extract_page(page, deadline)
                all_sailings.extend(batch)
                print(f"  Extracted {len(batch)} sailings (running total: {len(all_sailings)})")

                if time.monotonic() >= deadline:
                    print(f"  Time limit ({self.timeout_seconds:.0f}s) reached — stopping.")
                    break
                if page_num >= self.max_pages:
                    print(f"  Page limit ({self.max_pages}) reached — stopping.")
                    break
                if not await self._go_next_page(page):
                    break
                page_num += 1

            await self._save_context(context)

            if self.debug:
                api_file = OUTPUT_DIR / "debug_api_responses.json"
                api_file.write_text(
                    json.dumps(self._api_cache, indent=2, ensure_ascii=False, default=str)
                )
                print(f"\nDebug: {len(self._api_cache)} API responses → {api_file}")

            await browser.close()

        return [s.to_dict() for s in all_sailings]

    # ------------------------------------------------------------------
    # Browser launch + session management
    # ------------------------------------------------------------------

    async def _launch_browser(self, pw):
        stealth_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        try:
            browser = await pw.chromium.launch(
                channel="chrome",
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=stealth_args,
            )
            print("Launched: Google Chrome (real browser fingerprint)")
            return browser
        except Exception:
            print("Chrome not found — falling back to Playwright Chromium.")
            return await pw.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=stealth_args,
            )

    async def _load_context(self, browser) -> BrowserContext:
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36"
            ),
        )
        await ctx.add_init_script(self._STEALTH_SCRIPT)

        if COOKIES_FILE.exists():
            cookies = json.loads(COOKIES_FILE.read_text())
            await ctx.add_cookies(cookies)
            print("Loaded saved session cookies.")
        return ctx

    async def _save_context(self, ctx: BrowserContext) -> None:
        cookies = await ctx.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
        print("Session cookies saved.")

    # ------------------------------------------------------------------
    # Manual mode
    # ------------------------------------------------------------------

    async def _manual_navigate(self, page: Page) -> None:
        await page.goto(ZIM_BASE, wait_until="domcontentloaded", timeout=30_000)
        print("\n" + "=" * 60)
        print("  MANUAL MODE — browser is now open")
        print("=" * 60)
        print("  Steps:")
        print("  1. Log in to ZIM (my.zim.com)")
        print("  2. Go to Booking → Spot")
        print("  3. Fill Origin, Destination, Container and click Search")
        print("  4. Wait for the results list to appear on screen")
        print("  5. Come back here and press Enter")
        print("=" * 60)
        input("\n  → Press Enter when results are visible in the browser: ")
        print("\nResuming — extracting data from current page…")
        await page.wait_for_load_state("networkidle", timeout=15_000)

    # ------------------------------------------------------------------
    # Network interception
    # ------------------------------------------------------------------

    async def _on_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        keywords = ("spot", "sailing", "schedule", "booking", "rate", "price", "freight")
        if any(k in response.url.lower() for k in keywords):
            try:
                self._api_cache[response.url] = await response.json()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _ensure_logged_in(self, page: Page) -> None:
        await page.goto(SPOT_URL, wait_until="networkidle", timeout=30_000)
        on_login = (
            "login" in page.url.lower()
            or await page.locator('input[type="password"]').count() > 0
        )
        if on_login:
            await self._login(page)

    async def _login(self, page: Page) -> None:
        print("Logging in…")
        if not self.email or not self.password:
            raise RuntimeError("Set ZIM_EMAIL and ZIM_PASSWORD in your .env file.")

        if "login" not in page.url.lower():
            await page.goto(LOGIN_URL, wait_until="networkidle")

        for sel in [
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
            'input[placeholder*="email" i]',
            'input[placeholder*="user" i]',
        ]:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.fill(self.email)
                break

        await page.locator('input[type="password"]').first.fill(self.password)

        for sel in [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
            'button:has-text("Login")',
        ]:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click()
                break

        try:
            await page.wait_for_function(
                'window.location.href.indexOf("login") === -1',
                timeout=30_000,
            )
        except Exception:
            print("Waiting for manual completion (2FA / CAPTCHA)…")
            await page.wait_for_function(
                'window.location.href.indexOf("login") === -1',
                timeout=120_000,
            )

        print("Login successful.")
        if SPOT_URL not in page.url:
            await page.goto(SPOT_URL, wait_until="networkidle")

    # ------------------------------------------------------------------
    # Navigate to search results
    # ------------------------------------------------------------------

    async def _navigate_to_results(self, page: Page, config: dict) -> None:
        start_url: str = config.get("start_url", "").strip()
        if start_url:
            await page.goto(start_url, wait_until="networkidle")
            return

        search = config.get("search", {})
        if not search or (not search.get("origin") and not search.get("destination")):
            await page.wait_for_load_state("networkidle")
            return

        print("Filling search form…")
        if search.get("origin"):
            await self._fill_port(page, "origin", search["origin"])
        if search.get("destination"):
            await self._fill_port(page, "destination", search["destination"])

        if search.get("container_type"):
            ctype = search["container_type"]
            for sel in [
                'select[name*="container" i]',
                '[class*="container-type"] select',
                '[class*="equipment"] select',
            ]:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.select_option(label=ctype)
                    break

        search_btn = page.locator('button:has-text("Search")').first
        if await search_btn.count():
            await search_btn.click()

        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

    async def _fill_port(self, page: Page, field: str, value: str) -> None:
        for sel in [
            f'input[placeholder*="{field}" i]',
            f'input[name*="{field}" i]',
            f'[class*="{field}" i] input',
            f'[data-field*="{field}" i] input',
        ]:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.fill(value)
                await asyncio.sleep(1.2)
                option = page.locator(
                    '[role="option"], [class*="dropdown"] li, [class*="suggestion"] li'
                ).first
                if await option.count():
                    await option.click()
                return

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _go_next_page(self, page: Page) -> bool:
        for sel in [
            'button:has-text("Next")',
            'a:has-text("Next")',
            '[aria-label="Next page"]',
            '[class*="pagination"] [class*="next"]:not([disabled])',
            'button:has-text("Load More")',
            'button:has-text("Show More")',
        ]:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_enabled():
                await btn.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(1.5)
                return True
        return False

    # ------------------------------------------------------------------
    # Per-page extraction
    # ------------------------------------------------------------------

    async def _extract_page(self, page: Page, deadline: float = float("inf")) -> list[Sailing]:
        await asyncio.sleep(1.5)

        cards: Optional[Locator] = None
        for sel in CARD_SELECTORS:
            loc = page.locator(sel)
            if await loc.count() > 0:
                cards = loc
                print(f"  Card selector matched: {sel!r}")
                break

        if cards is None:
            print("  WARNING: No booking cards found on this page.")
            ss_path = OUTPUT_DIR / "debug_no_cards.png"
            html_path = OUTPUT_DIR / "debug_page.html"
            await page.screenshot(path=str(ss_path), full_page=True)
            html_path.write_text(await page.content(), encoding="utf-8")
            print(f"  Screenshot → {ss_path}")
            print(f"  Page HTML  → {html_path}")
            return []

        count = await cards.count()
        print(f"  {count} cards found")

        sailings: list[Sailing] = []
        for i in range(count):
            if time.monotonic() >= deadline:
                print(f"  Time limit reached after {i} cards — stopping early.")
                break
            card = cards.nth(i)
            try:
                s = await self._extract_card(page, card, i)
                if s:
                    sailings.append(s)
            except Exception as exc:
                print(f"  Card {i} error: {exc}")
        return sailings

    # ------------------------------------------------------------------
    # Single card extraction
    # ------------------------------------------------------------------

    async def _extract_card(self, page: Page, card: Locator, card_index: int) -> Optional[Sailing]:
        raw = (await card.inner_text()).strip()
        if not raw:
            return None

        await card.scroll_into_view_if_needed()

        # ✅ All selectors from real ZIM HTML
        vessel = await self._txt(card, '.vessel-info div:first-child')
        voyage = await self._txt(card, '[class*="voyage"]')
        etd = await self._txt(card, '.information .date')      # first .date = ETD
        eta_locs = card.locator('.information .date')
        eta = (await eta_locs.nth(1).inner_text()).strip() if await eta_locs.count() > 1 else ''
        duration_txt = await self._txt(card, '.days')
        pol = await self._txt(card, '.additional-text')         # first = POL
        pod_locs = card.locator('.additional-text')
        pod = (await pod_locs.nth(1).inner_text()).strip() if await pod_locs.count() > 1 else ''
        route_type = await self._txt(card, '.information.duration .additional', default='Direct')
        # ✅ Cutoffs — real HTML: .kontainer-booking-dates .cutoff .value (3 cutoffs)
        cutoff_values = card.locator('.kontainer-booking-dates .cutoff .value')
        vgm = (await cutoff_values.nth(0).inner_text()).strip() if await cutoff_values.count() > 0 else ''
        gate = (await cutoff_values.nth(1).inner_text()).strip() if await cutoff_values.count() > 1 else ''
        doc = (await cutoff_values.nth(2).inner_text()).strip() if await cutoff_values.count() > 2 else ''
        containers = ''

        # ✅ Extract price — use JS to get only the direct text of .price-value, not children
        # The div contains child elements (Price Breakdown span, No Allocation) which pollute inner_text
        async def get_price_text(selector: str) -> Optional[str]:
            try:
                el = card.locator(selector).first
                if await el.count():
                    # Get only the direct text node, not child elements
                    txt = await page.evaluate(
                        """el => {
                            for (const node of el.childNodes) {
                                if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                                    return node.textContent.trim();
                                }
                            }
                            return el.querySelector('.price-value') ?
                                Array.from(el.querySelector('.price-value').childNodes)
                                    .filter(n => n.nodeType === 3)
                                    .map(n => n.textContent.trim())
                                    .join('') : '';
                        }""",
                        await el.element_handle()
                    )
                    return txt or None
            except Exception:
                pass
            return None

        basic_price_txt = await get_price_text('.kontainer-booking-price.basic .price-value')
        premium_price_txt = await get_price_text('.kontainer-booking-price.premium .price-value')
        if basic_price_txt and 'not available' in basic_price_txt.lower():
            basic_price_txt = None
        if premium_price_txt and 'not available' in premium_price_txt.lower():
            premium_price_txt = None

        # ✅ Tags from .transit-status-chip, allocation from .no-allocation
        tag_els = card.locator('.transit-status-chip')
        tags = []
        for i in range(await tag_els.count()):
            tags.append((await tag_els.nth(i).inner_text()).strip())
        no_alloc = await card.locator('.kontainer-booking-price.no-allocation').count() > 0

        basic_bd, premium_bd = await self._extract_breakdowns(page, card, card_index)

        return Sailing(
            vessel=vessel,
            voyage=voyage,
            pol=pol,
            pod=pod,
            route_type=route_type or "Direct",
            etd=etd,
            eta=eta,
            duration_days=self._parse_days(duration_txt),
            vgm_cutoff=vgm,
            last_gate_in=gate,
            doc_cutoff=doc,
            tags=tags,
            allocation_available=not no_alloc,
            containers=containers,
            basic_price=self._parse_price(basic_price_txt),
            premium_price=self._parse_price(premium_price_txt),
            basic_breakdown=basic_bd,
            premium_breakdown=premium_bd,
        )

    # ------------------------------------------------------------------
    # Price breakdown — JS click + correct ZIM selectors
    # ------------------------------------------------------------------

    async def _extract_breakdowns(
        self, page: Page, card: Locator, card_index: int
    ) -> tuple[Optional[PriceBreakdown], Optional[PriceBreakdown]]:
        route_num = card_index + 1
        basic_id = f"kontainer_booking_option_p2p_route_{route_num}_basic_price_breakdown"
        premium_id = f"kontainer_booking_option_p2p_route_{route_num}_premium_price_breakdown"

        basic_bd = await self._js_click_and_extract(page, basic_id, "Basic")
        premium_bd = await self._js_click_and_extract(page, premium_id, "Premium")

        return basic_bd, premium_bd

    async def _js_click_and_extract(
        self, page: Page, element_id: str, label: str
    ) -> Optional[PriceBreakdown]:
        try:
            el = await page.query_selector(f"#{element_id}")
            if not el:
                print(f"    Panel ({label}): #{element_id} not found, skipping.")
                return None

            # ✅ JS click bypasses overlay div
            await page.evaluate(f"document.getElementById('{element_id}').click()")

            # ✅ FIX 3: Wait for the correct ZIM panel — price-breakdown-wrapper
            await page.wait_for_selector(
                PANEL_SELECTOR,
                state="visible",
                timeout=10_000,
            )
            await asyncio.sleep(0.6)

            if self.debug:
                await page.screenshot(
                    path=str(OUTPUT_DIR / f"debug_panel_{label.lower()}.png")
                )

            bd = await self._scrape_panel(page)
            return bd

        except Exception as exc:
            print(f"    Panel ({label}) error: {exc}")
            return None
        finally:
            await self._close_panel(page)

    async def _scrape_panel(self, page: Page) -> PriceBreakdown:
        panel = page.locator(PANEL_SELECTOR).last

        # ✅ Total price is inside .total-section .body-item with label "Total per Container Type"
        # Structure: .price-breakdown-item.total-section > .price-breakdown-body > .body-item
        # <span class="body-item-label">Total per Container Type</span>
        # <div class="body-item-value"><span>USD</span><span>633.17</span></div>
        total_txt = None
        total_body_items = panel.locator('.total-section .body-item')
        for i in range(await total_body_items.count()):
            item = total_body_items.nth(i)
            lbl = await self._txt(item, '.body-item-label')
            if 'total per container' in lbl.lower():
                spans = item.locator('.body-item-value span')
                if await spans.count() >= 2:
                    total_txt = (await spans.nth(1).inner_text()).strip()
                break

        # Container type from .details-value (containers-section)
        container_type = await self._txt(panel, '.details-value')

        currency = "USD"

        sections: list[BreakdownSection] = []
        for name in BREAKDOWN_SECTIONS:
            section = await self._scrape_section(page, panel, name)
            if section is not None:
                sections.append(section)

        return PriceBreakdown(
            container_type=container_type or None,
            currency=currency,
            sections=sections,
            total_per_container=self._parse_price(total_txt),
        )

    async def _scrape_section(
        self, page: Page, panel: Locator, section_name: str
    ) -> Optional[BreakdownSection]:
        # ✅ Real panel HTML structure:
        # .price-breakdown-item-wrapper
        #   └── .price-breakdown-item
        #         ├── .header-item[aria-label="Click to expand OceanFreight"]
        #         │     └── .text "Ocean Freight"
        #         └── .price-breakdown-body   ← always in DOM, no click needed
        #               └── .body-item (multiple)
        #
        # The body is ALWAYS rendered — no click needed to expand.
        # Find the .price-breakdown-item that contains .text matching our section name,
        # then grab its sibling .price-breakdown-body directly.

        # Find the wrapper whose .text span matches the section name
        item_wrapper = panel.locator(
            f'.price-breakdown-item:has(.text:has-text("{section_name}")):not(.total-section)'
        ).first

        if not await item_wrapper.count():
            return None

        content = item_wrapper.locator('.price-breakdown-body').first
        if not await content.count():
            return BreakdownSection(name=section_name, items=[])

        items = await self._extract_line_items(content)
        return BreakdownSection(name=section_name, items=items)

    async def _extract_line_items(self, content: Locator) -> list[LineItem]:
        items: list[LineItem] = []

        # ✅ FIX 8: Strategy 0 — ZIM specific .body-item structure
        # <div class="body-item">
        #   <span class="body-item-label">OCEAN FREIGHT (FRT)</span>
        #   <div class="body-item-value"><span>USD</span><span>4</span></div>
        # </div>
        body_items = content.locator('.body-item')
        if await body_items.count():
            for i in range(await body_items.count()):
                item = body_items.nth(i)
                label = (await self._txt(item, '.body-item-label')).strip()
                spans = item.locator('.body-item-value span')
                spans_count = await spans.count()
                if spans_count >= 2:
                    currency = (await spans.nth(0).inner_text()).strip()
                    amount_txt = (await spans.nth(1).inner_text()).strip()
                    # Skip subtotal rows — only keep individual charge rows
                    if label and 'subtotal' not in label.lower():
                        items.append(LineItem(
                            charge=label,
                            amount=self._parse_price(amount_txt),
                            currency=currency,
                        ))
            if items:
                return items

        # Fallback Strategy 1: table rows
        rows = content.locator("tr")
        if await rows.count():
            for i in range(await rows.count()):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()
                if cell_count < 2:
                    continue
                charge = (await cells.nth(0).inner_text()).strip()
                amount_txt = (await cells.nth(cell_count - 1).inner_text()).strip()
                currency = "USD"
                if cell_count >= 3:
                    mid = (await cells.nth(1).inner_text()).strip()
                    if re.match(r'^[A-Z]{3}$', mid):
                        currency = mid
                skip_headers = {"charge", "amount", "description", "total", "fee", "name"}
                if charge and charge.lower() not in skip_headers:
                    items.append(
                        LineItem(
                            charge=charge,
                            amount=self._parse_price(amount_txt),
                            currency=currency,
                        )
                    )
            if items:
                return items

        # Fallback Strategy 2: generic div/li rows
        rows2 = content.locator('[class*="row"], [class*="item"], [class*="charge"], li')
        for i in range(await rows2.count()):
            row = rows2.nth(i)
            txt = (await row.inner_text()).strip()
            if not txt:
                continue
            m = re.search(r'\$?\s*([\d,]+\.?\d*)\s*$', txt)
            if m:
                amount = float(m.group(1).replace(",", ""))
                charge = txt[: m.start()].strip().rstrip(":- \t")
                items.append(LineItem(charge=charge or "Unknown", amount=amount, currency="USD"))

        return items

    async def _close_panel(self, page: Page) -> None:
        for sel in CLOSE_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    await asyncio.sleep(0.4)
                    return
            except Exception:
                pass
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _txt(loc: Locator, selector: str, default: str = "") -> str:
        try:
            el = loc.locator(selector).first
            if await el.count():
                return (await el.inner_text()).strip() or default
        except Exception:
            pass
        return default

    @staticmethod
    def _parse_price(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        cleaned = text.replace(",", "").replace("$", "")
        m = re.search(r'[\d]+\.?\d*', cleaned)
        return float(m.group()) if m else None

    @staticmethod
    def _parse_days(text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        m = re.search(r'(\d+)\s*day', text, re.IGNORECASE)
        return int(m.group(1)) if m else None