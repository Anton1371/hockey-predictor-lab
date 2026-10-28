#!/usr/bin/env python3
"""Free, conservative NHL + KHL journal updater for Hockey Predictor Lab.

- Pulls scores from the NHL public scoreboard and KHL's public mobile scoreboard.
- Settles only existing, locked picks when a source marks the game final.
- Adds new games inside the next 36 hours exactly once.
- New games get a transparent last-five-results heuristic, never an automatic
  stake. Odds are left blank unless an independent bookmaker source is added.
- Does not rewrite existing picks, odds, or stake sizes.
"""
from __future__ import annotations
import datetime as dt
import html
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / "journal.json"
UTC = dt.timezone.utc
OMSK = dt.timezone(dt.timedelta(hours=6))
NOW = dt.datetime.now(UTC)
HORIZON = NOW + dt.timedelta(hours=36)
NHL_BASE = "https://api-web.nhle.com/v1"
KHL_BASE = "https://khl.api.webcaster.pro/api/khl_mobile"
NHL_SCORE_URL = NHL_BASE + "/score/"
KHL_EVENTS_URL = KHL_BASE + "/events_v2.json"

TEAM_CODES = {
    "Вашингтон Кэпиталз": "WSH", "Филадельфия Флайерз": "PHI",
    "Эдмонтон Ойлерз": "EDM", "Калгари Флэймз": "CGY",
    "Колорадо Эвеланш": "COL", "Нью-Джерси Девилз": "NJD",
    "Сиэтл Кракен": "SEA", "Торонто Мэйпл Лифс": "TOR",
    "Каролина Харрикейнз": "CAR", "Коламбус Блю Джекетс": "CBJ",
    "Тампа-Бэй Лайтнинг": "TBL", "Виннипег Джетс": "WPG",
    "Питтсбург Пингвинз": "PIT", "Юта Маммот": "UTA",
    "Детройт Ред Уингз": "DET", "Чикаго Блэкхокс": "CHI",
    "Сент-Луис Блюз": "STL", "Бостон Брюинз": "BOS",
    "Даллас Старз": "DAL", "Монреаль Канадиенс": "MTL",
    "Нэшвилл Предаторз": "NSH", "Миннесота Уайлд": "MIN",
    "Нью-Йорк Айлендерс": "NYI", "Сан-Хосе Шаркс": "SJS",
    "Ванкувер Кэнакс": "VAN", "Анахайм Дакс": "ANA",
    "Баффало Сейбрз": "BUF", "Лос-Анджелес Кингз": "LAK",
    "Оттава Сенаторз": "OTT", "Нью-Йорк Рейнджерс": "NYR",
    "Флорида Пантерз": "FLA", "Вегас Голден Найтс": "VGK",
}
NHL_CODES = set(TEAM_CODES.values())
NHL_FINAL = {"OFF", "FINAL", "FINAL/OT", "FINAL/SO"}
NHL_LIVE = {"LIVE", "CRIT"}
KHL_FINAL = {"finished", "final", "ended", "complete", "completed"}
KHL_LIVE = {"in_progress", "live", "started", "playing"}


def fetch_json(url: str, data: bytes | None = None) -> Any:
    headers = {
        "User-Agent": "HockeyPredictorLab/1.1 (+https://github.com/Anton1371/hockey-predictor-lab)",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "ru,en;q=0.8",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=OMSK)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def normalize_text(value: Any) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).lower().replace("ё", "е")
    value = re.sub(r"\b(хк|кхл|hc|hockey club)\b", " ", value)
    return re.sub(r"[^a-zа-я0-9]+", "", value)


