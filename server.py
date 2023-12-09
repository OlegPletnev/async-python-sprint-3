import asyncio
import json
import os
import time
from asyncio import StreamReader

import aiofiles

from config import *

user_stats = dict(dict())


class Server:
    def __init__(self, host: str = chat.host, port: int = chat.port):
        self.host = host
        self.port = port

    async def trigger(self) -> None:
        """
        Непрерывный режим прослушки (serve_forever) входящих запросов
        c последующей их обработкой методом client_connected
        """
        logger.info('Start server')
        server = await asyncio.start_server(
            self.client_connected, chat.host, chat.port
        )
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            logger.info('Server shutting down...')

    async def client_connected(
            self, reader: StreamReader, writer: StreamWriter
    ) -> None:
        """
        Перехватывает соединение с сервером клиента.

        - Производит авторизацию пользователя
        - В случае успеха п.1 восстанавливает юзеру пул сообщений
        - Запускает Live Chat - обработка приема-отправки любых сообщений
        """

        address: tuple[str, int] | None = writer.get_extra_info('peername')
        username, is_new_user = await self.authorization(writer, reader)
        try:
            if username:
                logger.info('Start serving %s', username)
                await self.restore_messages(writer, is_new_user)
                await self.live_chat(writer, reader)
            else:
                logger.info('Client <%s> error while authorization', address)
        except ConnectionResetError:
            # self.delete_from_members(writer)
            logger.info(
                f'Client {username} error while run: ConnectionResetError'
            )

    async def authorization(self, writer: StreamWriter, reader: StreamReader
                            ) -> tuple[str, bool]:
        """
        - Авторизация пользователя с простым запросом логина и пароля.
        - Сохраняем всю информацию о вошедшем в чат новом пользователе.
        - Для уже существующего юзера проверяем корректность пароля и,
          если всё ОК, обновляем основной словарь user_stats

        Возвращаем кортеж: "юзернейм", "новый ли пользователь".
        """
        while True:
            username, password = await self.get_login_and_password(
                writer,
                reader
            )
            is_new_user = username not in user_stats
            if is_new_user:
                user_stats[username] = {
                    'counter_message': 0,
                    'ban': False,
                    'complains': set(),
                    'start_timeout': None,
                    'finish_timeout': None,
                    'password': password,
                    'writers': [writer]
                }

            fact_password = user_stats.get(username)['password']
            if fact_password == password:
                await self.remove_old_messages()
                welcome_message = f'\nWelcome to chat, {username}!\n'
                await Server.write_to_chat(writer, welcome_message)

                if not is_new_user:
                    user_stats[username]['writers'].append(writer)

                actual_streams.append(writer)
                user_from_stream[writer] = username

                return username, is_new_user

            await Server.write_to_chat(writer, 'Wrong password. Try again.\n')
            return 'login_error', False

    @staticmethod
    async def get_login_and_password(
            writer: StreamWriter,
            reader: StreamReader
    ) -> tuple[str, str]:
        """
        Просто запрашиваем у пользователя логин и пароль.
        Логин должен быть без пробелов.
        """
        try:
            login_correct = False
            login: str | None = None
            while not login_correct:
                await Server.write_to_chat(writer, 'Enter login: ')

                login_bytes = await reader.read(1024)
                login = login_bytes.decode().strip()
                if login.find(' ') == -1:
                    login_correct = True
                else:
                    await Server.write_to_chat(
                        writer, 'login must consist of one word\n'
                    )
            await Server.write_to_chat(writer, 'Enter password: ')
            password_bytes = await reader.read(1024)
            password = password_bytes.decode().strip()
            return login, password
        except asyncio.CancelledError:
            # ... когда пользователь закрыл терминал, не предоставив данные
            return '', ''

    async def restore_messages(
            self, writer: StreamWriter, is_new_user: bool
    ) -> None:
        """
        Зашедшему в чат пользователю выводятся последние сообщения из бэкапа.
        Алгоритм вывода сообщений зависит от того,
        новый ли пользователь или он уже ранее регистрировался в чате.

        Структура бэкап-csv файла: **Date, Sender, Recipient, Text**
        """
        async with aiofiles.open(chat.backup_file, 'r') as backup:
            messages = await backup.readlines()

        if len(messages) == 1:
            return

        if is_new_user:
            await self.restore_for_new_user(messages[1:], writer)
        else:
            await self.restore_for_reconnected_user(messages[1:], writer)

    @staticmethod
    async def restore_for_new_user(
            messages: list[str],
            writer: StreamWriter
    ) -> None:
        """
        После подключения НОВОМУ клиенту доступны последние
        backup_last_message сообщений из общего чата (20, по умолчанию).

        Просроченные сообщения (*с истекшим сроком жизни*),
        а также приватные, отфильтровываются.
         Структура бэкап-csv файла: **Timestamp, Sender, Recipient, Text**
        """
        logger.debug('NEW USER restore messages')
        count = 0
        current_time = time.time()
        output = []
        for message in reversed(messages):
            msg = message.split(sep=',')
            if current_time - float(msg[0]) > chat.lifetime_message:
                continue
            if count > chat.backup_last_message:
                break
            recipient = msg[2]
            if recipient == 'None':   # should be non-private message
                count += 1
                output.append(f'{msg[1]}: {msg[3]}')
        for message in reversed(output):
            writer.write(message.encode())
            await writer.drain()

    @staticmethod
    async def restore_for_reconnected_user(
            messages: list[str],
            writer: StreamWriter
    ) -> None:
        """
        Повторно подключенный клиент имеет возможность просмотреть
        все ранее непрочитанные сообщения до момента последнего опроса
        (как из общего чата, так и приватные).

        Ищется момент выхода пользователя и после него выводятся все
        непросроченные пропущенные сообщения из общего чата.

          Структура бэкап-csv файла: **Timestamp, Sender, Recipient, Text**
        """
        username = user_from_stream[writer]
        if len(user_stats[username]['writers']) > 1:
            logger.info('RECONNECTED USER already in the chat. '
                        'Restore messages cancelled')
            return
        logger.debug('RECONNECTED USER restore messages')

        index_first_message = 0
        for i, message in reversed(list(enumerate(messages))):
            msg = message.split(',')
            if msg[3].startswith('/exit') and username == msg[1]:
                index_first_message = i

        for message in messages[index_first_message:]:
            msg = message.split(',')
            sender = msg[1]
            recipient = msg[2]
            text = msg[3]

            text_to_restore = ''
            if username == sender and recipient == 'None':
                text_to_restore = f'you:\t{text}'
            elif username != sender and recipient == 'None':
                text_to_restore = f'{sender}:\t{text}'
            elif username == recipient:
                text_to_restore = f'>> {sender}:\t{text}'
            elif username == sender and recipient != 'None':
                text_to_restore = f'you >> {recipient}:\t{text}'
            else:
                pass
            await Server.write_to_chat(writer, text_to_restore)

    async def live_chat(
            self,
            writer: StreamWriter,
            reader: StreamReader
    ) -> None:
        """
        Метод реагирования на отправку сообщения пользователем.

        Происходит проверка количества отправленных сообщений:
        если пользователь перешел лимит сообщений, то ожидает конца периода.

        В конце периода время и количество сообщений сбрасывается.

        Проверяется и количество жалоб на пользователя.

        Обрабатываются:
          - команды
          - стандартные и приватные сообщения
          - а также выход пользователя из чата
            *(единственный триггер, который прерывает эту корутину)*.
        """
        while True:
            data = await reader.read(1024)
            if not data:
                break

            username = user_from_stream[writer]

            message = data.decode().strip()
            logger.debug(message)

            if message == '/exit':
                if len(user_stats[username]['writers']) == 1:
                    logger.info('%s wants to leave the chat', username)
                await self.leave_chat(writer, username)
                break

            if message == '/status':
                await self.show_status(writer)
                continue

            if message == '/rules':
                writer.write(chat.rules.encode())
                await writer.drain()
                continue

            try:
                if not await Server.wait_for_unblocking(username):
                    if message.startswith('/ban'):
                        await Server.add_ban(username, message)

                    elif message.startswith('/private'):
                        await Server.send_private(writer, message)
                    else:
                        await self.send_general(writer, username, message)
                    user_stats[username]['counter_message'] += 1

            except ConnectionResetError:
                logger.error(
                    f'User {username} lost the connection while waiting'
                )

        try:
            writer.close()
        except Exception as e:
            logger.info(f'conn close: {e}')

    @staticmethod
    async def add_ban(sender: user, message: str):
        """
        Корутина для отправки жалоб на пользователя.
        В случае отсутствия пользователя отсылает отправителю жалобы
        сообщение об ошибке,
        иначе добавляет пользователю жалобу, идентифицирующую как
        имя отправителя жалобы.
        Проверяется количество жалоб, и в случае превышения
        пользователь блокируется.
        """
        writers = user_stats[sender]['writers']
        banned = message.split()[1]
        if banned not in user_stats:
            text = f'Ban error: check username'
            await Server.write_to_chat(writers, text)
            return
        banned_writers = user_stats[banned]['writers']
        old_count_bans = len(user_stats[banned]['complains'])
        user_stats[banned]['complains'].add(sender)
        new_count_bans = len(user_stats[banned]['complains'])
        if old_count_bans != new_count_bans:
            if new_count_bans < 3:
                text = (f'Someone complained about you. '
                        f'Total complaints: {new_count_bans}.')
                await Server.write_to_chat(banned_writers, text)
            else:
                text = f"You've been complained about for 3 time."
                user_stats[banned]['ban'] = True
                user_stats[banned]['start_timeout'] = time.time()
                await Server.write_to_chat(banned_writers, text)
                await Server.wait_for_unblocking(banned)

    @staticmethod
    async def wait_for_unblocking(username: str) -> bool:
        """
        Корутина для блокировки пользователя.
        Используется, когда пользователь истратил весь лимит
        сообщений.
        Блокировка до окончания текущего периода.
        """
        result_msg = ''
        writers_list = user_stats[username]['writers']
        origin_timeout = user_stats[username]['start_timeout']
        if user_stats[username]['ban']:
            if origin_timeout + chat.ban_time > time.time():
                finish_timeout = origin_timeout + chat.ban_time
                timeout = finish_timeout - time.time()
                user_stats[username]['finish_timeout'] = finish_timeout
                t = time.strftime('%H:%M:%S', time.gmtime(timeout))
                result_msg = f'You are banned. Wait another {t}'
                await Server.write_to_chat(writers_list, result_msg)
                await asyncio.sleep(timeout)
            user_stats[username]['ban'] = False
            user_stats[username]['start_timeout'] = None
            user_stats[username]['finish_timeout'] = None
            user_stats[username]['complains'] = set()
        else:
            if user_stats[username]['counter_message'] == chat.limit_message:
                async with aiofiles.open(chat.backup_file, 'r') as file:
                    messages = await file.readlines()
                counter = 0
                time_0: float = 0
                for i in reversed(messages):
                    msg = i.split(',')
                    if msg[1] == username and not msg[3].startswith('/exit'):
                        if counter == chat.limit_message:
                            time_0 = float(msg[0])
                            break
                        counter += 1
                if time_0 + chat.limit_message > time.time():
                    timeout = time_0 + chat.limit_message - time.time()
                    t = time.strftime('%H:%M:%S', time.gmtime(timeout))
                    result_msg = (
                        f'You have reached the message limit. '
                        f'Wait another {t}')
                    await Server.write_to_chat(writers_list, result_msg)
                    await asyncio.sleep(timeout)
                user_stats[username]['counter_message'] = 0

        if result_msg:
            text = f'You can write messages again'
            await Server.write_to_chat(writers_list, text)
            logger.info(f'{username} can write messages again')
            return True
        return False

    @staticmethod
    async def leave_chat(writer: StreamWriter, username: str) -> None:
        """
        Отключаем пользователя со всеми его запущенными StreamWriter от чата.
        То есть, выйдя с одного устройства, user выйдет и из всех остальных.

        Отправляет флаг клиенту и
        сообщение о его выходе другим пользователям чата.

        Запускаем метод на удаление информации об объектах StreamWriter.

        Сама информация о выходе сохраняется в бэкап-файле.
        """
        try:
            writer.write(b'/end')
            await Server.send_bye_message(username)
        except Exception as e:
            logger.info(f'Client {username} out already')
        finally:
            await Server.store_message(username, '/exit')
            Server.delete_from_members(writer)

    @staticmethod
    async def send_bye_message(username: str) -> None:
        """
        Отправка в общий чат сообщения о выходе пользователя.
        """
        bye_message = f'User {username} has left the chat'
        writers = user_stats[username]['writers']
        for some_writer in actual_streams:
            if some_writer not in writers:
                await Server.write_to_chat(some_writer, bye_message)

    @staticmethod
    def delete_from_members(writer: StreamWriter) -> None:
        """
        Функция удаляет всю информацию о пользователе,
        связанную с его объектами StreamWriter
        """
        username = user_from_stream[writer]
        actual_streams.remove(writer)
        del user_from_stream[writer]
        user_stats[username]['writers'].remove(writer)
        if len(actual_streams) == 0:
            with open("user-stats.json", "w") as file:
                json.dump(user_stats, file)

    async def show_status(self, writer: StreamWriter) -> None:
        """
        Вывод состояния чата.
        """
        user = user_from_stream[writer]
        status = (
            f'======= CHAT INFO: ========\n'
            f'*\tHOST\t= {self.host}\n'
            f'*\tPORT\t= {self.port}\n'
            f'*\tUSERS ONLINE:\t {len(user_stats)}\n'
            f'*\tCONNECTIONS:\t{len(actual_streams)}\n'
            f'======= ABOUT YOU: ========\n'
            f'*\tHOW MANY CLIENTS\t= {len(user_stats[user]['writers'])}\n'
            f'*\tCOUNTER MESSAGE\t= {user_stats[user]['counter_message']}\n'
            f'*\tAMOUNT OF COMPLAINTS\t= {len(user_stats[user]['complains'])}\n'
        )
        if user_stats[user]['ban'] or user_stats[user]['finish_timeout']:
            t = time.strftime(
                '%H:%M:%S',
                time.gmtime(user_stats[user]["finish_timeout"] - time.time())
            )
            status += (
                f'*\tBANNED FOR\t{t}\n'
            )
        await Server.write_to_chat(writer, status)

    @staticmethod
    async def send_private(sender_writer: StreamWriter,  message: str) -> None:
        """
        Отправка приватных сообщений, если получатель -
        среди актуальных пользователей чата.

        Иначе, выводится сообщение об ошибке.

        Корректные приватные сообщения сохраняются в бэкап-файле.
        """
        sender_name = user_from_stream[sender_writer]
        sender_writers = user_stats[sender_name]['writers']
        try:
            recipient_name = message.split(' ')[1]
            message = ' '.join(message.split(' ')[2:])

            if recipient_name not in user_stats:
                text = f'No such user - "{recipient_name}"\n'
                await Server.write_to_chat(sender_writers, text)
                return
            if recipient_name == sender_name:
                text = 'No point in sending it to yourself\n'
                await Server.write_to_chat(sender_writers, text)
                return
            logger.debug(f'Сообщение от {sender_name} к {recipient_name}:')
            logger.debug(message)

            for some_writer in user_stats[recipient_name]['writers']:
                text = f'>> {sender_name}:\t{message}'
                await Server.write_to_chat(some_writer, text)
                await Server.store_message(
                    sender_name, message, recipient_name
                )

        except IndexError:
            error_message = (
                'Incorrect syntax for private message.\n'
                'Template: /private <username> <message>\n'
            )
            await Server.write_to_chat(sender_writers, error_message)

    async def send_general(self, writer: StreamWriter, sender: str, text: str) -> None:
        """
        Отправка сообщений в общий чат.
        Для отправителя сообщение выводится с приставкой "you".
        """
        await self.store_message(sender, text)
        if text.strip() != '':
            for some_writer in actual_streams:
                if user_from_stream[some_writer] != sender:
                    message = f'{sender}:\t{text}'
                    await Server.write_to_chat(some_writer, message)
                else:
                    if some_writer != writer:
                        message = f'you:\t{text}'
                        await Server.write_to_chat(some_writer, message)


    @staticmethod
    async def store_message(
            sender: user, text: str, recipient: user = None
    ) -> None:
        """
        Метод для сохранения сообщения в файл.
        """
        async with aiofiles.open(chat.backup_file, 'a') as file:
            message_time = time.time()
            message = f'{message_time},{sender},{recipient},{text}\n'
            await file.write(message)

    async def show_messages_upon_login(self, username, is_new_user) -> None:
        pass

    @staticmethod
    async def remove_old_messages():
        async with aiofiles.open(chat.backup_file, 'r') as file:
            print('Removing old messages')
            text = await file.readlines()
            records = [text[0]]
        if len(text) > 1:
            current_time = time.time()
            for i in text[1:]:
                if (current_time - float(i.split(',')[0]) <
                        chat.lifetime_message):
                    records.append(i)
        async with aiofiles.open(chat.backup_file, 'w') as file:
            await file.writelines(records)

    @staticmethod
    async def write_to_chat(
            writers: StreamWriter | list[StreamWriter], message: str
    ) -> None:
        if message:
            if isinstance(writers, StreamWriter):
                writers.write(message.encode())
                await writers.drain()
            else:
                for writer in writers:
                    writer.write(message.encode())
                    await writer.drain()


if __name__ == '__main__':
    chat_server = Server()
    if (not os.path.exists(chat.backup_file)
            or os.path.getsize(chat.backup_file) == 0):
        with open(chat.backup_file, 'w') as f:
            f.write('Timestamp,Sender,Recipient,Text\n')
            logger.info('Backup file is created')
    if (os.path.exists("user-stat.json")
            and os.path.getsize("user-stat.json") != 0):
        with open("user-stat.json", "r") as f:
            user_stats = json.load(f)
    try:
        asyncio.run(chat_server.trigger())
    except (KeyboardInterrupt, RuntimeError):
        logger.info('Server was stopped')
