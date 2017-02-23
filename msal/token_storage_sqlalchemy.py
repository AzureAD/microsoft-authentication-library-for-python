"""This module only serves as a demonstration of how to follow the TokenStorage
interface and implement a token storage in RDBMS, with the help of SQLAlchemy.
For production use, you will also need to add proper index into your db,
but those are out of the scope of this module.
"""
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import sessionmaker

from .token_storage import AbstractStorage


class StorageInAlchemy(AbstractStorage):
    def __init__(self, connect_string, ItemClass, *args, **kwargs):
        engine = create_engine(connect_string, *args, **kwargs)
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.ItemClass = ItemClass

    def find(self, query):  # Returns a list of rows, but they work like dict
        return self.session.query(self.ItemClass).filter_by(**query)

    def add(self, item):
        return self.session.add(self.ItemClass(**item))

    def remove(self, item):
        return self.session.delete(item)

    def commit(self):
        self.session.commit()


Base = declarative_base()


class DictionaryMixin(object):  # Let SqlAlchemy row act like a (read-only) dict
    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __contains__(self, item):
        return item in self.__dict__


class RefreshToken(Base, DictionaryMixin):
    __tablename__ = 'refresh_token'
    id = Column(Integer, primary_key=True)
    client_id = Column(String)
    authority = Column(String)
    user_id = Column(String)
    refresh_token = Column(String)
    scope = Column(String)
    create_at = Column(Integer)


class AccessToken(Base, DictionaryMixin):
    __tablename__ = 'access_token'
    id = Column(Integer, primary_key=True)
    client_id = Column(String)
    authority = Column(String)
    user_id = Column(String)
    access_token = Column(String)
    id_token = Column(String)
    scope = Column(String)
    expires_in = Column(Integer)
    ext_expires_in = Column(Integer)
    create_at = Column(Integer)

