import logging
import sys
from asyncio import StreamWriter
from typing import TypedDict

from pydantic_settings import BaseSettings

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler(stream=sys.stdout))


class Settings(BaseSettings):
    host: str = '127.0.0.1'     # хост, на котором будет запущен сервер
    port: int = 8000            # порт, на котором будет слушать сервер
    backup_file: str = 'backup.csv'
    backup_last_message: int = 20
    lifetime_message: int = 3600  # период жизни доставленных сообщений (сек)
    limit_message: int = 20     # кол-во сообщений для 1 клиента за limit_time
    limit_time: int = 1 * 3600  # сколько (в сек) выделено для limit_message
    ban_time: int = 4 * 3600    # сколько времени (в секундах) идет блокировка
    rules: str = (
        'Be polite to other chat participants, otherwise,\n'
        'after three complaints, you will be banned for 4 hours.\n'
        '\nSome commands:\n'
        '*\t/rules -- show these chat rules\n'
        '*\t/status -- show info about the chat\n'
        '*\t/private <username> <message> -- send private message\n'
        '*\t/ban <username> -- complain about some user\n'
        '*\t/exit -- log out of the chat\n\n'
        'You can send a maximum of 20 messages '
        'to a public or private chat in one hour\n'
    )


chat = Settings()

type user = str                             # В коде чаще встречается username

actual_streams: list[StreamWriter] = []  # список актуальных клиентов (сокетов)
user_from_stream: dict[StreamWriter, user] = {}  # {writer: username}


class UserStats(TypedDict):
    counter_message: int          # Счётчик сообщений за лимитированный период
    finish_timeout: float | None  # Конец заблокированного периода
    complains: set[user]          # Список желающих забанить юзера
    ban: bool                     # Есть ли бан у юзера?
    start_timeout: float | None   # Время начала блокировки
    writers: list[StreamWriter]   # Ведь юзер может иметь несколько клиентов
    password: str                 # Пароль к логину


# user_stats: dict[user, UserStats]
# user_stats = dict[str, {}]
