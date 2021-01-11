from __future__ import annotations

import warnings
import re
from copy import deepcopy
from typing import Any, Dict, Optional, List, Union, Tuple, Type

from helo import db
from helo import util
from helo import err
from helo import _sql
from helo import _helper
from helo.types import _abc as types, func


ROWTYPE = util.adict(
    MODEL=1,
    ADICT=2,
)
JOINTYPE = util.adict(
    INNER='INNER',
    LEFT='LEFT',
    RIGHT='RIGHT',
)

_BUILTIN_MODEL_NAMES = ("ModelBase", "Model", "Model1")


class ModelType(type):

    def __new__(
        cls, name: str, bases: Tuple[type, ...], attrs: Dict[str, Any]
    ) -> ModelType:

        def gen_attrs():
            model_fields, model_attrs = {}, {}
            for attr in attrs.copy():
                field = attrs[attr]
                if isinstance(field, types.Field):
                    field.name = field.name or attr
                    model_fields[attr] = field
                    model_attrs[field.name] = attr
                    attrs.pop(attr)

            baseclass = bases[0] if bases else None
            if baseclass:
                base_table = deepcopy(baseclass.__table__)
                if base_table:
                    base_table.fields_dict.update(model_fields)
                    model_fields = base_table.fields_dict
                    base_names = deepcopy(baseclass.__attrs__)
                    base_names.update(model_attrs)
                    model_attrs = base_names

            metaclass = attrs.get('Meta')
            if not metaclass:
                metaclass = getattr(baseclass, 'Meta', None)

            indexes = getattr(metaclass, 'indexes', [])
            if indexes and not isinstance(indexes, (tuple, list)):
                raise TypeError("indexes type must be `tuple` or `list`")
            for index in indexes:
                if not isinstance(index, types.Index):
                    raise TypeError(f"invalid index type {type(index)}")

            primary = util.adict(auto=False, field=None, attr=None, begin=None)
            for attr_name, field in model_fields.items():
                if getattr(field, 'primary_key', None):
                    if primary.field is not None:
                        raise err.DuplicatePKError(
                            "duplicate primary key found for field "
                            f"{field.name}"
                        )
                    primary.field = field
                    primary.attr = attr_name
                    if getattr(field, "auto", False):
                        primary.auto = True
                        primary.begin = int(field.auto)
                        if field.name != types.Table.PK:
                            warnings.warn(
                                "The field name of AUTO_INCREMENT "
                                "primary key is suggested to use "
                                f"`id` instead of {field.name}",
                                err.ProgrammingWarning)

            attrs["__attrs__"] = model_attrs
            attrs["__table__"] = types.Table(
                name=getattr(metaclass, 'name', _helper.snake_name(name)),
                fields_dict=model_fields,
                primary=primary,
                indexes=indexes,
                engine=getattr(metaclass, "engine", None),
                charset=getattr(metaclass, "charset", None),
                comment=getattr(metaclass, "comment", None),
            )

            return attrs

        attrs["__table__"] = None
        if name not in _BUILTIN_MODEL_NAMES:
            attrs = gen_attrs()

        attrs["__rowtype__"] = ROWTYPE.MODEL
        return type.__new__(cls, name, bases, attrs)  # type: ignore

    def __getattr__(cls, name: str) -> Any:
        if cls.__table__ is not None:
            if name in cls.__table__.fields_dict:
                return cls.__table__.fields_dict[name]

        raise AttributeError(
            f"'{cls.__name__}' class does not have attribute '{name}'"
        )

    def __repr__(cls) -> str:
        if cls.__name__ in _BUILTIN_MODEL_NAMES:
            return f"helo.{cls.__name__}"
        return f"Model<{cls.__name__}>"

    def __str__(cls) -> str:
        return cls.__name__

    def __hash__(cls) -> int:
        if cls.__table__:
            return hash(cls.__table__)
        return 0

    def __aiter__(cls) -> Select:
        return ApiProxy.select(cls)  # type: ignore


