from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

LOGIN_URL = "https://bocdataext.hi.wcicloud.com/login.aspx"
SEARCH_URL = "https://bocdataext.hi.wcicloud.com/search.aspx"
DEBUG_DIR = Path(__file__).resolve().parent / "debug"

LOGIN_USER_ID = "ctl00_ContentPlaceHolder1_LoginControl1_tbUser"
LOGIN_PASSWORD_ID = "ctl00_ContentPlaceHolder1_LoginControl1_tbPassword"
LOGIN_BUTTON_ID = "ctl00_ContentPlaceHolder1_LoginControl1_btnLogin"

CONDO_RADIO_ID = "ctl00_ContentPlaceHolder1_SearchControl1_rlist_legal_2"
CONDO_POSTBACK_TARGET = "ctl00$ContentPlaceHolder1$SearchControl1$rlist_legal$2"
SEARCH_BUTTON_ID = "ctl00_ContentPlaceHolder1_btnSearch2"
PLATTED_CLEAR_ID = "ctl00_ContentPlaceHolder1_SearchControl1_platted_clear"
LEGAL_PLAT_LABEL_ID = "ctl00_ContentPlaceHolder1_SearchControl1_lblLegalPlat"

# RecordEASE reuses the platted plat/lot inputs for condominium name and unit(s).
CONDO_NAME_FIELD_IDS = (
    "ctl00_ContentPlaceHolder1_SearchControl1_tbPlatName",
    "ctl00_ContentPlaceHolder1_SearchControl1_tbCondoName",
    "ctl00_ContentPlaceHolder1_SearchControl1_tbCondominiumName",
)
CONDO_UNITS_FIELD_IDS = (
    "ctl00_ContentPlaceHolder1_SearchControl1_tbLots",
    "ctl00_ContentPlaceHolder1_SearchControl1_tbUnits",
    "ctl00_ContentPlaceHolder1_SearchControl1_tbCondoUnits",
    "ctl00_ContentPlaceHolder1_SearchControl1_tbUnit",
)

MORTGAGE_CODES = frozenset({"M", "MFS"})
RELEASE_CODE = "R"
LIEN_CODE = "NL"

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "document_number": ("doc", "document", "doc #", "document #", "doc number", "document number"),
    "instrument_code": ("instrument", "inst", "type", "instrument code", "inst code", "doc type"),
    "recording_date": ("date", "recorded", "recording date", "rec date", "record date"),
    "grantor": ("grantor", "grantors"),
    "grantee": ("grantee", "grantees"),
    "description": ("description", "legal", "remarks", "comment"),
}


@dataclass
class BocDocument:
    boc_tmk: str
    document_number: str = ""
    instrument_code: str = ""
    recording_date: str = ""
    grantor: str = ""
    grantee: str = ""
    description: str = ""
    raw_fields: dict[str, str] = field(default_factory=dict)


class _ResultsTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._cell_parts: list[str] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            cell = re.sub(r"\s+", " ", "".join(self._cell_parts)).strip()
            if self._current_row is not None:
                self._current_row.append(cell)
            self._in_cell = False
            self._cell_parts = []
        elif tag == "tr" and self._in_row:
            if self._current_table is not None and self._current_row is not None:
                if any(cell.strip() for cell in self._current_row):
                    self._current_table.append(self._current_row)
            self._in_row = False
            self._current_row = None
        elif tag == "table" and self._in_table:
            if self._current_table:
                self._tables.append(self._current_table)
            self._in_table = False
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def _canonical_header(value: str) -> str | None:
    normalized = _normalize_header(value)
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized in aliases:
            return canonical
    return None


def _looks_like_results_table(table: list[list[str]]) -> bool:
    if len(table) < 2:
        return False
    header = table[0]
    canonical = {_canonical_header(cell) for cell in header}
    return "instrument_code" in canonical or "document_number" in canonical


def _select_results_table(tables: list[list[list[str]]]) -> list[list[str]] | None:
    candidates = [table for table in tables if _looks_like_results_table(table)]
    if not candidates:
        return None
    return max(candidates, key=len)


