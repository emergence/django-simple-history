from __future__ import unicode_literals

import copy
import importlib
import threading

from django.db import models, router
from django.db.models import Q
from django.db.models.fields.proxy import OrderWrt
from django.conf import settings
from django.contrib import admin
from django.utils import six
from django.utils.encoding import python_2_unicode_compatible
from django.utils.encoding import smart_text
from django.utils.timezone import now
from django.utils.translation import string_concat

try:
    from django.apps import apps
except ImportError:  # Django < 1.7
    from django.db.models import get_app
try:
    from django.db.models.fields.related import ForwardManyToOneDescriptor as ManyToOneDescriptor
except ImportError:  # Django < 1.9
    from django.db.models.fields.related import ReverseSingleRelatedObjectDescriptor as ManyToOneDescriptor
try:
    from south.modelsinspector import add_introspection_rules
except ImportError:  # south not present
    pass
else:  # south configuration for CustomForeignKeyField
    add_introspection_rules(
        [], ["^simple_history.models.CustomForeignKeyField"])

from . import exceptions
from simple_history import register
from .manager import HistoryDescriptor

ALL_M2M_FIELDS = object()

registered_models = {}


def not_registered(model):
    if model._meta.proxy:
        return '%s%s' % (model._meta.db_table, model.__name__) not in registered_models
    return model._meta.db_table not in registered_models