def khl_team_key(value: Any) -> str:
    raw = normalize_text(value)
    if "динамо" in raw and "минск" in raw:
        return "khl:dinamo-minsk"
    if "динамо" in raw and "москва" in raw:
        return "khl:dinamo-moscow"
    if "металлург" in raw:
        return "khl:metallurg-magnitogorsk"
    if "торпедо" in raw:
        return "khl:torpedo"
    if "сочи" in raw:
        return "khl:sochi"
    aliases = {
        "акбарс": "ak-bars", "авангард": "avangard", "барыс": "barys",
        "цска": "cska", "трактор": "traktor", "сибирь": "sibir",
        "автомобилист": "avtomobilist", "спартак": "spartak",
        "северсталь": "severstal", "нефтехимик": "neftekhimik",
        "адмирал": "admiral", "локомотив": "lokomotiv", "амур": "amur",
        "салаватюлаев": "salavat-yulaev", "салават": "salavat-yulaev",
        "лада": "lada", "витязь": "vityaz", "динамо": "dinamo-unknown",
        "куньлунь": "kunlun", "шангайскиедраконы": "shanghai-dragons",
        "драконы": "shanghai-dragons",
    }
    for alias, key in aliases.items():
        if raw == normalize_text(alias) or raw.startswith(normalize_text(alias)):
            return "khl:" + key
    return "khl:" + raw if raw else ""


def team_key(league: str, name: Any) -> str:
    if league == "NHL":
        text = str(name or "").strip()
        if text.upper() in NHL_CODES:
            return "nhl:" + text.upper()
        code = TEAM_CODES.get(text)
        if code:
            return "nhl:" + code
        norm = normalize_text(text)
        for known_name, known_code in TEAM_CODES.items():
            if normalize_text(known_name) == norm:
                return "nhl:" + known_code
        return "nhl:" + norm
    return khl_team_key(name)


