""" Model code """
import contextlib
import copy
from boto.dynamodb2.types import Dynamizer

from .fields import Field, NUMBER
from .model_meta import ModelMetaclass, ModelMetadata, Ordering


DYNAMIZER = Dynamizer()


class SetDelta(object):

    """ Wrapper for an atomic change to a Dynamo set """

    def __init__(self):
        self.action = None
        self.values = set()

    def merge(self, other):
        """ Merge the delta with the original set value """
        other = other or set()
        new = set()
        new.update(other)
        if self.action == 'ADD':
            new.update(self.values)
        elif other.issuperset(self.values):
            new.difference_update(self.values)
        else:
            raise KeyError("Cannot remove values that are not in the set!")

        return new

    def add(self, action, value):
        """ Add another update to the delta """
        if action not in ('ADD', 'DELETE'):
            raise ValueError("Invalid action '%s'" % action)
        if self.action is None:
            self.action = action

        if action == self.action:
            if isinstance(value, set):
                self.values.update(value)
            else:
                self.values.add(value)
        else:
            if not isinstance(value, set):
                value = set([value])
            if self.values.issuperset(value):
                self.values.difference_update(value)
            else:
                raise ValueError("Cannot ADD and REMOVE items from the same "
                                 "set in the same update")


class Model(object):

    """
    Base class for all tube models

    For documentation on the metadata fields, check the attributes on the
    :class:`.ModelMetadata` class.

    """
    __abstract__ = True
    __metaclass__ = ModelMetaclass
    __metadata_class__ = ModelMetadata
    __metadata__ = {}
    meta_ = None
    persisted_ = False
    __engine__ = None
    _overflow = None
    __dirty__ = None
    __cache__ = None
    __incrs__ = None
    _loading = False

    @classmethod
    def __on_create__(cls):
        """ Called after class is constructed but before meta_ is set """
        pass

    @classmethod
    def __after_create__(cls):
        """ Called after class is constructed and after meta_ is set """
        pass

    @classmethod
    def field_(cls, name):
        """ Construct a placeholder Field for an undeclared field """
        field = Field()
        field.name = name
        field.overflow = True
        return field

    def __new__(cls, *_, **__):
        """ Override __new__ to set default field values """
        obj = super(Model, cls).__new__(cls)
        mark_dirty = []
        with obj.loading():
            for name, field in cls.meta_.fields.iteritems():
                if not field.composite:
                    setattr(obj, name, field.default)
                    if not field.is_null(field.default):
                        mark_dirty.append(name)
        obj.__dirty__.update(mark_dirty)
        obj._overflow = {}
        obj.persisted_ = False
        return obj

    def __setattr__(self, name, value):
        if name.startswith('_') or name.endswith('_'):
            # Don't interfere with private fields
            super(Model, self).__setattr__(name, value)
            return
        if self.persisted_:
            if ((self.meta_.hash_key.name in self.meta_.related_fields[name])
                    or (self.meta_.range_key is not None and
                        self.meta_.range_key.name in
                        self.meta_.related_fields[name])):
                if value != getattr(self, name):
                    raise AttributeError(
                        "Cannot change an item's primary key!")
                else:
                    return
        self.mark_dirty_(name)
        field = self.meta_.fields.get(name)
        if field is not None:
            # Ignore if trying to set a composite field
            if not field.composite:
                if (not self._loading and self.persisted_ and
                        name not in self.__cache__):
                    for related in self.meta_.related_fields[name]:
                        cached_var = copy.copy(getattr(self, related))
                        self.__cache__[related] = cached_var
                super(Model, self).__setattr__(name, field.coerce(value))
        else:
            if (not self._loading and self.persisted_ and name not in
                    self.__cache__ and Field.is_overflow_mutable(value)):
                self.__cache__[name] = copy.copy(self.get(name))
            self._overflow[name] = value

    def __delattr__(self, name):
        if name.startswith('_') or name.endswith('_'):
            # Don't interfere with private fields
            super(Model, self).__delattr__(name)
            return
        field = self.meta_.fields.get(name)
        if field is not None:
            if not field.composite:
                super(Model, self).__delattr__(name)
        else:
            del self._overflow[name]

    def __getattribute__(self, name):
        if not name.startswith('_') and not name.endswith('_'):
            field = self.meta_.fields.get(name)
            # Intercept getattribute to construct composite fields on the fly
            if field is not None and field.composite:
                return field.resolve(self)
        return super(Model, self).__getattribute__(name)

    def __getattr__(self, name):
        try:
            return self._overflow[name]
        except KeyError:
            raise AttributeError("%s not found" % name)

    def mark_dirty_(self, name):
        """ Mark that a field is dirty """
        if self._loading or self.__dirty__ is None:
            return
        if name in self.__incrs__:
            raise ValueError("Cannot increment field '%s' and set it in "
                             "the same update!" % name)
        if name in self.meta_.fields:
            self.__dirty__.update(self.meta_.related_fields[name])
            # Never mark the primary key as dirty
            if self.meta_.hash_key.name in self.__dirty__:
                self.__dirty__.remove(self.meta_.hash_key.name)
            if (self.meta_.range_key is not None and
                    self.meta_.range_key.name in self.__dirty__):
                self.__dirty__.remove(self.meta_.range_key.name)
        else:
            self.__dirty__.add(name)

    def get(self, name, default=None):
        """ Dict-style getter for overflow attrs """
        return self._overflow.get(name, default)

    @property
    def hk_(self):
        """ The value of the hash key """
        return self.meta_.hk(self)

    @property
    def rk_(self):
        """ The value of the range key """
        return self.meta_.rk(self)

    @property
    def pk_dict_(self):
        """ The primary key dict, encoded for dynamo """
        return self.meta_.pk_dict(self, ddb_dump=True)

    def keys_(self):
        """ All declared fields and any additional fields """
        return self.meta_.fields.keys() + self._overflow.keys()

    def cached_(self, name):
        """ Get the cached (server) value of a field """
        if not self.persisted_:
            return None
        if name in self.__cache__:
            return self.__cache__[name]
        return getattr(self, name, None)

    def incr_(self, **kwargs):
        """ Atomically increment a number value """
        for key, val in kwargs.iteritems():
            if ((self.meta_.hash_key.name in self.meta_.related_fields[key])
                    or (self.meta_.range_key is not None and
                        self.meta_.range_key.name in
                        self.meta_.related_fields[key])):
                raise AttributeError("Cannot increment an item's primary key!")

            field = self.meta_.fields.get(key)
            if field is not None:
                if field.ddb_data_type != NUMBER:
                    raise ValueError("Cannot increment non-number field '%s'" %
                                     key)
                if field.composite:
                    raise ValueError("Cannot increment composite field '%s'" %
                                     key)
            if key in self.__dirty__:
                raise ValueError("Cannot set field '%s' and increment it in "
                                 "the same update!" % key)
            self.__incrs__[key] = self.__incrs__.get(key, 0) + val
            if field is not None:
                self.__incrs__[key] = field.coerce(self.__incrs__[key], True)
                for name in self.meta_.related_fields[key]:
                    self.__cache__.setdefault(name, getattr(self, name))
                    if name != key:
                        self.__dirty__.add(name)
                self.__dict__[key] = self.cached_(key) + self.__incrs__[key]
            else:
                self.__cache__.setdefault(key, getattr(self, key, 0))
                self._overflow[key] = self.cached_(key) + self.__incrs__[key]

    def add_(self, **kwargs):
        """ Atomically add to a set """
        self.mutate_('ADD', **kwargs)

    def remove_(self, **kwargs):
        """ Atomically remove from a set """
        self.mutate_('DELETE', **kwargs)

    def mutate_(self, action, **kwargs):
        """ Atomically mutate a set """
        for key, val in kwargs.iteritems():
            field = self.meta_.fields.get(key)
            if field is not None:
                if not field.is_set:
                    raise ValueError("Cannot mutate non-set field '%s'" %
                                     key)
                if field.composite:
                    raise ValueError("Cannot mutate composite field '%s'" %
                                     key)
            if key in self.__dirty__:
                raise ValueError("Cannot set field '%s' and mutate it in "
                                 "the same update!" % key)

            previous = self.__incrs__.get(key, SetDelta())
            previous.add(action, val)
            self.__incrs__[key] = previous
            if field is not None:
                for name in self.meta_.related_fields[key]:
                    self.__cache__.setdefault(name, getattr(self, name))
                    if name != key:
                        self.__dirty__.add(name)
                self.__dict__[key] = previous.merge(self.cached_(key))
            else:
                self.__cache__.setdefault(key, getattr(self, key, None))
                self._overflow[key] = previous.merge(self.cached_(key))

    def pre_save(self, engine):
        """ Called before saving items """
        self.__engine__ = engine
        for field in self.meta_.fields.itervalues():
            if field.check is not None:
                val = field.resolve(self)
                if not field.check(val):
                    raise ValueError("Validation check on field %s failed "
                                     "for value %s" % (field.name, val))

    def post_save(self):
        """ Called after item is saved to database """
        self.persisted_ = True
        self.__dirty__ = set()
        self.__incrs__ = {}
        self._reset_cache()

    def refresh(self, consistent=False):
        """ Overwrite model data with freshest from database """
        if self.__engine__ is None:
            raise ValueError("Cannot sync: No DB connection")

        self.__engine__.refresh(self, consistent=consistent)

    def sync(self, atomic=False):
        """ Sync model changes back to database """
        if self.__engine__ is None:
            raise ValueError("Cannot sync: No DB connection")

        self.__engine__.sync(self, atomic=atomic)

    def delete(self, atomic=False):
        """ Delete the model from the database """
        if self.__engine__ is None:
            raise ValueError("Cannot delete: No DB connection")
        self.__engine__.delete(self, atomic=atomic)

    def post_load(self, engine):
        """ Called after model loaded from database """
        if engine is not None:
            self.__engine__ = engine
        self.persisted_ = True
        self.__dirty__ = set()
        self.__incrs__ = {}
        self._reset_cache()

    def _reset_cache(self):
        """ Reset the __cache__ to only track mutable fields """
        self.__cache__ = {}
        for name in self.keys_():
            field = self.meta_.fields.get(name)
            if field is None:
                value = self.get(name)
                if Field.is_overflow_mutable(value):
                    self.__cache__[name] = copy.copy(value)
            elif field.is_mutable:
                self.__cache__[name] = copy.copy(getattr(self, name))

    @contextlib.contextmanager
    def loading(self, engine=None):
        """ Context manager to speed up object load process """
        self._loading = True
        self._overflow = {}
        yield
        self._loading = False
        self.post_load(engine)

    def ddb_dump_field(self, name):
        """ Dump a field to a Dynamo-friendly value """
        val = getattr(self, name)
        if name in self.meta_.fields:
            return self.meta_.fields[name].ddb_dump(val)
        else:
            return Field.ddb_dump_overflow(val)

    def ddb_dump(self):
        """ Return a dict for inserting into DynamoDB """
        data = {}
        for name in self.meta_.fields:
            data[name] = self.ddb_dump_field(name)
        for name in self._overflow:
            data[name] = self.ddb_dump_field(name)

        return data

    def set_ddb_val(self, key, val):
        """ Decode and set a value retrieved from Dynamo """
        if key.startswith('_'):
            pass
        elif key in self.meta_.fields:
            setattr(self, key, self.meta_.fields[key].ddb_load(val))
        else:
            setattr(self, key, Field.ddb_load_overflow(val))

    @classmethod
    def ddb_load(cls, engine, data):
        """ Load a model from DynamoDB data """
        obj = cls.__new__(cls)
        with obj.loading(engine):
            for key, val in data.items():
                obj.set_ddb_val(key, val)
        return obj

    def construct_ddb_expects(self):
        """ Construct a dynamo "expects" mapping based on the cached fields """
        expected = {}
        for name in self.keys_():
            cache_val = self.cached_(name)
            expect = {
                'Exists': not Field.is_null(cache_val),
            }
            field = self.meta_.fields.get(name)
            if field is not None:
                cache_val = field.ddb_dump(cache_val)
            else:
                cache_val = Field.ddb_dump_overflow(cache_val)
            if expect['Exists']:
                expect['Value'] = DYNAMIZER.encode(cache_val)
            expected[name] = expect
        return expected

    def __json__(self, request=None):
        data = {}
        for name in self.meta_.fields:
            data[name] = getattr(self, name)
        for key, val in self._overflow.iteritems():
            data[key] = val
        return data

    def __hash__(self):
        return hash(self.hk_) + hash(self.rk_)

    def __eq__(self, other):
        return (self.meta_.name == other.meta_.name and self.hk_ == other.hk_
                and self.rk_ == other.rk_)

    def __ne__(self, other):
        return not self.__eq__(other)
