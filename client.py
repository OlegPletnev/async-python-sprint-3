import asyncio
from aioconsole import ainput

from config import chat, logger


class Client:
    def __init__(self, host: str = chat.host, port: int = chat.port):
        self.host = host
        self.port = port
        self.is_server_work = True
        self.reader = None
        self.writer = None

    async def connect_chat(self) -> None:
        """
        Подключение к серверу.
        Ожидает завершения прочих сопрограмм по отправке и чтению сообщений.
        """
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port
        )
        await asyncio.gather(self.receive(), self.send())

    async def receive(self) -> None:
        """
        Корутина для непрерывного чтения сообщений.
        Работает до получения информации о завершении работы сервера.
        """
        while True:
            try:
                message = await self.reader.read(1024)
                if message.decode() in ['/end', '']:
                    self.is_server_work = False
                    break
                else:
                    logger.info(message.decode())

            except Exception as e:
                logger.error('read message error ', e)

    async def send(self) -> None:
        """
        Корутина для отправки сообщений.
        Завершает работу по отправке сообщения /exit или отключения сервера.
        """
        while True:
            try:
                message = await ainput('')
                if message.strip() != '':
                    self.writer.write(message.encode())
                    await self.writer.drain()
                if message == '/exit' or not self.is_server_work:
                    break
            except Exception as e:
                logger.error('send_message_error', e)


if __name__ == '__main__':
    client = Client()
    try:
        asyncio.run(client.connect_chat())
    except (KeyboardInterrupt, ConnectionRefusedError, RuntimeError):
        logger.error('STOP')