def _split_document_number_and_instrument(
    document_number: str,
    instrument_code: str,
) -> tuple[str, str]:
    document_number = document_number.strip()
    instrument_code = instrument_code.strip().upper()
    if document_number.lower() == "reference documents":
        return "", ""
    match = re.match(r"^(.+?)/([A-Z]+)$", document_number)
    if match:
        document_number = match.group(1).strip()
        if not instrument_code:
            instrument_code = match.group(2).strip().upper()
    return document_number, instrument_code


def _row_to_document(boc_tmk: str, header: list[str], values: list[str]) -> BocDocument | None:
    mapping: dict[str, str] = {}
    for index, label in enumerate(header):
        canonical = _canonical_header(label)
        if canonical is None:
            continue
        value = values[index].strip() if index < len(values) else ""
        mapping[canonical] = value

    instrument = mapping.get("instrument_code", "").upper()
    document_number = mapping.get("document_number", "")
    if not instrument and not document_number:
        joined = " ".join(values).upper()
        for code in sorted(MORTGAGE_CODES | {RELEASE_CODE, LIEN_CODE}, key=len, reverse=True):
            if re.search(rf"\b{re.escape(code)}\b", joined):
                instrument = code
                break
        if not document_number:
            match = re.search(r"\b(\d{4,})\b", joined)
            if match:
                document_number = match.group(1)

    document_number, instrument = _split_document_number_and_instrument(
        document_number,
        instrument,
    )
    if not instrument and not document_number:
        return None

    return BocDocument(
        boc_tmk=boc_tmk,
        document_number=document_number,
        instrument_code=instrument,
        recording_date=mapping.get("recording_date", ""),
        grantor=mapping.get("grantor", ""),
        grantee=mapping.get("grantee", ""),
        description=mapping.get("description", ""),
        raw_fields={label: values[i] if i < len(values) else "" for i, label in enumerate(header)},
    )


def parse_results_html(html: str, boc_tmk: str) -> list[BocDocument]:
    parser = _ResultsTableParser()
    parser.feed(html)
    table = _select_results_table(parser._tables)
    if table is None:
        return []

    header, *rows = table
    documents: list[BocDocument] = []
    for row in rows:
        document = _row_to_document(boc_tmk, header, row)
        if document is not None:
            documents.append(document)
    return documents


