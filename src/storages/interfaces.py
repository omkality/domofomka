import contextlib
import typing as tp
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Iterable
from datetime import timedelta


class KeyValueClientProtocol(tp.Protocol):
    """Протокол клиента хранилища ключ-значение."""

    async def get(self, key: str) -> tp.Any:
        """Получение по ключу"""
        ...

    async def set(
        self,
        key: str,
        value: tp.Any,
        ttl: int | None = None,
        uttl: int | None = None,
        is_exists: bool = False,
        not_exist: bool = False,
        get_prev: bool = False,
    ) -> tp.Any:
        """:param key: Ключ
        :param value: Значение
        :param ttl: Время существования данных
        :param is_exists: Устанавливает значение для ключа, если значение уже существует
        :param not_exist: Устанавливает значение для ключа, только если значения еще не существует
        :param get_prev: Устанавливает значение для ключа,
        возвращает предыдущее значение, или None, если предыдущего не было
        :param uttl: Время сущеcтвования данных в unix
        :return:
        """
        ...

    async def append(
        self, key: str, *values: bytes | memoryview | str | int | float
    ) -> Awaitable[int] | int:
        """:param key: Ключ
        :param values: Значения
        """
        ...

    async def prepend(
        self, key: str, *values: bytes | memoryview | str | int | float
    ) -> Awaitable[int] | int:
        """:param key: Ключ
        :param values: Значения
        """
        ...

    async def delete(self, *keys: bytes | str | memoryview) -> tp.Any:
        """:param keys: Ключи"""
        ...

    async def list_set(self, key: str, index: int, value: tp.Any) -> None:
        """:param key: Ключ
        :param index: Индекс элемента
        :param value: Значение
        """
        ...

    async def list_range(
        self, key: str, start: int, end: int
    ) -> Awaitable[list] | list:
        """:param key: Ключ
        :param start: Индекс начала получаемого слайса
        :param end: Индекс конца получаемого слайса
        """
        ...

    async def multiple_get(
        self,
        keys: bytes | str | memoryview | Iterable[bytes | str | memoryview],
    ) -> Awaitable | tp.Any:
        """:param keys: Ключи"""
        ...

    async def multiple_set(self, mapping: dict) -> None:
        """:param mapping: Словарь пар {"ключ": "значение"}"""
        ...

    async def pop(self, key: str, count: int = None) -> tp.Any:
        """:param key: Ключ
        :param count: Количество удаляемых элементов с конца
        (При использовании вернет список удаленных элементов в порядке их удаления)
        """
        ...

    async def left_pop(self, key: str, count: int = None) -> tp.Any:
        """:param key: Ключ
        :param count: Количество удаляемых элементов с начала
        (При использовании вернет список удаленных элементов в порядке их удаления)
        """
        ...

    async def expire(
        self,
        key: str,
        ttl: int | timedelta,
        not_exist: bool = False,
        if_exist: bool = False,
        gt: bool = False,
        lt: bool = False,
    ) -> None:
        """:param key: Ключ
        :param ttl: Время жизни ключа, может быть указано как int, так и timedelta
        :param not_exist: Устанавливает время жизни ключа, только если у ключа его не было
        :param if_exist: Устанавливает время жизни ключа, только если у ключа оно было
        :param gt: Устанавливает время жизни ключа, только если у ключа оно было и оно было меньше устанавливаемого
        :param lt: Устанавливает время жизни ключа, только если у ключа оно было и оно было больше устанавливаемого
        """
        ...

    async def list_remove(self, key: str, value: str, count: int = 0) -> tp.Any:
        """:param key: Ключ
        :param value: Значение
        :param count: Количество удаляемых элементов. Если count > 0, удаляет count значений, начиная с начала списка,
        если count < 0, то с конца, если count = 0, то удаляет все
        """

    async def scan_iter(
        self,
        match: str | bytes | None = None,
        count: int | None = None,
        _type: str | None = None,
        **kwargs: tp.Any,
    ) -> AsyncIterator[bytes]:
        """:param match: Позволяет фильтровать ключи согласно паттерну
        :param count: Подсказывает Redis количество возвращаемых ключей за одну серию
        :param _type: Фильтрует значения по определенному типу данных Redis
        """
        ...

    async def hash_get(self, key: str, field: str) -> str | None:
        """Получает значение поля из хэша
        :param key: Ключ хэша
        :param field: Имя поля
        :return: Значение поля или None, если ключ или поле не существует
        """
        ...

    async def hash_set(
        self, key: str, mapping: dict[str, tp.Any] | None = None, **fields: tp.Any
    ) -> int:
        """Устанавливает поля в хэш
        :param key: Ключ хэша
        :param mapping: Словарь полей для установки (альтернатива fields)
        :param fields: Пары "поле=значение" для установки
        :return: Количество установленных полей
        """
        ...

    async def hash_del(self, key: str, *fields: str) -> int:
        """Удаляет поля из хэша
        :param key: Ключ хэша
        :param fields: Имена полей для удаления
        :return: Количество удаленных полей
        """
        ...

    async def hash_getall(self, key: str) -> dict[str, str]:
        """Получает все поля и значения из хэша
        :param key: Ключ хэша
        :return: Словарь полей и их значений
        """
        ...

    async def hash_exists(self, key: str, field: str) -> bool:
        """Проверяет, существует ли поле в хэше
        :param key: Ключ хэша
        :param field: Имя поля
        :return: True, если поле существует, иначе False
        """
        ...

    async def hash_keys(self, key: str) -> list[str]:
        """Получает все ключи (поля) из хэша
        :param key: Ключ хэша
        :return: Список полей
        """
        ...

    async def hash_vals(self, key: str) -> list[str]:
        """Получает все значения из хэша
        :param key: Ключ хэша
        :return: Список значений
        """
        ...

    async def hash_len(self, key: str) -> int:
        """Получает количество полей в хэше
        :param key: Ключ хэша
        :return: Количество полей
        """
        ...


class PipelineProtocol(KeyValueClientProtocol):
    """Протокол пайплайна"""

    pass


class KeyValueStorageProtocol(KeyValueClientProtocol):
    """Протокол хранилища ключ-значения"""

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncGenerator[PipelineProtocol, None]:  # type: ignore
        """Сессия для транзакционных операций."""
        ...