class ModelBase:

    def __init__(self, **kwargs: Any) -> None:
        for attr in kwargs:
            setattr(self, attr, kwargs[attr])

    def __repr__(self) -> str:
        id_ = getattr(self, self.__table__.primary.attr, None)
        return f"<{self.__class__.__name__} object {id_}>"

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.__dict__})"

    def __hash__(self) -> int:
        return hash(self.__table__)

    def __eq__(self, other) -> bool:
        return self.__dict__ == other.__dict__

    def __setattr__(self, name: str, value: Any) -> None:
        f = self.__table__.fields_dict.get(name)
        if not f:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}'"
            )

        value = f.py_value(value)
        self.__dict__[name] = value

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__[name]
        except KeyError:
            if name in self.__table__.fields_dict:
                return None
            raise AttributeError(
                f"'{self.__class__}' object has no attribute '{name}'"
            )

    def __bool__(self) -> bool:
        return bool(self.__dict__)

    @property
    def __self__(self) -> Dict[str, Any]:
        return deepcopy(self.__dict__)


class Fetcher(_sql.ClauseElement):

    __slots__ = ('_model', '_props', '_aliases')
    __r__ = True

    def __init__(self, model: ModelType) -> None:
        self._model = model
        self._props = util.adict()
        self._aliases = {}  # type: Dict[str, Any]

    def __repr__(self) -> str:
        return repr(self.query())

    def __str__(self) -> str:
        return str(self.query())

    async def __do__(self, **props) -> Any:
        database = self._model.__db__
        if database is None:
            raise err.UnconnectedError(
                "Database is not connected yet, "
                "please call `connect` before"
            )

        if props:
            self._props.update(props)

        return await database.execute(self.query(), **self._props)

    def query(self) -> _sql.Query:
        ctx = _sql.Context.from_clause(self)
        self._aliases = ctx._aliases
        q = ctx.query()
        q.r = self.__r__
        return q


class Executor(Fetcher):

    __slots__ = ()
    __r__ = False

    async def do(self) -> db.ExeResult:
        return await self.__do__()