class HistoricalRecords(object):
    thread = threading.local()

    def __init__(self, verbose_name=None, bases=(models.Model,),
                 user_related_name='+', table_name=None, inherit=False, m2m_fields=None):
        self.user_set_verbose_name = verbose_name
        self.user_related_name = user_related_name
        self.table_name = table_name
        self.inherit = inherit
        self.m2m_fields = m2m_fields
        try:
            if isinstance(bases, six.string_types):
                raise TypeError
            self.bases = tuple(bases)
        except TypeError:
            raise TypeError("The `bases` option must be a list or a tuple.")

    def contribute_to_class(self, cls, name):
        self.manager_name = name
        self.module = cls.__module__
        self.cls = cls
        models.signals.class_prepared.connect(self.finalize, weak=False)
        self.add_extra_methods(cls)
        self.setup_m2m_history(cls)

    def add_extra_methods(self, cls):
        def save_without_historical_record(self, *args, **kwargs):
            """
            Save model without saving a historical record

            Make sure you know what you're doing before you use this method.
            """
            self.skip_history_when_saving = True
            try:
                ret = self.save(*args, **kwargs)
            finally:
                del self.skip_history_when_saving
            return ret
        setattr(cls, 'save_without_historical_record',
                save_without_historical_record)

    def setup_m2m_history(self, cls):
        m2m_history_fields = self.m2m_fields
        if m2m_history_fields is ALL_M2M_FIELDS:
            for field in cls._meta.many_to_many:
                field = getattr(cls, field.name).field
                assert isinstance(field, models.fields.related.ManyToManyField), \
                    ('%s must be a ManyToManyField' % field.name)
                if not sum([
                        isinstance(item, HistoricalRecords) for item in field.remote_field.through.__dict__.values()
                ]):
                    through_model = field.remote_field.through
                    if through_model._meta.auto_created and not_registered(through_model):
                        through_model.history = HistoricalRecords()
                        register(through_model)
        elif m2m_history_fields:
            assert (isinstance(m2m_history_fields, list) or isinstance(m2m_history_fields, tuple)), \
                'm2m_history_fields must be a list or tuple'
            for field_name in m2m_history_fields:
                field = getattr(cls, field_name).field
                assert isinstance(field, models.fields.related.ManyToManyField), \
                    ('%s must be a ManyToManyField' % field_name)
                if not sum([
                        isinstance(item, HistoricalRecords) for item in field.remote_field.through.__dict__.values()
                ]):
                    through_model = field.remote_field.through
                    if through_model._meta.auto_created and not_registered(through_model):
                        through_model.history = HistoricalRecords()
                        register(through_model)

    def finalize(self, sender, **kwargs):
        try:
            hint_class = self.cls
        except AttributeError:  # called via `register`
            pass
        else:
            if hint_class is not sender:  # set in concrete
                if not (self.inherit and issubclass(sender, hint_class)):
                    return  # set in abstract
        if hasattr(sender._meta, 'simple_history_manager_attribute'):
            raise exceptions.MultipleRegistrationsError(
                '{}.{} registered multiple times for history tracking.'.format(
                    sender._meta.app_label,
                    sender._meta.object_name,
                )
            )
        if sender._meta.proxy:
            original_class = [base_class for base_class in sender.__bases__ if base_class._meta.abstract is False][0]
            # Parent model must be registered before the proxy model is
            if not_registered(original_class):
                # Ignore the `app` kwarg, since the proxy model may be in a different app than the original model
                register_kwargs = {
                    'manager_name': self.manager_name,
                    'records_class': self.__class__,
                    'verbose_name': self.user_set_verbose_name,
                    'bases': self.bases,
                    'user_related_name': self.user_related_name,
                    'm2m_fields': self.m2m_fields,
                }
                register(original_class, **register_kwargs)
            # Proxy models use their parent's history model
            history_model = getattr(sender, self.manager_name).model
        else:
            history_model = self.create_history_model(sender)
            module = importlib.import_module(self.module)
            setattr(module, history_model.__name__, history_model)
        # The HistoricalRecords object will be discarded,
        # so the signal handlers can't use weak references.
        models.signals.post_save.connect(self.post_save, sender=sender,
                                         weak=False)
        models.signals.pre_delete.connect(self.pre_delete, sender=sender,
                                          weak=False)
        models.signals.post_delete.connect(self.post_delete, sender=sender,
                                           weak=False)
        models.signals.m2m_changed.connect(self.m2m_changed, sender=sender, weak=False)

        descriptor = HistoryDescriptor(history_model)
        setattr(sender, self.manager_name, descriptor)
        sender._meta.simple_history_manager_attribute = self.manager_name

    def create_history_model(self, model):
        """
        Creates a historical model to associate with the model provided.
        """
        attrs = {'__module__': self.module}

        app_module = '%s.models' % model._meta.app_label
        if model.__module__ != self.module:
            # registered under different app
            attrs['__module__'] = self.module
        elif app_module != self.module:
            try:
                # Abuse an internal API because the app registry is loading.
                app = apps.app_configs[model._meta.app_label]
            except NameError:  # Django < 1.7
                models_module = get_app(model._meta.app_label).__name__
            else:
                models_module = app.name
            attrs['__module__'] = models_module

        fields = self.copy_fields(model)
        attrs.update(fields)
        attrs.update(self.get_extra_fields(model, fields))
        # type in python2 wants str as a first argument
        attrs.update(Meta=type(str('Meta'), (), self.get_meta_options(model)))
        if self.table_name is not None:
            attrs['Meta'].db_table = self.table_name
        name = 'Historical%s' % model._meta.object_name
        registered_models[model._meta.db_table] = model
        return python_2_unicode_compatible(
            type(str(name), self.bases, attrs))

    def copy_fields(self, model):
        """
        Creates copies of the model's original fields, returning
        a dictionary mapping field name to copied field object.
        """
        fields = {}
        for field in model._meta.fields:
            field = copy.copy(field)
            try:
                field.remote_field = copy.copy(field.remote_field)
            except AttributeError:
                field.remote_field = copy.copy(field.remote_field)
            if isinstance(field, OrderWrt):
                # OrderWrt is a proxy field, switch to a plain IntegerField
                field.__class__ = models.IntegerField
            if isinstance(field, models.ForeignKey):
                old_field = field
                field_arguments = {'db_constraint': False}
                if (getattr(old_field, 'one_to_one', False) or
                        isinstance(old_field, models.OneToOneField)):
                    FieldType = models.ForeignKey
                else:
                    FieldType = type(old_field)
                if getattr(old_field, 'to_fields', []):
                    field_arguments['to_field'] = old_field.to_fields[0]
                if getattr(old_field, 'db_column', None):
                    field_arguments['db_column'] = old_field.db_column
                field = FieldType(
                    old_field.remote_field.model,
                    related_name='+',
                    null=True,
                    blank=True,
                    primary_key=False,
                    db_index=True,
                    serialize=True,
                    unique=False,
                    on_delete=models.DO_NOTHING,
                    **field_arguments
                )
                field.name = old_field.name
            else:
                transform_field(field)
            fields[field.name] = field
        return fields

    def get_extra_fields(self, model, fields):
        """Return dict of extra fields added to the historical record model"""

        user_model = getattr(settings, 'AUTH_USER_MODEL', 'auth.User')

        @models.permalink
        def revert_url(self):
            """URL for this change in the default admin site."""
            opts = model._meta
            app_label, model_name = opts.app_label, opts.model_name
            return ('%s:%s_%s_simple_history' %
                    (admin.site.name, app_label, model_name),
                    [getattr(self, opts.pk.attname), self.history_id])

        def get_instance(self):
            return model(**{
                field.attname: getattr(self, field.attname)
                for field in fields.values()
            })

        return {
            'history_id': models.AutoField(primary_key=True),
            'history_date': models.DateTimeField(),
            'history_user': models.ForeignKey(
                user_model, null=True, related_name=self.user_related_name,
                on_delete=models.SET_NULL),
            'history_type': models.CharField(max_length=1, choices=(
                ('+', 'Created'),
                ('~', 'Changed'),
                ('-', 'Deleted'),
            )),
            'history_object': HistoricalObjectDescriptor(model),
            'instance': property(get_instance),
            'instance_type': model,
            'revert_url': revert_url,
            '__str__': lambda self: '%s as of %s' % (self.history_object,
                                                     self.history_date)
        }

    def get_meta_options(self, model):
        """
        Returns a dictionary of fields that will be added to
        the Meta inner class of the historical record model.
        """
        meta_fields = {
            'ordering': ('-history_date', '-history_id'),
            'get_latest_by': 'history_date',
        }
        if self.user_set_verbose_name:
            name = self.user_set_verbose_name
        else:
            name = string_concat('historical ',
                                 smart_text(model._meta.verbose_name))
        meta_fields['verbose_name'] = name
        return meta_fields

    def post_save(self, instance, created, **kwargs):
        if not created and hasattr(instance, 'skip_history_when_saving'):
            return
        if not kwargs.get('raw', False):
            self.create_historical_record(instance, created and '+' or '~')

    def pre_delete(self, instance, **kwargs):
        """
        Creates deletion records for the through model of m2m fields. Also creates change records for objects on the
        other side of the m2m relationship.
        """
        for m2m_field in instance._meta.many_to_many:
            through_model = m2m_field.remote_field.through
            if hasattr(through_model._meta, 'simple_history_manager_attribute'):
                items = through_model.objects.filter(Q(**{m2m_field.m2m_column_name(): instance.pk}))
                for item in items:
                    self.create_historical_record(item, '-')
                for related in m2m_field.value_from_object(instance):
                    self.create_historical_record(related, '~')

    def post_delete(self, instance, **kwargs):
        self.create_historical_record(instance, '-')

    def m2m_changed(self, action, instance, sender, **kwargs):
        source_field_name, target_field_name = None, None
        for field_name, field_value in sender.__dict__.items():
            if isinstance(field_value, ManyToOneDescriptor):
                if field_value.field.remote_field.model == kwargs['model']:
                    target_field_name = field_name
                elif isinstance(instance, field_value.field.remote_field.model):
                    source_field_name = field_name
        items = sender.objects.filter(**{source_field_name: instance})
        if kwargs['pk_set']:
            items = items.filter(**{target_field_name + '__id__in': kwargs['pk_set']})
        for item in items:
            if action == 'post_add':
                if hasattr(item, 'skip_history_when_saving'):
                    continue
                self.create_historical_record(item, '+')
            elif action == 'pre_remove':
                self.create_historical_record(item, '-')
            elif action == 'pre_clear':
                self.create_historical_record(item, '-')
        if action == 'pre_clear':
            setattr(instance, '__pre_clear_items', items)
        elif action == 'post_add' and hasattr(instance, '__pre_clear_items'):
            other_items = getattr(instance, '__pre_clear_items')
            for item in other_items:
                target = getattr(item, target_field_name)
                if has_m2m_field(target, sender) and not [i for i in items if target == getattr(i, target_field_name)]:
                    self.create_historical_record(target, '~')
            for item in items:
                target = getattr(item, target_field_name)
                if has_m2m_field(target, sender) and not [
                    i for i in other_items if target == getattr(i, target_field_name)
                ]:
                    self.create_historical_record(target, '~')
            delattr(instance, '__pre_clear_items')

    def create_historical_record(self, instance, history_type):
        history_date = getattr(instance, '_history_date', now())
        history_user = self.get_history_user(instance)
        manager = getattr(instance, self.manager_name)
        attrs = {}
        for field in instance._meta.fields:
            attrs[field.attname] = getattr(instance, field.attname)
        manager.create(history_date=history_date, history_type=history_type,
                       history_user=history_user, **attrs)

    def get_history_user(self, instance):
        """Get the modifying user from instance or middleware."""
        try:
            return instance._history_user
        except AttributeError:
            try:
                if self.thread.request.user.is_authenticated:
                    return self.thread.request.user
                return None
            except AttributeError:
                return None