def parse_score(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        for a, b in (("home", "away"), ("team_a", "team_b"), ("teamA", "teamB")):
            if a in value and b in value:
                try:
                    return int(value[a]), int(value[b])
                except (TypeError, ValueError):
                    pass
    found = re.search(r"(?<!\d)(\d+)\s*[:–—-]\s*(\d+)(?!\d)", str(value or ""))
    return (int(found.group(1)), int(found.group(2))) if found else None


def date_from_match(match: dict) -> dt.date | None:
    value = str(match.get("date") or "")[:10]
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def same_fixture(match: dict, league: str, home_key: str, away_key: str, start_local: dt.datetime) -> bool:
    if str(match.get("league", "")).upper() != league:
        return False
    mk_home = team_key(league, match.get("home"))
    mk_away = team_key(league, match.get("away"))
    if {mk_home, mk_away} != {home_key, away_key}:
        return False
    match_date = date_from_match(match)
    if match_date is None:
        return True
    return abs((match_date - start_local.date()).days) <= 1


def settled_result(match: dict) -> str | None:
    if match.get("result") in {"won", "lost", "push"}:
        return match["result"]
    if match.get("homeScore") is None or match.get("awayScore") is None or match.get("status") != "final":
        return None
    market = str(match.get("market") or "").lower()
    hs, aws = int(match["homeScore"]), int(match["awayScore"])
    if market in {"moneyline", "winner", "1x2"}:
        side = match.get("pickSide")
        if side not in {"home", "away"}:
            return None
        if hs == aws:
            return "push"
        actual = "home" if hs > aws else "away"
        return "won" if side == actual else "lost"
    if market in {"total_over", "total_under"}:
        line = match.get("totalLine")
        if line is None:
            found = re.search(r"(\d+(?:\.\d+)?)", str(match.get("selection") or ""))
            line = float(found.group(1)) if found else None
        if line is None:
            return None
        total = hs + aws
        if total == float(line):
            return "push"
        over = total > float(line)
        return "won" if (over and market == "total_over") or (not over and market == "total_under") else "lost"
    return None


def refresh_accounting(data: dict) -> None:
    starting = float(data.get("startingBank", 1000))
    profit = pending = 0.0
    graded = wins = losses = pushes = 0
    for match in data.get("matches", []):
        stake = float(match.get("stake") or 0)
        result = match.get("result")
        if result in {"won", "lost", "push"}:
            graded += 1
            if result == "won":
                wins += 1
                if stake > 0:
                    profit += stake * (float(match.get("odds") or 1) - 1)
            elif result == "lost":
                losses += 1
                profit -= stake
            else:
                pushes += 1
        elif match.get("status") in {"upcoming", "live"}:
            pending += stake
    equity = starting + profit
    data["bank"] = round(equity, 2)
    acc = data.setdefault("accounting", {})
    acc.update({
        "currency": "RUB", "virtual": True, "startingBank": starting,
        "settledProfit": round(profit, 2), "equityBeforeResults": round(equity, 2),
        "pendingStakeSum": round(pending, 2), "uncommittedCash": round(equity - pending, 2),
        "gradedWagers": graded, "wins": wins, "losses": losses, "pushes": pushes,
        "note": "Только виртуальный банк; старые прогнозы не переписываются."
    })


def get_nhl_games() -> list[dict]:
    try:
        payload = fetch_json(NHL_BASE + "/scoreboard/now")
        games = payload.get("games", []) if isinstance(payload, dict) else []
        if games:
            return games
    except Exception as exc:
        print("NHL scoreboard/now unavailable; using daily score fallback:", exc)
    games: list[dict] = []
    for offset in range(-7, 3):
        day = (NOW + dt.timedelta(days=offset)).date().isoformat()
        try:
            payload = fetch_json(NHL_SCORE_URL + day)
            games.extend(payload.get("games", []) if isinstance(payload, dict) else [])
        except Exception as exc:
            print(f"NHL score request failed for {day}: {exc}")
    return games


def get_khl_events() -> list[dict]:
    common = fetch_json(KHL_BASE + "/data.json?application=khl_web&locale=ru")
    stage_id = common.get("current_stage_id") if isinstance(common, dict) else None
    start_ts = int((NOW - dt.timedelta(days=8)).timestamp())
    end_ts = int((NOW + dt.timedelta(days=2)).timestamp())
    events: list[dict] = []
    for page in range(1, 9):
        params = {
            "application": "khl_web", "locale": "ru", "order_direction": "asc", "page": str(page),
            "q[start_at_gt_time_from_unixtime]": str(start_ts),
            "q[start_at_lt_time_from_unixtime]": str(end_ts),
        }
        if stage_id is not None:
            params["stage_id"] = str(stage_id)
        url = KHL_EVENTS_URL + "?" + urllib.parse.urlencode(params)
        payload = fetch_json(url)
        rows = payload if isinstance(payload, list) else payload.get("events", payload.get("data", [])) if isinstance(payload, dict) else []
        page_events = []
        for row in rows or []:
            event = row.get("event", row) if isinstance(row, dict) else None
            if isinstance(event, dict) and event.get("id") is not None:
                page_events.append(event)
        if not page_events:
            break
        before = len(events)
        known = {str(e.get("id")) for e in events}
        events.extend(e for e in page_events if str(e.get("id")) not in known)
        if len(events) == before:
            break
        if len(page_events) < 16:
            break
    return events


def nhl_game_info(game: dict) -> dict | None:
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    home_code, away_code = home.get("abbrev"), away.get("abbrev")
    if not home_code or not away_code:
        return None
    start = parse_iso(game.get("startTimeUTC"))
    if start is None:
        return None
    state = str(game.get("gameState") or "").upper()
    status = "final" if state in NHL_FINAL else "live" if state in NHL_LIVE else "upcoming"
    home_name = home.get("name", {}).get("default", home_code) if isinstance(home.get("name"), dict) else home_code
    away_name = away.get("name", {}).get("default", away_code) if isinstance(away.get("name"), dict) else away_code
    return {
        "league": "NHL", "id": str(game.get("id") or ""),
        "home": home_name, "away": away_name,
        "homeKey": "nhl:" + str(home_code), "awayKey": "nhl:" + str(away_code),
        "homeScore": home.get("score"), "awayScore": away.get("score"),
        "status": status, "startUtc": start, "startLocal": start.astimezone(OMSK),
        "state": state, "source": NHL_SCORE_URL + start.astimezone(OMSK).date().isoformat(),
        "raw": game,
    }


def khl_game_info(event: dict) -> dict | None:
    team_a = event.get("team_a") or {}
    team_b = event.get("team_b") or {}
    start_ms = event.get("start_at") or event.get("event_start_at")
    try:
        start = dt.datetime.fromtimestamp(float(start_ms) / 1000, UTC)
    except (TypeError, ValueError, OSError):
        return None
    home_name, away_name = team_a.get("name"), team_b.get("name")
    if not home_name or not away_name:
        return None
    state = str(event.get("game_state_key") or "").lower()
    status = "final" if state in KHL_FINAL else "live" if state in KHL_LIVE else "upcoming"
    score = parse_score(event.get("score"))
    if score is None and isinstance(event.get("scores"), dict):
        scores = event["scores"]
        score = parse_score(scores.get("third_period") or scores.get("bullitt") or scores.get("overtime"))
    return {
        "league": "KHL", "id": str(event.get("id") or event.get("khl_id") or ""),
        "khlId": event.get("khl_id"), "stageId": event.get("stage_id"),
        "home": str(home_name), "away": str(away_name),
        "homeKey": khl_team_key(home_name), "awayKey": khl_team_key(away_name),
        "homeScore": score[0] if score else None, "awayScore": score[1] if score else None,
        "status": status, "startUtc": start, "startLocal": start.astimezone(OMSK),
        "state": state, "source": "https://www.khl.ru/",
        "raw": event,
    }


def update_existing_match(match: dict, game: dict, changed: list[str]) -> None:
    league = str(match.get("league", "")).upper()
    expected_home = team_key(league, match.get("home"))
    expected_away = team_key(league, match.get("away"))
    actual_home, actual_away = game["homeKey"], game["awayKey"]
    reversed_order = actual_home == expected_away and actual_away == expected_home
    if not ((actual_home == expected_home and actual_away == expected_away) or reversed_order):
        return
    new_home = game.get("awayScore") if reversed_order else game.get("homeScore")
    new_away = game.get("homeScore") if reversed_order else game.get("awayScore")
    if game["status"] in {"live", "final"} and new_home is not None and new_away is not None:
        try:
            new_home, new_away = int(new_home), int(new_away)
        except (TypeError, ValueError):
            new_home = new_away = None
        if new_home is not None and (match.get("homeScore") != new_home or match.get("awayScore") != new_away):
            match["homeScore"], match["awayScore"] = new_home, new_away
            match["lastScoreCheckAt"] = NOW.isoformat()
            match["scoreSource"] = game["source"]
            changed.append(str(match.get("id")) + " score")
    old_status = match.get("status")
    new_status = game["status"]
    if old_status == "final" and new_status != "final":
        new_status = "final"
    if match.get("result") in {"won", "lost", "push"}:
        new_status = "final"
    if old_status != new_status:
        match["status"] = new_status
        changed.append(str(match.get("id")) + " status")
    if new_status == "final" and match.get("result") not in {"won", "lost", "push"}:
        result = settled_result(match)
        if result:
            match["result"] = result
            match["settledAt"] = NOW.isoformat()
            changed.append(str(match.get("id")) + " result")


def build_form(games: list[dict], league: str) -> dict[str, list[dict]]:
    form: dict[str, list[dict]] = {}
    for raw in games:
        info = nhl_game_info(raw) if league == "NHL" else khl_game_info(raw)
        if not info or info["status"] != "final" or info["homeScore"] is None or info["awayScore"] is None:
            continue
        hs, aws = int(info["homeScore"]), int(info["awayScore"])
        overtime = False
        if league == "NHL":
            outcome = raw.get("gameOutcome") or {}
            overtime = str(outcome.get("lastPeriodType") or "").upper() in {"OT", "SO"}
        else:
            scores = raw.get("scores") or {}
            overtime = bool(scores.get("overtime") or scores.get("bullitt"))
        if hs > aws:
            home_points, away_points = 2, (1 if overtime else 0)
        elif aws > hs:
            home_points, away_points = (1 if overtime else 0), 2
        else:
            home_points = away_points = 1
        for key, points, gd in ((info["homeKey"], home_points, hs - aws), (info["awayKey"], away_points, aws - hs)):
            form.setdefault(key, []).append({"date": info["startUtc"], "points": points, "gd": gd})
    for key in form:
        form[key].sort(key=lambda row: row["date"], reverse=True)
        form[key] = form[key][:5]
    return form


def form_rating(form: dict[str, list[dict]], key: str) -> tuple[float, int, float] | None:
    rows = form.get(key, [])
    if len(rows) < 3:
        return None
    points_pct = sum(float(r["points"]) for r in rows) / (2.0 * len(rows))
    avg_gd = sum(float(r["gd"]) for r in rows) / len(rows)
    rating = max(0.0, min(1.0, points_pct + 0.025 * avg_gd))
    return rating, len(rows), avg_gd


def make_new_match(game: dict, form: dict[str, list[dict]]) -> dict:
    league = game["league"]
    home_key, away_key = game["homeKey"], game["awayKey"]
    h_form, a_form = form_rating(form, home_key), form_rating(form, away_key)
    selection = "Пропуск — недостаточно свежих результатов для оценки"
    reason = "Автоматическая проверка формы: менее трёх подтверждённых завершённых матчей у одной или обеих команд. Коэффициент не подтверждён, ставка 0 ₽."
    market = "manual"
    pick_side = None
    prob = None
    record_type = "auto_skip"
    if h_form and a_form:
        h_rating, h_n, h_gd = h_form
        a_rating, a_n, a_gd = a_form
        p_home = max(0.35, min(0.65, 0.50 + 0.28 * (h_rating - a_rating) + 0.03))
        confidence = max(p_home, 1.0 - p_home)
        if abs(p_home - 0.5) >= 0.035:
            pick_side = "home" if p_home > 0.5 else "away"
            picked = game["home"] if pick_side == "home" else game["away"]
            prob = round(confidence * 100)
            market = "moneyline"
            selection = f"{picked} победит — предварительная оценка по последним матчам (без ставки)"
            record_type = "auto_form_forecast"
            reason = (
                f"Простая бесплатная эвристика по последним {h_n} матчам хозяев и {a_n} матчам гостей: "
                f"индекс формы {h_rating:.3f} против {a_rating:.3f}; средняя разница шайб "
                f"{h_gd:+.2f} и {a_gd:+.2f}. Оценка {prob}% не является калиброванной вероятностью. "
                "Коэффициент букмекера не подтверждён — ставка 0 ₽. Существующие прогнозы не меняются."
            )
        else:
            reason = (
                f"Форма близкая: индекс хозяев {h_rating:.3f}, гостей {a_rating:.3f}; "
                "перевес слишком мал. Пропуск, коэффициент не подтверждён, ставка 0 ₽."
            )
    local = game["startLocal"]
    event_id = re.sub(r"[^a-zA-Z0-9-]+", "-", str(game.get("id") or "")).strip("-")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{home_key}-{away_key}".lower()).strip("-")
    match_id = f"{league.lower()}-{local.strftime('%Y%m%d')}-{event_id or slug}"
    return {
        "id": match_id, "league": league, "date": local.strftime("%Y-%m-%dT%H:%M"),
        "home": game["home"], "away": game["away"], "market": market,
        "selection": selection, "odds": None, "stake": 0, "prob": prob,
        "status": "upcoming", "homeScore": None, "awayScore": None,
        "pickSide": pick_side, "result": None, "reason": reason,
        "source": game["source"], "lockedAt": NOW.astimezone(OMSK).isoformat(),
        "recordType": record_type, "scoreSource": game["source"],
        "forecastMethod": "last5_form_heuristic_v1",
        "oddsNote": "Коэффициент не подтверждён; виртуальная ставка = 0 ₽.",
    }


def fixture_exists(matches: list[dict], game: dict) -> bool:
    return any(same_fixture(m, game["league"], game["homeKey"], game["awayKey"], game["startLocal"]) for m in matches)


def main() -> int:
    data = json.loads(JOURNAL.read_text(encoding="utf-8"))
    data.setdefault("matches", [])
    data.setdefault("events", [])
    changed: list[str] = []
    errors: list[str] = []

    try:
        nhl_games = get_nhl_games()
    except Exception as exc:
        nhl_games = []
        errors.append(f"NHL source: {exc}")
    try:
        khl_events = get_khl_events()
    except Exception as exc:
        khl_events = []
        errors.append(f"KHL source: {exc}")

    normalized: list[dict] = []
    for raw in nhl_games:
        info = nhl_game_info(raw)
        if info:
            normalized.append(info)
    for raw in khl_events:
        info = khl_game_info(raw)
        if info:
            normalized.append(info)

    nhl_form = build_form(nhl_games, "NHL")
    khl_form = build_form(khl_events, "KHL")
    forms = {"NHL": nhl_form, "KHL": khl_form}

    # Update only existing score/status/result fields; never alter locked picks/odds/stakes.
    for match in data["matches"]:
        league = str(match.get("league", "")).upper()
        if league not in {"NHL", "KHL"}:
            continue
        for game in normalized:
            if game["league"] == league and same_fixture(match, league, game["homeKey"], game["awayKey"], game["startLocal"]):
                update_existing_match(match, game, changed)
                break

    # Discover and forecast every new fixture beginning in the next 36 hours.
    new_count = 0
    for game in normalized:
        if game["status"] != "upcoming" or not (NOW < game["startUtc"] <= HORIZON):
            continue
        if fixture_exists(data["matches"], game):
            continue
        new_match = make_new_match(game, forms[game["league"]])
        data["matches"].append(new_match)
        changed.append(new_match["id"] + " new forecast")
        new_count += 1

    if errors:
        data["sourceHealth"] = data.get("sourceHealth", {})
        data["sourceHealth"].update({"lastErrors": errors, "checkedAt": NOW.isoformat()})
        print("Source warnings:", " | ".join(errors))

    print(f"Fetched NHL games={len(nhl_games)}, KHL events={len(khl_events)}; added forecasts={new_count}; changed fields={len(set(changed))}.")
    if not changed:
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as output:
            output.write("changed=false\n")
        return 0

    data["lastCheckedAt"] = NOW.isoformat()
    data["updatedAt"] = NOW.astimezone(OMSK).isoformat()
    data["currentRun"] = data["updatedAt"]
    data["automationNote"] = (
        "Автоматическая проверка НХЛ и КХЛ. Счёт/статус обновляется из публичных табло; "
        "новые матчи получают только прозрачный прогноз по последним результатам без ставки. "
        "Букмекерские коэффициенты и live-ставки автоматически не создаются."
    )
    changed_unique = sorted(set(changed))
    detail = "Изменены записи: " + ", ".join(changed_unique[:30])
    if len(changed_unique) > 30:
        detail += f" и ещё {len(changed_unique) - 30} изменений"
    data["events"].append({
        "type": "hockey_data_update", "id": "hockey-update-" + NOW.strftime("%Y%m%dT%H%M%S"),
        "at": NOW.isoformat(), "title": "Обновлены результаты и новые прогнозы",
        "detail": detail, "source": "https://www.nhl.com/scores/ и https://www.khl.ru/",
        "newForecasts": new_count,
    })
    data["events"] = data["events"][-150:]
    refresh_accounting(data)
    JOURNAL.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as output:
        output.write("changed=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