class Select(Fetcher):

    __slots__ = (
        '_columns', '_froms', '_where',
        '_group_by', '_having', '_order_by', '_limit',
        '_offset', '_gotlist', '_gotidx', '_joined',
    )
    _SINGLE = 1
    _BATCH = 200

    def __init__(
        self,
        model: ModelType,
        columns: Union[List[types.Column], List[_sql.SQL]]
    ) -> None:
        super().__init__(model)
        self._columns = columns
        self._where = None
        self._group_by = None
        self._having = None
        self._order_by = None
        self._limit = None     # type: Optional[int]
        self._offset = None    # type: Optional[int]
        self._froms = [model.__table__]  # type:List[_sql.ClauseElement]
        self._gotlist = []     # type: List[ModelBase]
        self._gotidx = 0
        self._joined = False

    def join(
        self,
        target: ModelType,
        join_type: str = JOINTYPE.INNER,
        on: Optional[types.Expression] = None
    ) -> Select:
        if not self._model.__db__ is target.__db__:
            raise ValueError("illegal join")

        lt = self._froms.pop()
        rt = target.__table__
        self._froms.append(Join(lt, rt, join_type, on))  # type:ignore
        self._joined = True
        return self

    def where(self, *filters: _sql.ClauseElement) -> Select:
        self._where = util.and_(*filters) or None
        return self

    def group_by(self, *columns: types.Column) -> Select:
        if not columns:
            raise ValueError("group by clause cannot be empty")
        for f in columns:
            if not isinstance(f, types.Column):
                raise TypeError(
                    f"invalid value '{f}' for group_by field"
                )

        self._group_by = columns  # type: ignore
        return self

    def having(self, *filters: _sql.ClauseElement) -> Select:
        self._having = util.and_(*filters) or None
        return self

    def order_by(self, *columns: types.Column):
        if not columns:
            raise ValueError("order by clause cannot be empty")
        for f in columns:
            if not isinstance(f, types.Column):
                raise TypeError(
                    f"invalid value '{f}' for order_by field")

        self._order_by = columns  # type: ignore
        return self

    def limit(self, limit: int = 500) -> Select:
        self._limit = limit
        return self

    def offset(self, offset: Optional[int] = 0) -> Select:
        if self._limit is None:
            raise err.NotAllowedOperation("offset clause has no limit")
        self._offset = offset
        return self

    async def get(
        self, wrap: bool = True
    ) -> Union[None, util.adict, ModelBase]:
        """If "wrap" is False, the returned row type is not
        wrapped as the ``Model`` object, and the original
        ``helo.util.adict`` is used
        """
        return await self.__do__(rows=self._SINGLE, wrap=wrap)

    async def first(
        self, wrap: bool = True
    ) -> Union[None, util.adict, ModelBase]:
        """If "wrap" is False, the returned row type is not
        wrapped as the ``Model`` object, and the original
        ``helo.util.adict`` is used
        """
        self.limit(self._SINGLE)
        return await self.__do__(rows=self._SINGLE, wrap=wrap)

    async def rows(
        self,
        rows: int,
        start: int = 0,
        wrap: bool = True
    ) -> Union[List[util.adict], List[ModelBase]]:
        """If "wrap" is False, the returned row type is not
        wrapped as the ``Model`` object, and the original
        ``helo.util.adict`` is used
        """
        self.limit(rows).offset(start)
        if rows <= 0:
            raise ValueError(f"invalid select rows: {rows}")
        return await self.__do__(wrap=wrap)

    async def paginate(
        self,
        page: int,
        size: int = 20,
        wrap: bool = True
    ) -> Union[List[util.adict], List[ModelBase]]:
        """If "wrap" is False, the returned row type is not
        wrapped as the ``Model`` object, and the original
        ``helo.util.adict`` is used
        """
        if page < 0 or size <= 0:
            raise ValueError("invalid page or size")
        if page > 0:
            page -= 1
        self._limit = size
        self._offset = page * size
        return await self.__do__(wrap=wrap)

    async def all(
        self, wrap: bool = True
    ) -> Union[List[util.adict], List[ModelBase]]:
        """If "wrap" is False, the returned row type is not
        wrapped as the ``Model`` object, and the original
        ``helo.util.adict`` is used
        """
        return await self.__do__(wrap=wrap)

    async def scalar(self) -> Union[int, Dict[str, int]]:
        row = await self.first(wrap=False)  # Optional[util.adict]
        if not row:
            return 0
        return tuple(row.values())[0] if len(row) == 1 else row

    async def count(self) -> int:
        self._columns = [func.F.COUNT(_sql.SQL('1'))]  # type: ignore
        return await self.scalar()  # type: ignore

    async def exist(self) -> bool:
        return bool(await self.limit(self._SINGLE).count())

    async def __do__(self, **props) -> Any:
        wrap = props.pop('wrap', False)
        if wrap is True and self._joined:
            wrap = False
        return Loader(
            data=await super().__do__(**props),
            model=self._model,
            aliases=self._aliases,
            wrap=wrap
        )()

    async def __row__(self) -> Optional[ModelBase]:
        async def fetch():
            self._gotlist = await (
                self.limit(self._BATCH).offset(self._gotidx)
                .all())

        if not self._gotlist:
            await fetch()
        elif self._gotlist and self._gotidx >= self._BATCH:
            await fetch()
            self._gotidx = 0
        try:
            return self._gotlist[self._gotidx]
        except IndexError:
            return None

    def __aiter__(self) -> Select:
        return self

    async def __anext__(self) -> Optional[ModelBase]:
        row = await self.__row__()
        if row is None:
            raise StopAsyncIteration
        self._gotidx += self._SINGLE
        return row

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.props.select = True
        ctx.literal(
            "SELECT "
        ).sql(
            _sql.CommaClauseElements(self._columns)  # type: ignore
        ).literal(
            " FROM "
        ).sql(_sql.CommaClauseElements(self._froms))

        if self._where:
            ctx.literal(" WHERE ").sql(self._where)

        if self._group_by:
            ctx.literal(
                " GROUP BY "
            ).sql(_sql.CommaClauseElements(self._group_by))

        if self._having:
            ctx.literal(" HAVING ").sql(self._having)

        if self._order_by:
            ctx.literal(
                " ORDER BY "
            ).sql(_sql.CommaClauseElements(self._order_by))

        if self._limit is not None:
            ctx.literal(f" LIMIT {self._limit}")

        if self._offset is not None:
            ctx.literal(f" OFFSET {self._offset}")
        return ctx