def transform_field(field):
    """Customize field appropriately for use in historical model"""
    field.name = field.attname
    if isinstance(field, models.AutoField):
        field.__class__ = convert_auto_field(field)

    elif isinstance(field, models.FileField):
        # Don't copy file, just path.
        field.__class__ = models.TextField

    # Historical instance shouldn't change create/update timestamps
    field.auto_now = False
    field.auto_now_add = False

    if field.primary_key or field.unique:
        # Unique fields can no longer be guaranteed unique,
        # but they should still be indexed for faster lookups.
        field.primary_key = False
        field._unique = False
        field.db_index = True
        field.serialize = True


def has_m2m_field(instance, through):
    for m2m_field in instance._meta.many_to_many:
        if through is m2m_field.remote_field.through:
            return True
    return False


def convert_auto_field(field):
    """Convert AutoField to a non-incrementing type

    The historical model gets its own AutoField, so any existing one
    must be replaced with an IntegerField.
    """
    connection = router.db_for_write(field.model)
    if settings.DATABASES[connection]['ENGINE'] in ('django_mongodb_engine',):
        # Check if AutoField is string for django-non-rel support
        return models.TextField
    return models.IntegerField


class HistoricalObjectDescriptor(object):
    def __init__(self, model):
        self.model = model

    def __get__(self, instance, owner):
        values = (getattr(instance, f.attname)
                  for f in self.model._meta.fields)
        return self.model(*values)
