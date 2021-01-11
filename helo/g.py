"""
    helo.g
    ~~~~~~
"""

import typing as typing

from helo import db
from helo import model
from helo import err


class Helo:

    def __init__(
        self,
        app: typing.Optional[typing.Any] = None,
        debug: bool = False,
    ):
        self.init_app(app)
        self.debug = debug
        self._database = None  # type: typing.Optional[db.Database]
        self._model_cls = model.new()

    @property
    def Model(self):
        return self._model_cls

    @property
    def is_connected(self):
        return self._database is not None and self._database.is_connected

    def init_app(self, app):
        if not app:
            return None

        self.app = app
        self.app.db = self

        @self.app.before_request
        async def _first():
            if not self._database.is_connected:
                await self.connect()

        return None

    async def connect(self, url: str = "", **options: typing.Any) -> None:
        if not self.is_connected:
            if not url and self.app is not None:
                url = self.app.config.get(db.ENV_KEY, "")
            self._database = db.Database(url, debug=self.debug, **options)

        self._model_cls.__db__ = self._database
        await self._database.connect()

    async def close(self):
        if self._database is None:
            raise err.UnconnectedError()

        await self._database.close()

    def transaction(self):
        return self._database.transaction()

    async def raw(self, query):
        return await self._database.execute(query)