class Insert(Executor):

    __slots__ = ('_values', '_from')

    def __init__(
        self,
        model: ModelType,
        values: Union[ValuesMatch, List[types.Field]],
        many: bool = False
    ) -> None:
        super().__init__(model)
        self._values = values
        self._from = None  # type: Optional[Select]
        if many:
            self._props.many = True

    def from_(self, select: Select) -> Insert:
        if not isinstance(select, Select):
            raise TypeError(
                'from select clause must be `Select` object')
        self._from = select
        return self

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.literal(
            "INSERT INTO "
        ).sql(
            self._model.__table__
        )

        if isinstance(self._values, ValuesMatch):
            ctx.sql(self._values)
        elif isinstance(self._values, list):
            for i, f in enumerate(self._values):
                if isinstance(f, str):
                    self._values[i] = _sql.SQL(f.join('``'))
            ctx.literal(' ').sql(_sql.EnclosedNodeList(self._values))  # type: ignore
        if self._from:
            ctx.literal(' ').sql(self._from)

        return ctx


class Replace(Executor):

    __slots__ = ('_values', '_from')

    def __init__(
        self,
        model: ModelType,
        values: Union[ValuesMatch],
        many: bool = False
    ) -> None:
        super().__init__(model)
        self._values = values
        if many:
            self._props.many = True

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.literal(
            "REPLACE INTO "
        ).sql(self._model.__table__)

        ctx.sql(self._values)
        return ctx


class Update(Executor):

    __slots__ = ('_values', '_from', '_where')

    def __init__(
        self, model: ModelType, values: AssignmentList
    ) -> None:
        super().__init__(model)
        self._values = values
        self._from = None  # type: Optional[types.Table]
        self._where = None

    def from_(self, source: ModelType) -> Update:
        self._from = source.__table__
        return self

    def where(self, *filters: types.Column) -> Update:
        self._where = util.and_(*filters) or None
        return self

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.literal(
            "UPDATE "
        ).sql(
            self._model.__table__
        ).literal(
            " SET "
        )

        if self._from is not None:
            ctx.props.update_from = True
        ctx.sql(self._values)
        if self._from is not None:
            ctx.literal(" FROM ").sql(self._from)

        if self._where is not None:
            ctx.literal(" WHERE ").sql(self._where)
        return ctx


class Delete(Executor):

    __slots__ = ('_where', '_limit', '_force')

    def __init__(self, model: ModelType, force: bool = False) -> None:
        super().__init__(model)
        self._where = None
        self._limit = None  # type: Optional[int]
        self._force = force

    def where(self, *filters: types.Column) -> Delete:
        self._where = util.and_(*filters) or None
        return self

    def limit(self, row_count: int) -> Delete:
        self._limit = row_count
        return self

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.literal(
            "DELETE FROM "
        ).sql(
            self._model.__table__
        )
        if self._where:
            ctx.literal(
                " WHERE "
            ).sql(self._where)
        elif not self._force:
            raise err.DangerousOperation(
                "delete is too dangerous as no where clause"
            )
        if self._limit is not None:
            ctx.literal(f" LIMIT {self._limit}")

        return ctx


class Show(Fetcher):

    __slots__ = ("_key")

    _options = {
        "create": "SHOW CREATE TABLE ",
        "columns": "SHOW FULL COLUMNS FROM ",
        "indexes": "SHOW INDEX FROM ",
    }

    def __init__(self, model: ModelType) -> None:
        super().__init__(model)
        self._key = None  # type: Optional[str]

    def __repr__(self) -> str:
        return f"<Show object for {self._model.__table__!r}>"

    __str__ = __repr__

    async def create_syntax(self) -> Optional[util.adict]:
        self._key = "create"
        return (await self.__do__(rows=1)).get("Create Table")

    async def columns(self) -> List[Any]:
        self._key = "columns"
        return await self.__do__()

    async def indexes(self) -> List[Any]:
        self._key = "indexes"
        return await self.__do__()

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        if self._key is not None:
            ctx.literal(
                self._options[self._key]
            ).sql(
                self._model.__table__
            )
        return ctx


class Create(Executor):

    __slots__ = ('_options')

    def __init__(self, model: ModelType, **options: Any) -> None:
        super().__init__(model)
        self._options = options

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.props.update({
            "scheme": self._model.__db__.url.scheme,
            "safe": self._options.get('safe', True),
            "temporary": self._options.get('temporary', False),
        })
        ctx.sql(self._model.__table__.__ddl__())
        return ctx


class Drop(Create):

    __slots__ = ()

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.literal(
            'DROP TABLE '
        ).sql(
            self._model.__table__
        )
        return ctx


class Loader:

    __slots__ = (
        '_data', '_modelclass', '_wrap',
        '_mattrs', '_mfields', '_aliases'
    )

    def __init__(
        self,
        data: Union[None, util.adict, List[util.adict]],
        model: ModelType,
        aliases: Dict[str, Any],
        wrap: bool = True
    ) -> None:
        self._data = data
        self._modelclass = model
        self._aliases = aliases
        self._wrap = wrap

        self._mattrs = self._modelclass.__attrs__
        self._mfields = self._modelclass.__table__.fields_dict

    def __call__(self) -> Any:
        if not self._data:
            return self._data

        if isinstance(self._data, list):
            if self._wrap is True:
                for i in range(len(self._data)):
                    mobj = self._to_model(self._data[i])
                    self._data[i] = mobj or self._data[i]
            else:
                for i in range(len(self._data)):
                    self._data[i] = self._convert_type(self._data[i])
        elif isinstance(self._data, dict):
            if self._wrap is True:
                self._data = self._to_model(self._data) or self._data
            else:
                self._data = self._convert_type(self._data)
        return self._data

    def _convert_type(
        self, row: util.adict
    ) -> util.adict:
        for name in row.copy():
            if name not in self._mattrs.values():
                aname = self._aliases.get(name, name)
                rname = self._mattrs.get(aname, aname)
                row[rname] = row.pop(name)
                name = rname

            f = self._mfields.get(name)
            if f and not isinstance(row[name], f.py_type):
                row[name] = f.py_value(row[name])
        return row

    def _to_model(self, row: util.adict) -> Optional[ModelBase]:
        model = self._modelclass()
        for name, value in row.items():
            name = self._aliases.get(name, name)
            name = self._mattrs.get(name)
            if not name:
                return None
            try:
                setattr(model, name, value)
            except Exception:  # pylint: disable=broad-except
                return None
        return model


class ValuesMatch(_sql.ClauseElement):

    __slots__ = ("_columns", "_params", "_values")

    def __init__(
        self, rows: Union[Dict[str, Any], List[Dict[str, Any]]]
    ) -> None:
        if isinstance(rows, dict):
            columns = list(rows.keys())
            self._values = tuple(rows.values())
        elif isinstance(rows, list):
            columns = list(rows[0].keys())
            self._values = tuple([tuple(r.values()) for r in rows])
        else:
            raise ValueError("invalid data unpack to values")

        self._columns, self._params = [], []  # type: List[_sql.ClauseElement], List[_sql.ClauseElement]
        for col in columns:
            self._columns.append(_sql.SQL(col.join("``")))
            self._params.append(_sql.SQL("%s"))

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        ctx.literal(
            ' '
        ).sql(
            _sql.EnclosedClauseElements(self._columns)
        )
        ctx.literal(
            " VALUES "
        ).sql(
            _sql.EnclosedClauseElements(self._params)
        ).values(
            self._values
        )
        return ctx


class Join(_sql.ClauseElement):

    __slots__ = ('lt', 'rt', 'join_type', '_on')

    def __init__(
        self,
        lt: types.Table,
        rt: types.Table,
        join_type: str = JOINTYPE.INNER,
        on: Optional[types.Expression] = None
    ):
        self.lt = lt
        self.rt = rt
        self.join_type = join_type
        self._on = on

    def on(self, expr: types.Expression):
        self._on = expr
        return self

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        with ctx(params=True):
            ctx.sql(
                self.lt
            ).literal(
                f' {self.join_type} JOIN '
            ).sql(
                self.rt
            )
            if self._on is not None:
                ctx.literal(' ON ').sql(self._on)
        return ctx


class AssignmentList(_sql.ClauseElement):

    __slots__ = ('_data_dict',)

    _VSM = "`{col}` = {val}"

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data_dict = data

    def __sql__(self, ctx: _sql.Context) -> _sql.Context:
        values, params = [], []
        for col, value in self._data_dict.items():
            if isinstance(value, types.Field):
                values.append(_sql.SQL(
                    self._VSM.format(
                        col=col,
                        val="{}.{}".format(
                            value.table.table_name,
                            value.column)
                    )
                ))
            elif isinstance(value, types.Expression):
                query = _sql.query(value)
                values.append(_sql.SQL(
                    self._VSM.format(
                        col=col,
                        val=query.sql[0:-1]
                    )
                ))
                params.append(query.params)
            else:
                values.append(_sql.SQL(
                    self._VSM.format(col=col, val='%s')
                ))
                params.append(value)

        ctx.sql(
            _sql.CommaClauseElements(values)  # type: ignore
        )
        if params:
            ctx.values(params)

        return ctx