class BocScraper:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        headless: bool = False,
        request_delay: tuple[float, float] = (2.0, 4.0),
        max_retries: int = 2,
    ) -> None:
        self.username = username
        self.password = password
        self.headless = headless
        self.request_delay = request_delay
        self.max_retries = max_retries
        self._logged_in = False

    def login(self, driver) -> None:
        driver.get(LOGIN_URL)
        self._sleep(short=True)
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, 30)
        user_field = wait.until(EC.presence_of_element_located((By.ID, LOGIN_USER_ID)))
        password_field = driver.find_element(By.ID, LOGIN_PASSWORD_ID)
        user_field.clear()
        user_field.send_keys(self.username)
        password_field.clear()
        password_field.send_keys(self.password)
        driver.find_element(By.ID, LOGIN_BUTTON_ID).click()
        self._sleep()
        if "login.aspx" in driver.current_url.lower():
            self._save_debug("login_failed.html", driver.page_source)
            raise RuntimeError("BOC login failed; still on login page")
        self._logged_in = True
        logger.info("BOC login successful")

    def search_condominium(
        self,
        driver,
        condominium_name: str,
        boc_unit: str,
        boc_tmk: str,
    ) -> list[BocDocument]:
        if not self._logged_in:
            self.login(driver)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                driver.get(SEARCH_URL)
                self._sleep(short=True)
                self._submit_condominium_search(driver, condominium_name, boc_unit)
                self._sleep()
                html = driver.page_source
                documents = parse_results_html(html, boc_tmk)
                if not documents:
                    selenium_docs = self._parse_results_selenium(driver, boc_tmk)
                    if selenium_docs:
                        return selenium_docs
                    if self._page_indicates_no_results(html):
                        return []
                    self._save_debug(
                        f"empty_results_{condominium_name.replace(' ', '_')}_{boc_unit}.html",
                        html,
                    )
                return documents
            except Exception as exc:  # noqa: BLE001 - retry wrapper
                last_error = exc
                logger.warning(
                    "BOC search attempt %d failed for %s %s: %s",
                    attempt,
                    condominium_name,
                    boc_unit,
                    exc,
                )
                self._sleep(short=True)
        raise RuntimeError(
            f"BOC search failed for {condominium_name} unit {boc_unit}"
        ) from last_error

    def search_tmk(self, driver, boc_tmk: str) -> list[BocDocument]:
        """Deprecated: BOC condo portfolio searches use search_condominium()."""
        raise NotImplementedError("Use search_condominium() for Maui condo units")

    def scrape_tmks(self, boc_tmks: list[str]) -> dict[str, list[BocDocument]]:
        raise NotImplementedError("Use search_condominium() per unit")

    def _submit_condominium_search(
        self,
        driver,
        condominium_name: str,
        boc_unit: str,
    ) -> None:
        self._activate_condominium_search_mode(driver)
        self._fill_condominium_form(driver, condominium_name, boc_unit)
        self._wait_for_ajax_idle(driver)
        self._click_search_button(driver)

    def _fill_condominium_form(self, driver, condominium_name: str, boc_unit: str) -> None:
        self._fill_condominium_name(driver, condominium_name)
        self._fill_condominium_units(driver, boc_unit)

    def _fill_condominium_name(self, driver, condominium_name: str) -> None:
        field_id = self._resolve_condominium_name_field_id(driver)
        self._fill_input_by_id(driver, field_id, condominium_name)

    def _fill_condominium_units(self, driver, boc_unit: str) -> None:
        field_id = self._resolve_condominium_units_field_id(driver)
        self._fill_input_by_id(driver, field_id, boc_unit)

    def _fill_input_by_id(self, driver, field_id: str, value: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, field_id))
        )
        # Set value via JS to avoid tbPlatName onchange partial postbacks while typing.
        driver.execute_script(
            "const field = document.getElementById(arguments[0]);"
            "field.focus();"
            "field.value = arguments[1];",
            field_id,
            value,
        )

    def _activate_condominium_search_mode(self, driver) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        if self._is_condo_panel_active(driver):
            return

        radio = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.ID, CONDO_RADIO_ID))
        )
        try:
            radio.click()
        except Exception:  # noqa: BLE001 - fall back to ASP.NET postback
            driver.execute_script(
                "__doPostBack(arguments[0], '');",
                CONDO_POSTBACK_TARGET,
            )

        self._wait_for_condo_panel(driver)
        self._wait_for_ajax_idle(driver)
        self._sleep(short=True)

    def _wait_for_condo_panel(self, driver) -> None:
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(driver, 30).until(lambda current: self._is_condo_panel_active(current))
        WebDriverWait(driver, 30).until(
            lambda current: current.execute_script("return document.readyState") == "complete"
        )

    @staticmethod
    def _legal_plat_label_text(driver) -> str:
        from selenium.webdriver.common.by import By

        try:
            return driver.find_element(By.ID, LEGAL_PLAT_LABEL_ID).text.strip()
        except Exception:  # noqa: BLE001 - label may be absent briefly during postback
            return ""

    @staticmethod
    def _is_element_displayed(driver, element_id: str) -> bool:
        from selenium.webdriver.common.by import By

        try:
            return driver.find_element(By.ID, element_id).is_displayed()
        except Exception:  # noqa: BLE001 - element may not exist yet
            return False

    def _is_condo_panel_active(self, driver) -> bool:
        from selenium.webdriver.common.by import By

        try:
            radio = driver.find_element(By.ID, CONDO_RADIO_ID)
        except Exception:  # noqa: BLE001 - search form not ready
            return False
        if not radio.is_selected():
            return False

        plat_label = self._legal_plat_label_text(driver).lower()
        if "condo" in plat_label:
            return True
        return not self._is_element_displayed(driver, PLATTED_CLEAR_ID)

    def _wait_for_ajax_idle(self, driver) -> None:
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(driver, 20).until(
            lambda current: current.execute_script(
                "return (typeof jQuery === 'undefined') || jQuery.active === 0;"
            )
        )

    def _resolve_condominium_name_field_id(self, driver) -> str:
        from selenium.webdriver.common.by import By

        self._assert_condo_mode_selected(driver)
        for field_id in CONDO_NAME_FIELD_IDS:
            try:
                field = driver.find_element(By.ID, field_id)
            except Exception:  # noqa: BLE001 - try next id
                continue
            if field.is_displayed() and field.is_enabled():
                return field_id
        raise RuntimeError("could not locate BOC Condominium Name field")

    def _resolve_condominium_units_field_id(self, driver) -> str:
        from selenium.webdriver.common.by import By

        self._assert_condo_mode_selected(driver)
        for field_id in CONDO_UNITS_FIELD_IDS:
            try:
                field = driver.find_element(By.ID, field_id)
            except Exception:  # noqa: BLE001 - try next id
                continue
            if field.is_displayed() and field.is_enabled():
                return field_id

        for field in driver.find_elements(By.CSS_SELECTOR, "input[type='text'], textarea"):
            field_id = field.get_attribute("id") or ""
            lowered = field_id.lower()
            if "unit" not in lowered or not field.is_displayed() or not field.is_enabled():
                continue
            if any(token in lowered for token in ("grantor", "grantee", "date", "plat", "block")):
                continue
            if "lot" in lowered and not driver.find_element(By.ID, CONDO_RADIO_ID).is_selected():
                continue
            return field_id
        raise RuntimeError("could not locate BOC Unit(s) field")

    @staticmethod
    def _assert_condo_mode_selected(driver) -> None:
        from selenium.webdriver.common.by import By

        if not driver.find_element(By.ID, CONDO_RADIO_ID).is_selected():
            raise RuntimeError("BOC Condominium search mode is not selected")
        plat_label = driver.find_element(By.ID, LEGAL_PLAT_LABEL_ID).text.lower()
        if "condo" not in plat_label and driver.find_element(By.ID, PLATTED_CLEAR_ID).is_displayed():
            raise RuntimeError("BOC Condominium search panel is not active")

    def _click_search_button(self, driver) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.ID, SEARCH_BUTTON_ID))
        )
        driver.execute_script(
            "document.getElementById(arguments[0]).click();",
            SEARCH_BUTTON_ID,
        )
        WebDriverWait(driver, 30).until(
            lambda current: current.execute_script("return document.readyState") == "complete"
        )
        self._wait_for_ajax_idle(driver)

    def _parse_results_selenium(self, driver, boc_tmk: str) -> list[BocDocument]:
        from selenium.webdriver.common.by import By

        tables = driver.find_elements(By.TAG_NAME, "table")
        best: list[BocDocument] = []
        for table in tables:
            rows = table.find_elements(By.TAG_NAME, "tr")
            if len(rows) < 2:
                continue
            header = [cell.text.strip() for cell in rows[0].find_elements(By.XPATH, ".//th|.//td")]
            body: list[list[str]] = []
            for row in rows[1:]:
                values = [cell.text.strip() for cell in row.find_elements(By.XPATH, ".//td|.//th")]
                if any(values):
                    body.append(values)
            documents = []
            for values in body:
                document = _row_to_document(boc_tmk, header, values)
                if document is not None:
                    documents.append(document)
            if len(documents) > len(best):
                best = documents
        return best

    @staticmethod
    def _page_indicates_no_results(html: str) -> bool:
        lowered = html.lower()
        phrases = (
            "no records found",
            "no documents found",
            "no results found",
            "0 records found",
            "0 documents found",
        )
        return any(phrase in lowered for phrase in phrases)

    def _create_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1400,1000")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-features=PasswordManagerOnboarding")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(60)
        return driver

    def _save_debug(self, filename: str, html: str) -> None:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / filename
        path.write_text(html, encoding="utf-8")
        logger.warning("Saved BOC debug HTML to %s", path)

    def _sleep(self, *, short: bool = False) -> None:
        if short:
            time.sleep(random.uniform(0.8, 1.5))
            return
        time.sleep(random.uniform(*self.request_delay))


def document_to_dict(document: BocDocument) -> dict[str, str]:
    return {
        "boc_tmk": document.boc_tmk,
        "document_number": document.document_number,
        "instrument_code": document.instrument_code,
        "recording_date": document.recording_date,
        "grantor": document.grantor,
        "grantee": document.grantee,
        "description": document.description,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }
