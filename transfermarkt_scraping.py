from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
import threading
import tempfile
from typing import Iterable, Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.transfermarkt.com.br"
API_BASE_URL = "https://tmapi-alpha.transfermarkt.technology"
API_CONTEXT = "br"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
COMPETITION_SLUGS = {
    "BRA1": "campeonato-brasileiro-serie-a",
    "BRA2": "campeonato-brasileiro-serie-b",
}
logger = logging.getLogger(__name__)


def normalize_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text).strip())


def slugify(text: object) -> str:
    s = unicodedata.normalize("NFKD", normalize_text(text)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s


def parse_date(value: object) -> Optional[pd.Timestamp]:
    text = normalize_text(value)
    if not text or text in {"-", "--"}:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return pd.Timestamp(datetime.strptime(text, fmt).date())
        except ValueError:
            pass
    return None


def parse_money_eur(text: object) -> Optional[float]:
    s = normalize_text(text).replace("€", "").replace("\xa0", " ").lower()
    if not s or s in {"-", "--"}:
        return None
    mult = 1.0
    if re.search(r"\b(?:bi\.?|bn|bilh(?:ao|ão|oes|ões)?)\b", s):
        mult = 1_000_000_000
    elif re.search(r"\b(?:mi\.?|mio\.?|mln|m)\b", s):
        mult = 1_000_000
    elif re.search(r"\b(?:mil|k)\b", s):
        mult = 1_000
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    number_text = m.group(1)
    if "," in number_text and "." in number_text:
        if number_text.rfind(",") > number_text.rfind("."):
            number_text = number_text.replace(".", "").replace(",", ".")
        else:
            number_text = number_text.replace(",", "")
    elif "," in number_text:
        number_text = number_text.replace(".", "").replace(",", ".")
    elif "." in number_text:
        if re.search(r"\.\d{3}(?:\D|$)", number_text):
            number_text = number_text.replace(".", "")
    return float(number_text) * mult


def safe_int(text: object) -> Optional[int]:
    m = re.search(r"-?\d+", normalize_text(text))
    return int(m.group()) if m else None


def parse_compact_int(text: object) -> Optional[int]:
    s = normalize_text(text)
    if not s or s in {"-", "--", "nan"}:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def parse_minutes(text: object) -> Optional[int]:
    return parse_compact_int(str(text).replace("'", ""))


def parse_iso_datetime_date(value: object) -> Optional[pd.Timestamp]:
    text = normalize_text(value)
    if not text or text in {"-", "--", "nan"}:
        return None
    try:
        return pd.Timestamp(text).normalize()
    except (TypeError, ValueError):
        return None


def chunked(seq: Iterable[object], size: int) -> Iterable[list[object]]:
    batch: list[object] = []
    for item in seq:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def map_position_group(position: Optional[str]) -> Optional[str]:
    if not position:
        return None
    s = slugify(position)
    if any(token in s for token in ["goleiro", "goalkeeper", "keeper"]):
        return "Goleiro"
    if any(token in s for token in ["zagueiro", "defender", "defesa", "lateral", "back", "wing_back", "center_back"]):
        return "Defesa"
    if any(token in s for token in ["meio", "midfield", "volante", "meia", "medio", "winger"]):
        return "Meio-campo"
    if any(token in s for token in ["atac", "forward", "striker", "wing", "ponta", "centre_forward", "center_forward"]):
        return "Ataque"
    return normalize_text(position)


def season_label_from_tm_id(tm_season_id: int) -> str:
    return f"{tm_season_id % 100:02d}/{(tm_season_id + 1) % 100:02d}"


def display_year_to_tm_season_id(display_year: int) -> int:
    return display_year - 1


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [
            normalize_text(" ".join(str(x) for x in tup if str(x) != "nan"))
            for tup in out.columns
        ]
    else:
        out.columns = [normalize_text(c) for c in out.columns]
    return out


def html_tables(html: str) -> list[pd.DataFrame]:
    try:
        return [
            flatten_columns(df)
            for df in pd.read_html(StringIO(html), displayed_only=False, flavor="lxml")
        ]
    except (ValueError, ImportError) as exc:
        logger.debug("Failed to parse HTML tables: %s", exc)
        return []


@dataclass
class TMClient:
    cache_dir: Path
    min_sleep: float = 1.0
    max_sleep: float = 2.0
    timeout: int = 30

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._entity_lock = threading.Lock()
        self._entity_cache: dict[str, dict[str, dict]] = {}

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(HEADERS)
            try:
                session.get(BASE_URL, timeout=self.timeout)
            except requests.RequestException as exc:
                logger.debug("Session warm-up failed for %s: %s", threading.current_thread().name, exc)
            self._local.session = session
        return session

    def _cache_path(self, key: str, suffix: str = ".html") -> Path:
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()}{suffix}"

    def fetch(self, url: str, use_cache: bool = True) -> str:
        cache_path = self._cache_path(url, ".html")
        if use_cache and cache_path.exists():
            logger.debug("Cache hit: %s", url)
            return cache_path.read_text(encoding="utf-8")

        for attempt in range(4):
            try:
                logger.debug("Fetching %s (attempt %d/%d)", url, attempt + 1, 4)
                resp = self._session().get(url, timeout=self.timeout)
                resp.raise_for_status()
                resp.encoding = resp.encoding or "utf-8"
                html = resp.text
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    delete=False,
                    dir=str(self.cache_dir),
                    suffix=".tmp",
                ) as tmp:
                    tmp.write(html)
                    tmp_path = Path(tmp.name)
                tmp_path.replace(cache_path)
                logger.debug("Fetched %s (%d chars)", url, len(html))
                time.sleep(random.uniform(self.min_sleep, self.max_sleep))
                return html
            except requests.RequestException as exc:
                if attempt == 3:
                    logger.exception("Failed to fetch %s after %d attempts", url, attempt + 1)
                    raise
                logger.warning(
                    "Fetch failed for %s on attempt %d/%d: %s",
                    url,
                    attempt + 1,
                    4,
                    exc,
                )
                time.sleep((attempt + 1) * 2)

        raise RuntimeError(f"Falha ao baixar {url}")

    def fetch_json(self, url: str, params: Optional[Iterable[tuple[str, object]]] = None, use_cache: bool = True) -> dict:
        request = requests.Request("GET", url, params=list(params or []))
        prepared = self._session().prepare_request(request)
        cache_path = self._cache_path(prepared.url, ".json")
        if use_cache and cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))

        for attempt in range(4):
            try:
                logger.debug("Fetching JSON %s (attempt %d/%d)", prepared.url, attempt + 1, 4)
                resp = self._session().get(
                    url,
                    params=list(params or []),
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    delete=False,
                    dir=str(self.cache_dir),
                    suffix=".tmp",
                ) as tmp:
                    json.dump(payload, tmp, ensure_ascii=False)
                    tmp_path = Path(tmp.name)
                tmp_path.replace(cache_path)
                time.sleep(random.uniform(self.min_sleep, self.max_sleep))
                return payload
            except (requests.RequestException, ValueError) as exc:
                if attempt == 3:
                    logger.exception("Failed to fetch JSON %s after %d attempts", prepared.url, attempt + 1)
                    raise
                logger.warning(
                    "JSON fetch failed for %s on attempt %d/%d: %s",
                    prepared.url,
                    attempt + 1,
                    4,
                    exc,
                )
                time.sleep((attempt + 1) * 2)

        raise RuntimeError(f"Falha ao baixar JSON {prepared.url}")

    def fetch_entities(self, endpoint: str, ids: Iterable[object], batch_size: int = 100) -> dict[str, dict]:
        requested_ids = [str(x) for x in ids if normalize_text(x) and str(x) != "0"]
        if not requested_ids:
            return {}

        unique_ids = list(dict.fromkeys(requested_ids))
        with self._entity_lock:
            cache = self._entity_cache.setdefault(endpoint, {})
            pending = [entity_id for entity_id in unique_ids if entity_id not in cache]

        for group in chunked(pending, batch_size):
            params: list[tuple[str, object]] = [("ids[]", entity_id) for entity_id in group]
            params.append(("_x_preferred_context", API_CONTEXT))
            payload = self.fetch_json(f"{API_BASE_URL}/{endpoint}", params=params)
            data = payload.get("data", []) if isinstance(payload, dict) else []
            loaded = {str(item.get("id")): item for item in data if isinstance(item, dict) and item.get("id") is not None}
            with self._entity_lock:
                self._entity_cache.setdefault(endpoint, {}).update(loaded)

        with self._entity_lock:
            return {entity_id: self._entity_cache.get(endpoint, {}).get(entity_id) for entity_id in unique_ids}


def competition_url(code: str, tm_season_id: int) -> str:
    return f"{BASE_URL}/{COMPETITION_SLUGS[code]}/startseite/wettbewerb/{code}/saison_id/{tm_season_id}"


def squad_url_from_team_startseite(team_url: str) -> str:
    m = re.search(r"/([^/]+)/startseite/verein/(\d+)/saison_id/(\d+)", team_url)
    if not m:
        raise ValueError(f"URL de time inesperada: {team_url}")
    slug, team_id, season_id = m.groups()
    return f"{BASE_URL}/{slug}/kader/verein/{team_id}/saison_id/{season_id}/plus/1"


def parse_player_profile_url(url: str) -> tuple[str, int]:
    m = re.search(r"/([^/]+)/profil/spieler/(\d+)", url)
    if not m:
        raise ValueError(f"URL de jogador inesperada: {url}")
    return m.group(1), int(m.group(2))


def to_endpoint(profile_url: str, endpoint: str) -> str:
    slug, player_id = parse_player_profile_url(profile_url)
    return f"{BASE_URL}/{slug}/{endpoint}/spieler/{player_id}"


def extract_field_block(text: str, label: str, labels: Iterable[str]) -> Optional[str]:
    all_labels = [re.escape(x) + ":" for x in labels if x != label]
    pattern = rf"{re.escape(label)}:\s*(.*?)(?=\s+(?:{'|'.join(all_labels)})|$)"
    m = re.search(pattern, text, flags=re.I | re.S)
    return normalize_text(m.group(1)) if m else None


def get_team_urls(client: TMClient, competition_code: str, tm_season_id: int) -> list[str]:
    html = client.fetch(competition_url(competition_code, tm_season_id))
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.select("table a[href*='/startseite/verein/']"):
        href = a.get("href", "")
        if f"/saison_id/{tm_season_id}" in href:
            out[urljoin(BASE_URL, href)] = normalize_text(a.get_text(" ", strip=True))
    return list(out.keys())


def get_player_urls_from_squad(client: TMClient, squad_url: str) -> list[str]:
    html = client.fetch(squad_url)
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.select("table a[href*='/profil/spieler/']"):
        href = urljoin(BASE_URL, a.get("href", ""))
        try:
            _, player_id = parse_player_profile_url(href)
        except ValueError:
            continue
        out[player_id] = href
    return list(out.values())


def parse_info_table(soup: BeautifulSoup) -> dict[str, str]:
    info = soup.select_one("div.info-table")
    if info is None:
        return {}

    pairs: dict[str, str] = {}
    current_label: Optional[str] = None
    for span in info.select("span.info-table__content"):
        classes = set(span.get("class", []))
        text = normalize_text(span.get_text(" ", strip=True))
        if not text:
            continue
        if "info-table__content--regular" in classes:
            current_label = text.rstrip(":")
            continue
        if "info-table__content--bold" in classes and current_label:
            pairs[current_label] = text
            current_label = None
    return pairs


def extract_position_detail(soup: BeautifulSoup, fallback: Optional[str] = None) -> Optional[str]:
    text = normalize_text(soup.get_text(" ", strip=True))
    patterns = [
        r"Posição principal:\s*(.+?)(?=\s+Posições secundárias:|\s+Informações e fatos|\s+Nome no país de origem:|\s+Dados adicionais|\s*$)",
        r"Posição detalhada\s+Posição principal:\s*(.+?)(?=\s+Posições secundárias:|\s+Informações e fatos|\s+Nome no país de origem:|\s+Dados adicionais|\s*$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return normalize_text(m.group(1))
    return fallback


def scrape_profile(client: TMClient, profile_url: str) -> dict:
    html = client.fetch(profile_url)
    soup = BeautifulSoup(html, "html.parser")
    slug, player_id = parse_player_profile_url(profile_url)
    header = soup.select_one("header.data-header")
    info = parse_info_table(soup)

    headline = header.select_one("h1 strong") if header else soup.find("h1")
    player_name = normalize_text(headline.get_text(" ", strip=True)) if headline else None

    birth_block = info.get("Nasc./Idade")
    birth_date, age_current = None, None
    if birth_block:
        m = re.search(r"(\d{2}/\d{2}/\d{4})\s*\((\d+)\)", birth_block)
        if m:
            birth_date = parse_date(m.group(1))
            age_current = int(m.group(2))

    position_main = info.get("Posição")
    position_detail = extract_position_detail(soup, fallback=position_main)
    position_group = map_position_group(position_main or position_detail)

    mv_text = ""
    if header:
        mv_node = header.select_one(".data-header__market-value-wrapper")
        mv_text = normalize_text(mv_node.get_text(" ", strip=True)) if mv_node else ""
    mv_match = re.search(
        r"(€\s*\d+(?:[.,]\d+)?\s*(?:mil|mi\.?|bi\.?)?)\s*Última alteração:\s*(\d{2}/\d{2}/\d{4})",
        mv_text,
        flags=re.I,
    )

    current_club = None
    if header and header.select_one(".data-header__club"):
        current_club = normalize_text(header.select_one(".data-header__club").get_text(" ", strip=True))
    current_club = info.get("Clube atual") or current_club

    return {
        "player_id": player_id,
        "player_slug": slug,
        "player_name": player_name,
        "profile_url": profile_url,
        "full_name": info.get("Nome completo"),
        "birth_date": birth_date,
        "age_current": age_current,
        "birth_place": info.get("Local de nascimento"),
        "nationality": info.get("Nacionalidade"),
        "height_text": info.get("Altura"),
        "position_group": position_group,
        "position_detail": position_detail,
        "preferred_foot": info.get("Pé"),
        "agent": info.get("Empresários"),
        "current_club": current_club,
        "club_since": parse_date(info.get("No time desde")),
        "contract_until": parse_date(info.get("Contrato até")),
        "last_contract_extension": parse_date(info.get("Última renovação de contrato")),
        "current_market_value_eur": parse_money_eur(mv_match.group(1)) if mv_match else None,
        "current_market_value_date": parse_date(mv_match.group(2)) if mv_match else None,
    }


def extract_market_value_history_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    scripts = "\n".join(script.get_text("\n", strip=False) for script in soup.find_all("script"))
    rows = []

    for m in re.finditer(
        r"Date\.UTC\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)\s*,\s*([0-9]+(?:\.[0-9]+)?)",
        scripts,
    ):
        year, month_zero, day, value = m.groups()
        rows.append(
            {
                "valuation_date": pd.Timestamp(year=int(year), month=int(month_zero) + 1, day=int(day)),
                "market_value_eur": float(value),
                "club_at_valuation": None,
                "source_pattern": "date_utc",
            }
        )

    for m in re.finditer(
        r"datum_mw['\"]?\s*[:=]\s*['\"](?P<date>\d{2}/\d{2}/\d{4})['\"](?P<chunk>.{0,300}?)"
        r"(?:y|mw|marketValue)['\"]?\s*[:=]\s*['\"]?(?P<value>\d+(?:\.\d+)?)",
        scripts,
        flags=re.S,
    ):
        club_match = re.search(r"verein['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", m.group("chunk"))
        rows.append(
            {
                "valuation_date": parse_date(m.group("date")),
                "market_value_eur": float(m.group("value")),
                "club_at_valuation": normalize_text(club_match.group(1)) if club_match else None,
                "source_pattern": "datum_mw",
            }
        )

    for m in re.finditer(
        r"['\"]date['\"]\s*:\s*['\"](?P<date>\d{4}-\d{2}-\d{2})['\"](?P<chunk>.{0,250}?)"
        r"['\"](?:marketValue|mw|y)['\"]\s*:\s*['\"]?(?P<value>\d+(?:\.\d+)?)",
        scripts,
        flags=re.S,
    ):
        club_match = re.search(r"['\"](?:club|verein)['\"]\s*:\s*['\"]([^'\"]+)['\"]", m.group("chunk"))
        rows.append(
            {
                "valuation_date": parse_date(m.group("date")),
                "market_value_eur": float(m.group("value")),
                "club_at_valuation": normalize_text(club_match.group(1)) if club_match else None,
                "source_pattern": "json_date",
            }
        )

    dedup = {}
    for row in rows:
        if row["valuation_date"] is not None:
            dedup[pd.Timestamp(row["valuation_date"]).normalize()] = row
    return [dedup[k] for k in sorted(dedup)]


def scrape_market_values(client: TMClient, profile_url: str, debug_dir: Path) -> pd.DataFrame:
    slug, player_id = parse_player_profile_url(profile_url)
    try:
        payload = client.fetch_json(
            f"{API_BASE_URL}/player/{player_id}/market-value-history",
            params=[("_x_preferred_context", API_CONTEXT)],
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        history = data.get("history", []) if isinstance(data, dict) else []
        club_map = client.fetch_entities("clubs", data.get("clubIds", []))
        rows = []
        for entry in history:
            mv = entry.get("marketValue", {})
            club = club_map.get(str(entry.get("clubId"))) or {}
            rows.append(
                {
                    "player_id": player_id,
                    "player_slug": slug,
                    "valuation_date": parse_date(mv.get("determined")),
                    "market_value_eur": mv.get("value"),
                    "club_at_valuation": club.get("name") or club.get("baseDetails", {}).get("shortName"),
                    "source_pattern": "tmapi_market_value_history",
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["player_id", "player_slug", "valuation_date", "market_value_eur", "club_at_valuation", "source_pattern"])
        return df.dropna(subset=["valuation_date"]).sort_values("valuation_date").drop_duplicates(subset=["valuation_date"])
    except Exception:
        html = client.fetch(to_endpoint(profile_url, "marktwertverlauf"))
        rows = extract_market_value_history_from_html(html)
        if not rows:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{player_id}_marktwertverlauf.html").write_text(html, encoding="utf-8")
            return pd.DataFrame(columns=["player_id", "player_slug", "valuation_date", "market_value_eur", "club_at_valuation", "source_pattern"])
        df = pd.DataFrame(rows)
        df.insert(0, "player_id", player_id)
        df.insert(1, "player_slug", slug)
        return df


def find_max_page(html: str) -> int:
    pages = {int(x) for x in re.findall(r"/page/(\d+)", html)}
    return max(pages) if pages else 1


def candidate_table(html: str, must_have: Iterable[str]) -> Optional[pd.DataFrame]:
    wanted = [slugify(x) for x in must_have]
    for df in html_tables(html):
        cols = [slugify(c) for c in df.columns]
        if all(any(w in col for col in cols) for w in wanted):
            out = df.copy()
            out.columns = cols
            return out
    return None


def scrape_injuries(client: TMClient, profile_url: str) -> pd.DataFrame:
    slug, player_id = parse_player_profile_url(profile_url)
    base = to_endpoint(profile_url, "verletzungen") + "/plus/1"
    first_html = client.fetch(base)
    max_page = find_max_page(first_html)
    logger.debug("Player %s injuries: %d page(s)", player_id, max_page)
    chunks = []

    for page in range(1, max_page + 1):
        html = first_html if page == 1 else client.fetch(f"{base}/page/{page}")
        df = candidate_table(html, must_have=["Temporada", "Lesão"])
        if df is None or df.empty:
            continue
        df = df.rename(
            columns={
                "temporada": "season_label",
                "lesao": "injury",
                "de": "from_date",
                "ate": "to_date",
                "dias": "days_out",
                "jogos_perdidos": "games_missed",
            }
        )
        for col in ("from_date", "to_date"):
            if col in df.columns:
                df[col] = df[col].apply(parse_date)
        for col in ("days_out", "games_missed"):
            if col in df.columns:
                df[col] = df[col].apply(safe_int)
        df["player_id"] = player_id
        df["player_slug"] = slug
        chunks.append(df)

    return pd.concat(chunks, ignore_index=True).drop_duplicates() if chunks else pd.DataFrame(
        columns=["player_id", "player_slug", "season_label", "injury", "from_date", "to_date", "days_out", "games_missed"]
    )


def scrape_transfers(client: TMClient, profile_url: str) -> pd.DataFrame:
    slug, player_id = parse_player_profile_url(profile_url)
    try:
        payload = client.fetch_json(
            f"{API_BASE_URL}/transfer/history/player/{player_id}",
            params=[("_x_preferred_context", API_CONTEXT)],
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        history = data.get("history", {}) if isinstance(data, dict) else {}
        transfers = list(history.get("terminated", [])) + list(history.get("pending", []))
        club_map = client.fetch_entities("clubs", data.get("clubIds", []))
        rows = []
        for entry in transfers:
            details = entry.get("details", {})
            source = entry.get("transferSource", {})
            dest = entry.get("transferDestination", {})
            source_club = club_map.get(str(source.get("clubId"))) or {}
            dest_club = club_map.get(str(dest.get("clubId"))) or {}
            fee = details.get("fee", {})
            market_value = details.get("marketValue", {})
            season = details.get("season", {})
            rows.append(
                {
                    "player_id": player_id,
                    "player_slug": slug,
                    "transfer_id": entry.get("id"),
                    "transfer_date": parse_iso_datetime_date(details.get("date")),
                    "season_label": season.get("display"),
                    "season_id": season.get("id"),
                    "from_club_id": source.get("clubId"),
                    "from_club_name": source_club.get("name") or source_club.get("baseDetails", {}).get("shortName"),
                    "from_competition_id": source.get("competitionId"),
                    "from_country_id": source.get("countryId"),
                    "to_club_id": dest.get("clubId"),
                    "to_club_name": dest_club.get("name") or dest_club.get("baseDetails", {}).get("shortName"),
                    "to_competition_id": dest.get("competitionId"),
                    "to_country_id": dest.get("countryId"),
                    "age_at_transfer": details.get("age"),
                    "market_value_eur": market_value.get("value"),
                    "transfer_fee_eur": fee.get("value"),
                    "transfer_fee_text": fee.get("compact", {}).get("content"),
                    "contract_until": parse_iso_datetime_date(details.get("contractUntilDate")),
                    "is_pending": details.get("isPending"),
                    "transfer_type": entry.get("typeDetails", {}).get("type"),
                    "transfer_type_name": entry.get("typeDetails", {}).get("name"),
                    "transfer_fee_description": entry.get("typeDetails", {}).get("feeDescription"),
                    "relative_url": entry.get("relativeUrl"),
                }
            )
        if not rows:
            return pd.DataFrame(columns=["player_id", "player_slug"])
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame(columns=["player_id", "player_slug"])


def extract_tm_season_ids_from_performance_html(html: str) -> list[int]:
    season_ids = {int(x) for x in re.findall(r"/saison/(\d{4})(?:/|\"|')", html)}
    soup = BeautifulSoup(html, "html.parser")
    selector_text = ""
    for table in soup.find_all("table"):
        table_text = normalize_text(table.get_text(" ", strip=True))
        if "Selecionar temporada" in table_text and "Balanço total" in table_text:
            selector_text = table_text
            break

    labels = re.findall(r"(?<!\d)(\d{2})/(\d{2})(?!\d)", selector_text)
    out = set()
    for a, _ in labels:
        yy = int(a)
        out.add(1900 + yy if yy >= 90 else 2000 + yy)
    return sorted(season_ids | out, reverse=True)


def scrape_performance_summaries(client: TMClient, profile_url: str) -> pd.DataFrame:
    slug, player_id = parse_player_profile_url(profile_url)
    menu_html = client.fetch(to_endpoint(profile_url, "leistungsdaten") + "/saison//plus/1")
    season_ids = extract_tm_season_ids_from_performance_html(menu_html)
    logger.debug("Player %s performance seasons found: %d", player_id, len(season_ids))
    rows = []

    for season_id in season_ids:
        html = client.fetch(to_endpoint(profile_url, "leistungsdaten") + f"/saison/{season_id}")
        total_row = None
        for df in html_tables(html):
            cols = [normalize_text(c) for c in df.columns]
            if "Compet" in " ".join(cols) or "wettbewerb" in slugify(" ".join(cols)):
                mask = df.astype(str).apply(lambda s: s.str.contains("Total", case=False, na=False)).any(axis=1)
                if mask.any():
                    total_row = df.loc[mask].iloc[0]
                    break

        if total_row is not None:
            values = [normalize_text(v) for v in total_row.tolist()]
            row_text = " | ".join(values)
            row = {
                "player_id": player_id,
                "player_slug": slug,
                "tm_season_id": season_id,
                "season_label": season_label_from_tm_id(season_id),
                "summary_row_text": row_text,
            }
            for idx, value in enumerate(values[1:], start=1):
                row[f"metric_{idx}"] = value
        else:
            text = normalize_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
            m = re.search(r"Total\s*:\s*(.+?)(?=Links Rápidos|Participe|$)", text, flags=re.I)
            row_text = m.group(1) if m else None
            row = {
                "player_id": player_id,
                "player_slug": slug,
                "tm_season_id": season_id,
                "season_label": season_label_from_tm_id(season_id),
                "summary_row_text": row_text,
            }

        values = [normalize_text(v) for v in (total_row.tolist() if total_row is not None else [])]
        numeric_candidates = [parse_compact_int(v) for v in values[2:-1]]
        numeric_candidates = [v for v in numeric_candidates if v is not None]
        row["minutes_est"] = parse_minutes(values[-1]) if values else parse_minutes(row.get("summary_row_text"))
        row["appearances_est"] = numeric_candidates[0] if numeric_candidates else None
        rows.append(row)

    return pd.DataFrame(rows)


def build_core_dataset(profiles: pd.DataFrame, market_values: pd.DataFrame) -> pd.DataFrame:
    if profiles.empty or market_values.empty:
        return pd.DataFrame(
            columns=[
                "player_id",
                "player_slug",
                "valuation_date",
                "market_value_eur",
                "club_at_valuation",
                "source_pattern",
                "player_name",
                "full_name",
                "birth_date",
                "nationality",
                "position_group",
                "position_detail",
                "age_years",
            ]
        )
    out = market_values.merge(
        profiles[["player_id", "player_name", "full_name", "birth_date", "nationality", "position_group", "position_detail"]],
        on="player_id",
        how="left",
    )
    out["valuation_date"] = pd.to_datetime(out["valuation_date"])
    out["birth_date"] = pd.to_datetime(out["birth_date"])
    out["age_years"] = (out["valuation_date"] - out["birth_date"]).dt.days / 365.25
    return out.sort_values(["player_id", "valuation_date"]).reset_index(drop=True)


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def log_progress(done: int, total: int, started_at: float) -> None:
    if total <= 0:
        return
    elapsed = time.perf_counter() - started_at
    remaining = max(total - done, 0)
    pct = (done / total) * 100
    eta = (elapsed / done) * remaining if done else 0.0
    logger.info(
        "Progresso: %d/%d (%.1f%%) - faltam %d - decorrido %s - ETA %s",
        done,
        total,
        pct,
        remaining,
        format_duration(elapsed),
        format_duration(eta),
    )


def scrape_player_bundle(client: TMClient, profile_url: str, debug_dir: Path) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    player_id = parse_player_profile_url(profile_url)[1]
    logger.info("Player %s: starting bundle", player_id)
    try:
        logger.debug("Player %s: profile stage start", player_id)
        profile = scrape_profile(client, profile_url)
        logger.debug("Player %s: profile stage done", player_id)
        logger.debug("Player %s: market values stage start", player_id)
        market_values = scrape_market_values(client, profile_url, debug_dir)
        logger.debug("Player %s: market values stage done", player_id)
        logger.debug("Player %s: injuries stage start", player_id)
        injuries = scrape_injuries(client, profile_url)
        logger.debug("Player %s: injuries stage done", player_id)
        logger.debug("Player %s: transfers stage start", player_id)
        transfers = scrape_transfers(client, profile_url)
        logger.debug("Player %s: transfers stage done", player_id)
        logger.debug("Player %s: performance stage start", player_id)
        performance = scrape_performance_summaries(client, profile_url)
        logger.debug("Player %s: performance stage done", player_id)
        logger.info("Player %s: bundle done", player_id)
        return profile, market_values, injuries, transfers, performance
    except Exception:
        logger.exception("Player %s: bundle failed", player_id)
        raise


def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )
    out_dir = Path(args.out)
    cache_dir = out_dir / "cache"
    debug_dir = out_dir / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    client = TMClient(cache_dir=cache_dir, min_sleep=args.min_sleep, max_sleep=args.max_sleep)
    logger.info(
        "Starting scrape: competitions=%s display_years=%s-%s max_players=%s out=%s",
        ",".join(args.competitions),
        args.display_start,
        args.display_end,
        args.max_players if args.max_players is not None else "none",
        out_dir.resolve(),
    )

    squad_rows = []
    player_urls = {}

    for competition_code in args.competitions:
        logger.info("Competition %s started", competition_code)
        for display_year in range(args.display_start, args.display_end + 1):
            tm_season_id = display_year_to_tm_season_id(display_year)
            logger.info("Season %s (%s) started", display_year, tm_season_id)
            team_urls = get_team_urls(client, competition_code, tm_season_id)
            logger.info(
                "Season %s (%s): %d team(s) found",
                display_year,
                tm_season_id,
                len(team_urls),
            )
            for team_url in team_urls:
                squad_url = squad_url_from_team_startseite(team_url)
                players = get_player_urls_from_squad(client, squad_url)

                club_match = re.search(r"/([^/]+)/startseite/verein/(\d+)/saison_id/(\d+)", team_url)
                club_slug, club_id, _ = club_match.groups()
                logger.info(
                    "Team %s (%s): %d player(s) found",
                    club_slug,
                    club_id,
                    len(players),
                )
                for profile_url in players:
                    player_slug, player_id = parse_player_profile_url(profile_url)
                    player_urls[player_id] = profile_url
                    squad_rows.append(
                        {
                            "competition_code": competition_code,
                            "display_year": display_year,
                            "tm_season_id": tm_season_id,
                            "season_label": season_label_from_tm_id(tm_season_id),
                            "club_id": int(club_id),
                            "club_slug": club_slug,
                            "club_name_guess": club_slug.replace("-", " "),
                            "player_id": player_id,
                            "player_slug": player_slug,
                            "profile_url": profile_url,
                            "squad_url": squad_url,
                        }
                    )
                    if args.max_players and len(player_urls) >= args.max_players:
                        logger.info("Stopping early at max_players=%d", args.max_players)
                        break
                if args.max_players and len(player_urls) >= args.max_players:
                    break
            if args.max_players and len(player_urls) >= args.max_players:
                break
        if args.max_players and len(player_urls) >= args.max_players:
            break

    squad_membership = pd.DataFrame(squad_rows).drop_duplicates()
    logger.info("Squad membership rows collected: %d", len(squad_membership))
    squad_membership.to_csv(out_dir / "squad_membership.csv", index=False)

    profiles_rows, mv_parts, inj_parts, trans_parts, perf_parts = [], [], [], [], []
    total_players = len(player_urls)
    worker_count = max(1, min(args.workers, total_players)) if total_players else 0
    logger.info("Starting player scrape for %d unique player(s) with %d worker(s)", total_players, worker_count)
    player_scrape_started_at = time.perf_counter()

    player_results: dict[int, tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    if total_players and worker_count == 1:
        for index, (player_id, profile_url) in enumerate(player_urls.items(), start=1):
            try:
                logger.info("Player %d/%d: %s", index, total_players, player_id)
                player_results[player_id] = scrape_player_bundle(client, profile_url, debug_dir)
            except Exception as exc:
                logger.warning("Falha no jogador %s (%s): %s", player_id, profile_url, exc)
            finally:
                log_progress(index, total_players, player_scrape_started_at)
    elif total_players:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="player") as executor:
            future_to_player = {
                executor.submit(scrape_player_bundle, client, profile_url, debug_dir): (player_id, profile_url)
                for player_id, profile_url in player_urls.items()
            }
            for index, future in enumerate(as_completed(future_to_player), start=1):
                player_id, profile_url = future_to_player[future]
                try:
                    player_results[player_id] = future.result()
                    logger.info("Player %d/%d completed: %s", index, total_players, player_id)
                except Exception as exc:
                    logger.warning("Falha no jogador %s (%s): %s", player_id, profile_url, exc)
                finally:
                    log_progress(index, total_players, player_scrape_started_at)

    for player_id, _ in player_urls.items():
        result = player_results.get(player_id)
        if result is None:
            continue
        profile_row, mv_df, inj_df, trans_df, perf_df = result
        profiles_rows.append(profile_row)
        mv_parts.append(mv_df)
        inj_parts.append(inj_df)
        trans_parts.append(trans_df)
        perf_parts.append(perf_df)

    profiles = pd.DataFrame(profiles_rows).drop_duplicates(subset=["player_id"])
    market_values = pd.concat(mv_parts, ignore_index=True) if mv_parts else pd.DataFrame()
    injuries = pd.concat(inj_parts, ignore_index=True) if inj_parts else pd.DataFrame()
    transfers = pd.concat(trans_parts, ignore_index=True) if trans_parts else pd.DataFrame()
    performance = pd.concat(perf_parts, ignore_index=True) if perf_parts else pd.DataFrame()

    if args.nationality_filter and not profiles.empty:
        keep_ids = set(
            profiles.loc[
                profiles["nationality"].fillna("").str.contains(args.nationality_filter, case=False, na=False),
                "player_id",
            ]
        )
        profiles = profiles[profiles["player_id"].isin(keep_ids)]
        market_values = market_values[market_values["player_id"].isin(keep_ids)]
        injuries = injuries[injuries["player_id"].isin(keep_ids)]
        transfers = transfers[transfers["player_id"].isin(keep_ids)]
        performance = performance[performance["player_id"].isin(keep_ids)]
        squad_membership = squad_membership[squad_membership["player_id"].isin(keep_ids)]

    core = build_core_dataset(profiles, market_values)

    profiles.to_csv(out_dir / "profiles.csv", index=False)
    market_values.to_csv(out_dir / "market_values.csv", index=False)
    injuries.to_csv(out_dir / "injuries.csv", index=False)
    transfers.to_csv(out_dir / "transfers.csv", index=False)
    performance.to_csv(out_dir / "performance_summaries.csv", index=False)
    core.to_csv(out_dir / "career_arcs_base.csv", index=False)

    logger.info("Players únicos: %d", profiles["player_id"].nunique() if not profiles.empty else 0)
    logger.info("Pontos de valor de mercado: %d", len(market_values))
    logger.info("Lesões: %d", len(injuries))
    logger.info("Transferências: %d", len(transfers))
    logger.info("Temporadas de desempenho: %d", len(performance))
    logger.info("Arquivos salvos em: %s", out_dir.resolve())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scraper do Transfermarkt para o projeto Arcos de carreira.")
    p.add_argument("--display-start", type=int, default=2018)
    p.add_argument("--display-end", type=int, default=2026)
    p.add_argument("--competitions", nargs="+", default=["BRA1", "BRA2"])
    p.add_argument("--nationality-filter", default=None, help='Ex.: "Brasil"')
    p.add_argument("--out", default="data_transfermarkt_arcos")
    p.add_argument("--min-sleep", type=float, default=1.0)
    p.add_argument("--max-sleep", type=float, default=2.0)
    p.add_argument("--max-players", type=int, default=None)
    p.add_argument("--workers", type=int, default=4, help="Número de workers em paralelo na etapa por jogador.")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Nível de log exibido no console.",
    )
    return p


if __name__ == "__main__":
    run(build_parser().parse_args())
