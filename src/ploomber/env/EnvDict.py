from copy import copy, deepcopy
import importlib
import platform
from pathlib import Path
from collections.abc import Mapping
from reprlib import Repr

import yaml

from ploomber.env import validate
from ploomber.env.expand import EnvironmentExpander
from ploomber.env.FrozenJSON import FrozenJSON
from ploomber.util import default


# TODO: custom expanders, this could be done trough another special directive
# such as _expander_class to know which class to use
class EnvDict(Mapping):
    """
    Implements the initialization functionality for Env, except it allows
    to more than one instance to exist, this is used internally to allow
    factory functions introspection without having to create an actual Env

    Parameters
    ----------
    source : dict or str
        If str, it will be interpreted as a path to a YAML file

    path_to_here : str or pathlib.Path
        Value used to expand the {{here}} placeholder. If None, it uses the
        location of the YAML spec. If initialized with a dict and None,
        the {{here}} placeholder is not available.


    Notes
    -----
    By default, it includes the following placeholders (unless the passed
    dictionary already contains those keys): {{user}} (current user)
    {{cwd}} (working directory), {{here}} (env.yaml location, if any), {{root}}
    (project's root folder, if any)
    """
    def __init__(self, source, path_to_here=None):
        # if initialized from another EnvDict, copy the attributes to
        # initialize
        # this happens in the  CLI parser, which instanttiates the env
        # because it needs to create one and then replace cli args, then
        # passes this modified object to DAGSpec
        if isinstance(source, EnvDict):
            for attr in (
                    '_path_to_env',
                    '_preprocessed',
                    '_expander',
                    '_data',
                    '_repr',
            ):
                original = getattr(source, attr)
                setattr(self, attr, deepcopy(original))
        else:
            (
                # load data
                raw_data,
                # this will be None if source is a dict
                self._path_to_env) = load_from_source(source)

            # add default placeholders but override them if they are defined
            # in the raw data
            raw_data = {
                **self._default_dict(include_here=path_to_here is not None),
                **raw_data,
            }

            # check raw data is ok
            validate.raw_data_keys(raw_data)

            # expand _module special key, return its expanded value
            self._preprocessed = raw_preprocess(raw_data, self._path_to_env)

            # initialize expander, which converts placeholders to their values
            # we need to pass path_to_env since the {{here}} placeholder
            # resolves
            # to its parent
            if path_to_here is None:
                # if no pat_to_here, use path_to_end
                path_to_here = (None if self._path_to_env is None else Path(
                    self._path_to_env).parent)
            else:
                path_to_here = Path(path_to_here).resolve()

            self._expander = EnvironmentExpander(self._preprocessed,
                                                 path_to_here=path_to_here)
            # now expand all values
            self._data = self._expander.expand_raw_dictionary(raw_data)

            self._repr = Repr()

    @staticmethod
    def _default_dict(include_here):
        placeholders = {
            'user': '{{user}}',
            'cwd': '{{cwd}}',
        }

        if default.find_root_recursively() is not None:
            placeholders['root'] = '{{root}}'

        if include_here:
            placeholders['here'] = '{{here}}'

        return placeholders

    @classmethod
    def default(cls, path_to_here=None):
        """
        Returns and EnvDict with built-in placeholders
        Always available: {{user}}, {{cwd}}
        {{root}} available if a parent path has a setup.py
        {{here}} available is path_to_here is not None
        """
        placeholders = cls._default_dict(include_here=path_to_here is not None)
        return cls(placeholders, path_to_here=path_to_here)

    @property
    def path_to_env(self):
        return self._path_to_env

    def __getattr__(self, key):
        error = AttributeError("'{}' object has no attribute '{}'".format(
            type(self).__name__, key))
        # do not look up special atttributes this way!
        if key.startswith('__') and key.endswith('__'):
            raise error

        if key in self:
            return self[key]
        else:
            raise AttributeError("{} object has no atttribute '{}'".format(
                repr(self), key))

    def __getitem__(self, key):
        try:
            return self._getitem(key)
        except KeyError as e:
            # custom error will be displayed around quotes, but it's fine.
            # this is due to the KeyError.__str__ implementation
            msg = "{} object has no key '{}'".format(repr(self), key)
            e.args = (msg, )
            raise

    def _getitem(self, key):
        if key in self._preprocessed:
            return FrozenJSON(self._preprocessed[key])
        else:
            return FrozenJSON(self._data[key])

    def __setitem__(self, key, value):
        self._data[key] = value

    def __iter__(self):
        for k in self._data:
            yield k

    def __len__(self):
        return len(self._data)

    def __str__(self):
        return str(self._data)

    def __repr__(self):
        content = self._repr.repr_dict(self._data, level=2)
        return f'{type(self).__name__}({content})'

    def _replace_value(self, value, keys_all):
        """
        Replace a value in the underlying dictionary, by passing a value and
        a list of keys

        e.g. given {'a': {'b': 1}}, we can replace 1 by doing
        _replace_value(2, ['a', 'b'])
        """
        keys_to_final_dict = keys_all[:-1]
        key_to_edit = keys_all[-1]

        dict_to_edit = self._data

        for e in keys_to_final_dict:
            dict_to_edit = dict_to_edit[e]

        if dict_to_edit.get(key_to_edit) is None:
            dotted_path = '.'.join(keys_all)
            raise KeyError('Trying to replace key "{}" in env, '
                           'but it does not exist'.format(dotted_path))

        dict_to_edit[key_to_edit] = (self._expander.expand_raw_value(
            value, keys_all))

    def _inplace_replace_flatten_key(self, value, key_flatten):
        """
        Replace a value in the underlying dictionary, by passing a value and
        a list of keys

        e.g. given {'a': {'b': 1}}, we can replace 1 by doing
        _replace_flatten_keys(2, 'env__a__b'). This function is used
        internally to overrive env values when calling factories (functions
        decorated with @with_env or when doing so via the command line
        interface - ploomber build pipeline.yaml --env--a--b 2)

        Returns a copy
        """
        # convert env__a__b__c -> ['a', 'b', 'c']
        parts = key_flatten.split('__')

        if parts[0] != 'env':
            raise ValueError('keys_flatten must start with env__')

        keys_all = parts[1:]
        self._replace_value(value, keys_all)

    def _replace_flatten_key(self, value, key_flatten):
        obj = copy(self)
        obj._inplace_replace_flatten_key(value, key_flatten)
        return obj

    def _inplace_replace_flatten_keys(self, to_replace):
        """Replace multiple keys at once

        Returns a copy
        """
        for key, value in to_replace.items():
            self._inplace_replace_flatten_key(value, key)

    def _replace_flatten_keys(self, to_replace):
        obj = copy(self)
        obj._inplace_replace_flatten_keys(to_replace)
        return obj


