#!/usr/bin/env python3
"""Conservative NHL scoreboard sync for Hockey Predictor Lab.

Source: NHL's public web score endpoint. No API key is required.
This script never creates or edits predictions, odds, or stakes. It only
updates verified scores/status and settles an existing locked pick once.
KHL is intentionally not auto-updated until a reliable source is configured.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / "journal.json"
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)

# Russian names used by the current journal -> NHL API team abbreviations.
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
    "Оттава Сенаторз": "OTT",
}

FINAL_STATES = {"OFF", "FINAL", "FINAL/OT", "FINAL/SO"}
LIVE_STATES = {"LIVE", "CRIT"}

def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "HockeyPredictorLab/1.0"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))

def team_code(name: str | None) -> str | None:
    return TEAM_CODES.get(str(name or "").strip())

def game_code(game: dict, side: str) -> str | None:
    team = game.get(side, {}) or {}
    return team.get("abbrev") or team.get("triCode")

def settled_result(match: dict) -> str | None:
    """Grade only known, supported markets using scores aligned to journal order."""
    if match.get("result") in {"won", "lost", "push"}:
        return match["result"]
    if match.get("homeScore") is None or match.get("awayScore") is None:
        return None
    if match.get("status") != "final":
        return None
    stake = float(match.get("stake") or 0)
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
        is_over = total > float(line)
        return "won" if (is_over and market == "total_over") or (not is_over and market == "total_under") else "lost"
    return None

def refresh_accounting(data: dict) -> None:
    starting = float(data.get("startingBank", 1000))
    profit = 0.0
    pending = 0.0
    graded = 0
    wins = losses = pushes = 0
    for match in data.get("matches", []):
        stake = float(match.get("stake") or 0)
        result = match.get("result")
        if result in {"won", "lost", "push"}:
            graded += 1
            if result == "won":
                wins += 1
                profit += stake * (float(match.get("odds") or 1) - 1) if stake > 0 else 0
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
        "note": "Только виртуальный банк. Расчёт основан на зафиксированных прогнозах и подтверждённых итоговых счётах."
    })

def main() -> int:
    data = json.loads(JOURNAL.read_text(encoding="utf-8"))
    scores: list[dict] = []
    errors: list[str] = []
    dates = {(NOW + dt.timedelta(days=offset)).date().isoformat() for offset in (-1, 0, 1)}
    for day in sorted(dates):
        try:
            payload = fetch_json(f"https://api-web.nhle.com/v1/score/{day}")
            scores.extend(payload.get("games", []))
        except Exception as exc:
            errors.append(f"{day}: {exc}")

    changed: list[str] = []
    for match in data.get("matches", []):
        if str(match.get("league", "")).upper() != "NHL":
            continue
        expected_a = team_code(match.get("home"))
        expected_b = team_code(match.get("away"))
        if not expected_a or not expected_b:
            continue
        game = None
        reversed_order = False
        for candidate in scores:
            api_home = game_code(candidate, "homeTeam")
            api_away = game_code(candidate, "awayTeam")
            if api_home == expected_a and api_away == expected_b:
                game = candidate
                break
            if api_home == expected_b and api_away == expected_a:
                game = candidate
                reversed_order = True
                break
        if not game:
            continue

        api_h = (game.get("homeTeam") or {}).get("score")
        api_a = (game.get("awayTeam") or {}).get("score")
        if api_h is not None and api_a is not None:
            new_h, new_a = (api_a, api_h) if reversed_order else (api_h, api_a)
            if match.get("homeScore") != new_h or match.get("awayScore") != new_a:
                match["homeScore"], match["awayScore"] = new_h, new_a
                match["lastScoreCheckAt"] = NOW.isoformat()
                match["scoreSource"] = f"https://api-web.nhle.com/v1/score/{game.get('gameDate', '')}"
                changed.append(str(match.get("id")))

        state = str(game.get("gameState") or "").upper()
        new_status = "final" if state in FINAL_STATES else "live" if state in LIVE_STATES else "upcoming"
        if match.get("status") != new_status and (new_status != "upcoming" or match.get("status") != "final"):
            match["status"] = new_status
            changed.append(str(match.get("id")) + " status")
        if new_status == "final" and match.get("result") not in {"won", "lost", "push"}:
            result = settled_result(match)
            if result:
                match["result"] = result
                match["settledAt"] = NOW.isoformat()
                changed.append(str(match.get("id")) + " result")

    if errors:
        print("Some NHL date requests failed:", " | ".join(errors))
    print(f"Fetched {len(scores)} NHL game records; changed {len(set(changed))} journal fields.")
    if not changed:
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as output:
            output.write("changed=false\n")
        return 0

    data["lastCheckedAt"] = NOW.isoformat()
    data["currentRun"] = NOW.astimezone(dt.timezone(dt.timedelta(hours=6))).isoformat()
    event_id = "nhl-score-" + NOW.strftime("%Y%m%dT%H%M%S")
    data.setdefault("events", []).append({
        "type": "score_update", "id": event_id, "at": NOW.isoformat(),
        "title": "Обновлены подтверждённые результаты НХЛ",
        "detail": "Изменены записи: " + ", ".join(sorted(set(changed))),
        "source": "https://api-web.nhle.com/v1/score/"
    })
    refresh_accounting(data)
    data["automationNote"] = "Последняя автоматическая проверка НХЛ: " + NOW.isoformat() + ". KHL/live odds/new AI forecasts are not auto-generated."
    JOURNAL.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as output:
        output.write("changed=true\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
