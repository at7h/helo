"""
    helo.model
    ~~~~~~~~~~

    Implements the model.
"""
from __future__ import annotations

import warnings
import re
from copy import deepcopy
from typing import Any, Dict, Optional, List, Union, Tuple, Type

from helo import db
from helo.types import _abc as types
from helo.model.core import (
    ModelType, ModelBase, ApiProxy, Select, Update,
    Replace, Delete, Insert,
    JOINTYPE, ROWTYPE,
)
from helo._helper import with_metaclass


class Model(with_metaclass(ModelType, ModelBase)):  # type
    """From Model defining your model is easy
    >>> import helo
    >>>
    >>> db = helo.Helo()
    >>>
    >>> class User(db.Model):
    ...     id = helo.Auto()
    ...     nickname = helo.VarChar(length=45)
    ...     password = helo.VarChar(length=100)
    """

    @classmethod
    async def create(cls, **options: Any) -> db.ExeResult:
        """Create a table in the database from the model"""

        return await ApiProxy.create_table(cls, **options)

    @classmethod
    async def drop(cls, **options: Any) -> db.ExeResult:
        """Drop a table in the database from the model"""

        return await ApiProxy.drop_table(cls, **options)

    @classmethod
    def show(cls) -> Show:
        """Show information about table"""

        return ApiProxy.show(cls)

    #
    # Simple API for short
    #
    @classmethod
    async def get(
        cls,
        by: Union[types.ID, types.Expression]
    ) -> Union[None, Model]:
        """Getting a row by the primary key
        or simple query expression

        >>> user = await User.get(1)
        >>> user
        <User objetc> at 1
        >>> user.nickname
        'at7h'
        """

        if not by:
            return None
        return await ApiProxy.get(cls, by)

    @classmethod
    async def mget(
        cls,
        by: Union[List[types.ID], types.Expression],
        columns: Optional[List[types.Column]] = None,
    ) -> List[Model]:
        """Getting rows by the primary key list
        or simple query expression

        >>> await User.mget([1, 2, 3])
        [<User object 1>, <User object 2>, <User object 3>]
        """

        if not by:
            raise ValueError("no condition to mget")
        return await ApiProxy.get_many(cls, by, columns=columns)

    @classmethod
    async def add(
        cls,
        __row: Optional[Dict[str, Any]] = None,
        **values: Any
    ) -> types.ID:
        """Adding a row, simple and shortcut of ``insert``

        # Using keyword arguments:
        >>> await User.add(nickname='at7h', password='7777')
        1

        # Using values dict:
        >>> await User.add({'nickname': 'at7h', 'password': '777'})
        1
        """

        row = __row or values
        if not row:
            raise ValueError("no data to add")
        return await ApiProxy.add(cls, row)

    @classmethod
    async def madd(
        cls,
        rows: Union[List[Dict[str, Any]], List[Model]]
    ) -> int:
        """Adding multiple, simple and shortcut of ``minsert``

        # Using values dict list:
        >>> users = [
        ...    {'nickname': 'at7h', 'password': '777'}
        ...    {'nickname': 'mebo', 'password': '666'}]
        >>> await User.madd(users)
        2

        # Adding User object list:
        >>> users = [User(**u) for u in users]
        >>> await User.madd(users)
        2
        """

        if not rows:
            raise ValueError("no data to madd")
        return await ApiProxy.add_many(cls, rows)

    @classmethod
    async def set(cls, _id: types.ID, **values: Any) -> int:
        """Setting the value of a row with the primary key

        >>> user = await User.get(1)
        >>> user.password
        777
        >>> await User.set(1, password='888')
        1
        >>> user = await User.get(1)
        >>> user.password
        888
        """

        if not values:
            raise ValueError('no _id or values to set')
        return await ApiProxy.set(cls, _id, values)

    # API that translates directly from SQL statements(DQL, DML).
    # You have to explicitly execute them via methods like `do()`.
    @classmethod
    def select(cls, *columns: types.Column) -> Select:
        """Select Query, see ``Select``"""

        return ApiProxy.select(cls, *columns)

    @classmethod
    def insert(
        cls, __row: Optional[Dict[str, Any]] = None, **values: Any
    ) -> Insert:
        """Inserting a row

        # Using keyword arguments:
        >>> await User.insert(nickname='at7h', password='777').do()
        ExeResult(affected: 1, last_id: 1)

        # Using values dict list:
        >>> await User.insert({
        ...     'nickname': 'at7h',
        ...     'password': '777',
        ... }).do()
        ExeResult(affected: 1, last_id: 1)
        """

        row = __row or values
        if not row:
            raise ValueError("no data to insert")
        return ApiProxy.insert(cls, row)

    @classmethod
    def minsert(
        cls,
        rows: List[Union[Dict[str, Any], Tuple[Any, ...]]],
        columns: Optional[List[types.Field]] = None
    ) -> Insert:
        """Inserting multiple

        # Using values dict list:
        >>> users = [
        ...    {'nickname': 'Bob', 'password': '666'},
        ...    {'nickname': 'Her', 'password: '777'},
        ...    {'nickname': 'Nug', 'password': '888'}]

        >>> result = await User.insert(users).do()

        # We can also specify row tuples
        # columns the tuple values correspond to:
        >>> users = [
        ...    ('Bob', '666'),
        ...    ('Her', '777'),
        ...    ('Nug', '888')]
        >>> result = await User.insert(
        ...    users, columns=[User.nickname, User.password]
        ... ).do()
        """

        if not rows:
            raise ValueError("no data to minsert {}")
        return ApiProxy.insert_many(cls, rows, columns=columns)

    @classmethod
    def insert_from(
        cls, from_: Select, columns: List[types.Column]
    ) -> Insert:
        """Inserting from select clause

        >>> select = Employee.Select(
        ...     Employee.id, Employee.name
        ... ).where(Employee.id < 10)
        >>>
        >>> User.insert_from(select, [User.id, User.name]).do()
        """

        if not columns:
            raise ValueError("insert_from must specify columns")
        return ApiProxy.insert(cls, list(columns), from_select=from_)

    @classmethod
    def update(cls, **values: Any) -> Update:
        """Updating record

        >>> await User.update(
        ...    password='888').where(User.id == 1
        ... ).do()
        ExeResult(affected: 1, last_id: 0)
        """
        if not values:
            raise ValueError("no data to update")
        return ApiProxy.update(cls, values)

    @classmethod
    def delete(cls) -> Delete:
        """Deleting record

        >>> await User.delete().where(User.id == 1).do()
        ExeResult(affected: 1, last_id: 0)
        """
        return ApiProxy.delete(cls)

    @classmethod
    def replace(
        cls, __row: Optional[Dict[str, Any]] = None, **values: Any
    ) -> Replace:
        """MySQL REPLACE, similar to ``insert``"""

        row = __row or values
        if not row:
            raise ValueError("no data to replace")
        return ApiProxy.replace(cls, row)

    @classmethod
    def mreplace(
        cls,
        rows: List[Union[Dict[str, Any], Tuple[Any, ...]]],
        columns: Optional[List[types.Field]] = None
    ) -> Replace:
        """MySQL REPLACE, similar to ``minsert``"""

        if not rows:
            raise ValueError("no data to mreplace")
        return ApiProxy.replace_many(cls, rows, columns=columns)

    # instance

    async def save(self) -> types.ID:
        """Write objects in memory to database

        >>> user = User(nickname='at7h',password='777')
        >>> await user.save()
        1
        """
        return await ApiProxy.save(self)

    async def remove(self) -> int:
        """Removing a row

        >>> user = await User.get(1)
        >>> await user.remove()
        1
        >>> await User.get(1)
        None
        """
        return await ApiProxy.remove(self)


def new() -> Type[Model]:
    class Model1(Model):
        __db__ = None  # type: db.Database

    return Model1