def load_from_source(source):
    """
    Loads from a dictionary or a YAML and applies preprocesssing to the
    dictionary

    Returns
    -------
    dict
        Raw dictioanry
    pathlib.Path
        Path to the loaded file, None if source is a dict
    str
        Name, if loaded from a YAML file with the env.{name}.yaml format,
        None if another format or if source is a dict
    """
    if isinstance(source, Mapping):
        # dictiionary, path
        return source, None

    elif source is None:
        # look for an env.{name}.yaml, if that fails, try env.yaml
        name = platform.node()
        path_found = find_env_w_name(name)

        if path_found is None:
            raise FileNotFoundError(
                'Tried to initialize environment with '
                'None, but automatic '
                'file search failed to locate '
                'env.{}.yaml nor env.yaml in the '
                'current directory nor 6 levels up'.format(name))
        else:
            source = path_found

    elif isinstance(source, (str, Path)):
        # if not pointing to a file, try to locate it...
        if not Path(source).exists():
            source_found = default.find_file_recursively(source)

            if source_found is None:
                raise FileNotFoundError('Could not find file "{}" in the '
                                        'current working directory nor '
                                        '6 levels up'.format(source))
            else:
                source = source_found

    with open(str(source)) as f:
        try:
            raw = yaml.load(f, Loader=yaml.SafeLoader)
        except Exception as e:
            raise type(e)('yaml.load failed to parse your YAML file '
                          'fix syntax errors and try again') from e
        finally:
            # yaml.load returns None for empty files and str if file just
            # contains a string - those aren't valid for our use case, raise
            # an error
            if not isinstance(raw, Mapping):
                raise ValueError("Expected object loaded from '{}' to be "
                                 "a dict but got '{}' instead, "
                                 "verify the content".format(
                                     source,
                                     type(raw).__name__))

    path = Path(source).resolve()

    return raw, path


def raw_preprocess(raw, path_to_raw):
    """
    Preprocess a raw dictionary. If a '_module' key exists, it
    will be expanded: first, try to locate a module with that name and resolve
    to their location (root __init__.py parent), if no module is found,
    interpret as a path to the project's root folder, checks that the folder
    actually exists. '{{here}}' is also allowed, which resolves to the
    path_to_raw, raises Exception if path_to_raw is None

    Returns
    -------
    preprocessed : dict
        Dict with preprocessed keys (empty dictionary it no special
        keys exist in raw)

    Parameters
    ----------
    raw : dict
        Raw data dictionary
    path_to_raw : str
        Path to file where dict was read from, it read from a dict, pass None
    """
    module = raw.get('_module')
    preprocessed = {}

    if module:

        if raw['_module'] == '{{here}}':

            if path_to_raw is not None:
                preprocessed['_module'] = path_to_raw.parent
            else:
                raise ValueError('_module cannot be {{here}} if '
                                 'not loaded from a file')
        else:
            # check if it's a filesystem path
            as_path = Path(module)

            if as_path.exists():
                if as_path.is_file():
                    raise ValueError(
                        'Could not resolve _module "{}", '
                        'expected a module or a directory but got a '
                        'file'.format(module))
                else:
                    path_to_module = as_path

            # must be a dotted path
            else:
                module_spec = importlib.util.find_spec(module)

                # package does not exist
                if module_spec is None:
                    raise ValueError('Could not resolve _module "{}", '
                                     'it is not a valid module '
                                     'nor a directory'.format(module))
                else:
                    path_to_module = Path(module_spec.origin).parent

            preprocessed['_module'] = path_to_module

    return preprocessed


def find_env_w_name(name):
    """
    Find environment named 'env.{name}.yaml' by looking into the current
    directory and upper folders. If this fails, attempt to do the same
    for a file named just 'env.yaml'

    Returns
    -------
    path_to_env : pathlib.Path
        Path to environment file, None if no file could be found
    """
    path = default.find_file_recursively(name='env.{}.yaml'.format(name))

    if path is None:
        return default.find_file_recursively(name='env.yaml')
    else:
        return path