class ApiProxy:

    @classmethod
    async def create_table(
        cls, m: ModelType, **options: Any
    ) -> db.ExeResult:
        if m.__name__ in _BUILTIN_MODEL_NAMES:
            raise err.NotAllowedOperation(f"{m.__name__} is built-in model name")

        return await Create(m, **options).do()

    @classmethod
    async def drop_table(
        cls, m: ModelType, **options: Any
    ) -> db.ExeResult:
        if m.__name__ in _BUILTIN_MODEL_NAMES:
            raise err.NotAllowedOperation(f"{m.__name__} is built-in model name")

        return await Drop(m, **options).do()

    @classmethod
    def show(cls, m: ModelType) -> Show:
        return Show(m)

    @classmethod
    async def get(
        cls,
        m: ModelType,
        by: Union[types.ID, types.Expression],
    ) -> Union[None, ModelBase]:
        where = by
        if not isinstance(where, types.Expression):
            where = m.__table__.primary.field == where
        return (
            await Select(
                m, [_sql.SQL("*")]
            ).where(where).get()
        )

    @classmethod
    @util.argschecker(by=(types.SEQUENCE, types.Expression))
    async def get_many(
        cls,
        m: ModelType,
        by: Union[List[types.ID], types.Expression],
        columns: Optional[List[types.Column]] = None,
    ) -> List[ModelBase]:
        where = by
        if isinstance(where, types.SEQUENCE):
            where = m.__table__.primary.field.in_(by)
        return await (
            Select(m, columns or [_sql.SQL("*")]).where(where).all()  # type: ignore
        )

    @classmethod
    @util.argschecker(row=dict, nullable=False)
    async def add(
        cls,
        m: ModelType,
        row: Dict[str, Any]
    ) -> types.ID:
        addrow = cls._gen_insert_row(m, row)
        return (
            await Insert(m, ValuesMatch(addrow)).do()
        ).last_id

    @classmethod
    @util.argschecker(rows=list, nullable=False)
    async def add_many(
        cls,
        m: ModelType,
        rows: Union[List[Dict[str, Any]], List[ModelBase]]
    ) -> int:
        addrows = []
        for row in rows:
            if isinstance(row, cls):
                addrows.append(cls._gen_insert_row(m, row.__self__))
            elif isinstance(row, dict):
                addrows.append(cls._gen_insert_row(m, row))
            else:
                raise ValueError(f"invalid data {row!r} to add")

        return (
            await Insert(m, ValuesMatch(addrows), many=True).do()
        ).affected

    @classmethod
    @util.argschecker(values=dict, nullable=False)
    async def set(
        cls,
        m: ModelType,
        id_: types.ID,
        values: Any
    ) -> int:
        table = m.__table__
        values = cls._normalize_update_values(m, values)
        return (await Update(
            m, AssignmentList(values)
        ).where(
            table.primary.field == id_
        ).do()
        ).affected

    @classmethod
    def select(
        cls, m: ModelType, *columns: types.Column
    ) -> Select:
        return Select(m, list(columns) or [_sql.SQL("*")])  # type: ignore

    @classmethod
    def insert(
        cls,
        m: ModelType,
        row: Union[Dict[str, Any], List[types.Field]],
        from_select: Optional[Select] = None
    ) -> Insert:
        if isinstance(row, dict):
            toinsert = cls._gen_insert_row(m, row.copy())
            return Insert(m, ValuesMatch(toinsert))

        if from_select is None:
            raise ValueError('`from_select` cannot be None')

        return Insert(m, row).from_(from_select)

    @classmethod
    @util.argschecker(rows=types.SEQUENCE)
    def insert_many(
        cls,
        m: ModelType,
        rows: List[Union[Dict[str, Any], Tuple[Any, ...]]],
        columns: Optional[List[types.Field]] = None
    ) -> Insert:
        normalize_rows = cls._normalize_insert_rows(m, rows, columns)
        return Insert(
            m, ValuesMatch(normalize_rows), many=True
        )

    @classmethod
    def update(cls, m: ModelType, values: Dict[str, Any]) -> Update:
        values = cls._normalize_update_values(m, values)
        return Update(m, AssignmentList(values))

    @classmethod
    def delete(cls, m: ModelType) -> Delete:
        return Delete(m)

    @classmethod
    def replace(cls, m: ModelType, row: Dict[str, Any]) -> Replace:
        toreplace = cls._gen_insert_row(m, row, for_replace=True)
        return Replace(m, ValuesMatch(toreplace))

    @classmethod
    def replace_many(
        cls,
        m: ModelType,
        rows: List[Union[Dict[str, Any], Tuple[Any, ...]]],
        columns: Optional[List[types.Field]] = None
    ) -> Replace:
        normalize_rows = cls._normalize_insert_rows(m, rows, columns, for_replace=True)
        return Replace(m, ValuesMatch(normalize_rows), many=True)

    @classmethod
    async def save(cls, mo: ModelBase) -> types.ID:
        has_id = False
        pk_attr = mo.__table__.primary.attr
        if pk_attr in mo.__self__:
            has_id = True

        row = cls._gen_insert_row(mo, mo.__self__, for_replace=has_id)
        result = await Replace(mo.__class__, ValuesMatch(row)).do()
        mo.__setattr__(pk_attr, result.last_id)
        return result.last_id

    @classmethod
    async def remove(cls, mo: ModelBase) -> int:
        table = mo.__table__
        primary_value = getattr(mo, table.primary.attr, None)
        if not primary_value:
            raise RuntimeError("remove object has no primary key value")

        ret = await Delete(
            mo.__class__
        ).where(
            table.primary.field == primary_value
        ).do()
        return ret.affected

    @classmethod
    @util.argschecker(row_data=dict, nullable=False)
    def _gen_insert_row(
        cls,
        m: ModelType,
        row_data: Dict[str, Any],
        for_replace: bool = False
    ) -> Dict[str, Any]:
        toinserts = {}
        table = m.__table__
        for name, field in table.fields_dict.items():
            # Primary key fields should not be included when not for_replace
            # if name == table.primary.attr and not for_replace:
            #     continue

            value = row_data.pop(name, None)
            # if value is None, to get default
            if value is None:
                if hasattr(field, 'default'):
                    default = field.default() if callable(field.default) else field.default
                    if isinstance(default, _sql.SQL):
                        continue
                    value = default
            if value is None and not field.null:
                if not for_replace:
                    raise ValueError(
                        f"invalid data(None) for not null attribute {name}"
                    )
            try:
                toinserts[field.name] = field.db_value(value)
            except (ValueError, TypeError):
                raise ValueError(f'invalid data({value}) for {name}')

        for attr in row_data:
            # if attr == table.primary.attr and for_replace:
            #     raise err.NotAllowedOperation(
            #         f"auto field {attr!r} not allowed to set"
            #     )
            raise ValueError(f"'{m!r}' has no attribute {attr}")

        return toinserts

    @classmethod
    def _normalize_insert_rows(
        cls,
        m: ModelType,
        rows: List[Union[Dict[str, Any], Tuple[Any, ...]]],
        columns: Optional[List[types.Field]] = None,
        for_replace: bool = False,
    ) -> List[Dict[str, Any]]:
        cleaned_rows = []  # type: List[Dict[str, Any]]

        if columns:
            if not isinstance(columns, list):
                raise ValueError("specify columns must be list")
            mattrs = m.__attrs__
            for c in columns:
                if not isinstance(c, types.Field):
                    raise TypeError(f"invalid type of columns element {c}")

                if c.name not in mattrs:
                    raise ValueError(f"{m!r} has no attribute {c.name}")
                c = mattrs[c.name]

            for row in rows:
                if not isinstance(row, types.SEQUENCE):
                    raise ValueError(f"invalid data {row!r} for specify columns")
                row = dict(zip(columns, row))  # type: ignore
                if len(row) != len(columns):
                    raise ValueError("no enough data for columns")

                cleaned_rows.append(cls._gen_insert_row(m, row, for_replace))
        else:
            cleaned_rows = [cls._gen_insert_row(m, r, for_replace) for r in rows]
        return cleaned_rows

    @classmethod
    def _normalize_update_values(
        cls, m: ModelType, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        table = m.__table__
        normalized_values = {}  # type: Dict[str, Any]
        for attr in values:
            f = table.fields_dict.get(attr)
            if f is None:
                raise ValueError(f"'{m!r}' has no attribute {attr}")
            v = values[attr]
            if not isinstance(v, _sql.ClauseElement):
                v = f.db_value(v)
            normalized_values[f.name] = v
        return normalized_values
