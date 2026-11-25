import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from config import load_settings
from database import Database
from hh_client import VacancySearchResult
from main import run_search_cycle
from tests.test_config import VALID_ENV, write_profile
from tg_bot import AgentControl, TelegramService


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)


class FakeApprovalService:
    async def approve_and_apply(self, *_args):
        raise AssertionError("approval must not run in search-queries tests")

    def skip(self, *_args):
        raise AssertionError("skip must not run in search-queries tests")


class FakeMessage:
    def __init__(self, text: str, *, user_id: int = 42) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeAnalyzer:
    async def assess(self, title: str, description: str):
        raise AssertionError("no vacancies expected in search-queries tests")


class FakeTelegram:
    def __init__(self) -> None:
        self.notifications: list[str] = []
        self.previews: list[tuple[str, bool]] = []

    async def send_preview(self, vacancy, include_actions: bool) -> None:
        self.previews.append((vacancy.id, include_actions))

    async def notify(self, text: str) -> None:
        self.notifications.append(text)

    async def notify_analysis_failed(self, title: str, url: str, error_type: str) -> None:
        self.notifications.append(f"analysis_failed:{error_type}")

    async def request_captcha(self, screenshot, title: str, timeout_seconds: int):
        return None


class RecordingHHClient:
    def __init__(self) -> None:
        self.searched: list[str] = []

    async def search_vacancies(
        self, query, areas, experience_filters, *, remote_only=False
    ):
        self.searched.append(query)
        return VacancySearchResult([], 0, 0)

    async def check_messages(self, notify) -> None:
        return None


def settings(tmp_path: Path):
    # Владелец меняет запросы без перезапуска и в dry_run — фиксируем этот режим.
    loaded = load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, "TG_USER_ID": "42", "APP_MODE": "dry_run"},
    )
    return replace(loaded, database_path=tmp_path / "agent.db")


def service(tmp_path: Path) -> tuple[TelegramService, Database]:
    app_settings = settings(tmp_path)
    database = Database(app_settings.database_path)
    database.init()
    telegram = TelegramService(
        app_settings,
        database,
        FakeApprovalService(),
        AgentControl(),
        bot=FakeBot(),
        now_factory=lambda: NOW,
    )
    return telegram, database


def run_cycle(app_settings, database: Database) -> RecordingHHClient:
    client = RecordingHHClient()
    asyncio.run(
        run_search_cycle(
            app_settings,
            database,
            client,
            FakeAnalyzer(),
            FakeTelegram(),
            AgentControl(),
            now_factory=lambda: NOW,
        )
    )
    return client


def test_search_query_override_persists_across_database_reopen(tmp_path: Path) -> None:
    database = Database(tmp_path / "agent.db")
    database.init()
    assert database.search_query_override() is None

    database.set_search_query_override(["  A  ", "", "B C, D"])
    assert database.search_query_override() == ["A", "B C, D"]

    # Перезапуск процесса: новый экземпляр Database поверх того же файла.
    reopened = Database(tmp_path / "agent.db")
    reopened.init()
    assert reopened.search_query_override() == ["A", "B C, D"]

    reopened.set_search_query_override(["Rust"])
    assert reopened.search_query_override() == ["Rust"]


def test_queries_command_shows_defaults_then_replaces(tmp_path: Path) -> None:
    telegram, database = service(tmp_path)

    shown = telegram.command("queries", user_id=42)
    assert "1. Python developer" in shown
    assert "profile.yaml" in shown
    assert database.search_query_override() is None

    reply = telegram.command("queries", user_id=42, args="Backend, Go,  ")
    assert "1. Backend" in reply
    assert "2. Go" in reply
    assert database.search_query_override() == ["Backend", "Go"]

    # Повторный /queries показывает уже сохранённый список.
    shown_after = telegram.command("queries", 42)
    assert "1. Backend" in shown_after
    assert "2. Go" in shown_after
    assert "сохранены через /queries" in shown_after
    assert "profile.yaml" not in shown_after


def test_queries_via_real_handler_uses_comma_separated_args(tmp_path: Path) -> None:
    telegram, database = service(tmp_path)

    message = FakeMessage("/queries@HHAgentBackend Тестировщик, 1С,  Data Engineering,")
    asyncio.run(telegram._command_handler(message))

    assert "Тестировщик" in message.answers[0]
    assert "1С" in message.answers[0]
    assert "Data Engineering" in message.answers[0]
    assert database.search_query_override() == [
        "Тестировщик",
        "1С",
        "Data Engineering",
    ]


def test_queries_empty_args_keeps_previous_list(tmp_path: Path) -> None:
    telegram, database = service(tmp_path)
    telegram.command("queries", 42, args="Python")

    reply = telegram.command("queries", 42, args=" ,  , ")

    assert reply.startswith("Список запросов пуст")
    assert database.search_query_override() == ["Python"]


def test_foreign_user_cannot_see_or_change_queries(tmp_path: Path) -> None:
    telegram, database = service(tmp_path)

    assert telegram.command("queries", user_id=99) == "This bot is private."

    message = FakeMessage("/queries Go", user_id=99)
    asyncio.run(telegram._command_handler(message))
    assert message.answers == ["This bot is private."]
    assert database.search_query_override() is None


def test_override_reaches_next_search_cycle_without_settings_reload(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path)  # один объект Settings на все циклы
    database = Database(app_settings.database_path)
    database.init()

    # 1) Пока override нет — берётся список из profile.yaml.
    first = run_cycle(app_settings, database)
    assert first.searched == ["Python developer"]

    # 2) Владелец меняет запросы через Telegram.
    telegram, _ = service(tmp_path)
    asyncio.run(telegram._command_handler(FakeMessage("/queries Backend, Data Mining")))
    # /queries пишет в тот же путь базы (tmp_path/agent.db), читаем тем же объектом.
    telegram_database = Database(app_settings.database_path)
    telegram_database.init()
    assert telegram_database.search_query_override() == ["Backend", "Data Mining"]

    # 3) Следующий цикл использует новый список без перезапуска.
    second = run_cycle(app_settings, database)
    assert second.searched == ["Backend", "Data Mining"]

    # 4) После перезапуска (новый Database поверх той же базы) список сохраняется.
    restarted = Database(app_settings.database_path)
    restarted.init()
    third = run_cycle(app_settings, restarted)
    assert third.searched == ["Backend", "Data Mining"]
