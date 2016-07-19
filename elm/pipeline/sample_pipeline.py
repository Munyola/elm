import copy
import logging

import numpy as np
import xarray as xr

from elm.config import import_callable
from elm.model_selection.util import get_args_kwargs_defaults
from elm.preproc.elm_store import ElmStore
from elm.readers.util import row_col_to_xy

logger = logging.getLogger(__name__)
def check_action_data(action_data):
    '''Check that each action in action_data from all_sample_ops
    is a tuple of (func, args, kwargs)

    Params:
        action_data: list of (func, args, kwargs) tuples
    Returns True or raises ValueError

    '''
    if not isinstance(action_data, (list, tuple)):
        raise ValueError("Expected action_data to run_sample_pipeline to be a list. "
                        "Got {}".format(type(action_data)))
    for item in action_data:
        if not (isinstance(item, tuple) or len(item) == 3):
            raise ValueError('Expected each item in action_data to be a tuple of 3 items')
        func, args, kwargs = item
        func = import_callable(func)
        if not callable(func):
            raise ValueError('Expected first item in an action_data element '
                             'to be a callable, but got {}'.format(func))
        if not isinstance(args, (tuple, list)):
            raise ValueError('Expected second item in an action_data element '
                             'to be a tuple or list (args to {}). Got {}'.format(func, args))
        if not isinstance(kwargs, dict):
            raise ValueError('Expected third item in an action_data element '
                             'to be a dict (kwargs to {}).  Got {}'.format(func, kwargs))
    return True

def run_sample_pipeline(action_data, sample=None):
    '''Given action_data as a list of (func, args, kwargs) tuples,
    run each function passing args and kwargs to it
    Params:
        action_data: list from all_sample_ops typically
        sample:      None if the sample is not already taken
    '''
    check_action_data(action_data)
    if sample is None:
        samp = action_data[0]
        sampler_func_str, sampler_args, sampler_kwargs = samp
        sampler_func = import_callable(sampler_func_str, True, sampler_func_str)
        sample = sampler_func(*sampler_args, **sampler_kwargs)
    start_idx = (1 if sample is not None else 0)
    if len(action_data) > start_idx:
        for action in action_data[start_idx:]:
            func_str, args, kwargs = action
            func = import_callable(func_str, True, func_str)
            logger.debug('func {} args {} kwargs {}'.format(func, args, kwargs))
            sample = func(sample, *args, **kwargs)
    return sample


def all_sample_ops(train_or_predict_dict, config, step):
    '''Given sampling specs in a pipeline train or predict step,
    return action_data, a list of (func, args, kwargs) actions

    Params:
        train_or_predict_dict: a "train" or "predict" dict from config
        config:                full config
        step:                  a dictionary that is the current step
                               in the pipeline, like a "train" or "predict"
                               step
    '''
    d = train_or_predict_dict
    sampler_name = d.get('sampler')
    data_source = d.get('data_source')
    if sampler_name:
        sampler = config.samplers[sampler_name]
        data_generator = sampler['data_generator']
        gen = import_callable(data_generator)(**sampler)
        def sampler_func(*args, **kwargs):
            return next(gen)
        sampler_args = ()
        sampler_kwargs = {}
    else:
        data_source = config.data_sources[d['data_source']]
        sampler = config.samplers[d['data_source']]
        file_generator = config.file_generators[sampler['file_generator']]
        file_generator = import_callable(file_generator, True, file_generator)
        file_generator_kwargs = sampler.get('file_generator_kwargs') or {}
        data_source['LADSWEB_LOCAL_CACHE'] = config.LADSWEB_LOCAL_CACHE
        file_generator_kwargs['data_source'] = data_source
        included_filenames = tuple(file_generator(**file_generator_kwargs))
        sampler_func = 'elm.sample_util.samplers:random_image_selection'
        sampler_args = (data_source['band_specs'],)
        sampler_kwargs = {'included_filenames': included_filenames,
                          }
        reader = config.readers[data_source['reader']]
        load_meta = import_callable(reader['load_meta'])
        load_array = import_callable(reader['load_array'])
        selection_kwargs = sampler.get('selection_kwargs') or {}
        selection_kwargs.update({
            'data_filter': selection_kwargs.get('data_filter') or None,
            'metadata_filter': selection_kwargs.get('metadata_filter') or None,
            'filename_filter': selection_kwargs.get('filename_filter') or None,
            'geo_filters': selection_kwargs.get('geo_filters'),
            'include_polys': [config.polys[k]
                              for k in selection_kwargs.get('include_polys', [])],
            'exclude_polys': [config.polys[k]
                              for k in selection_kwargs.get('exclude_polys', [])],
            'load_meta': load_meta,
            'load_array': load_array,
        })
        sampler_kwargs.update(selection_kwargs)
    action_data = [(sampler_func, sampler_args, sampler_kwargs)]
    if 'sample_pipeline' in step:
        actions = make_sample_pipeline_func(config, step)
        action_data.extend(actions)
    return tuple(action_data)

def make_sample_pipeline_func(config, step):
    '''make list of (func, args, kwargs) tuples to run sample_pipeline
    Params:
        config: validated config from elm.config.ConfigParser
        step:   a dictionary that is one step of a "pipeline" list
    '''
    sample_pipeline = step['sample_pipeline']
    actions = []
    for action in sample_pipeline:
        if 'feature_selection' in action:
            if 'train' in step:
                key = step['train']
            elif 'predict' in step:
                key = step['predict']
            else:
                raise ValueError('Expected "feature_selection" as a '
                                 'key within a "train" or "predict" pipeline '
                                 'action ({})'.format(action))
            keep_columns = copy.deepcopy(config.train[key].get('keep_columns') or [])
            item = ('elm.sample_util.feature_selection:feature_selection_base',
                    (copy.deepcopy(config.feature_selection[action['feature_selection']]),),
                    {'keep_columns': keep_columns})
        else:
            # add items to actions of the form:
            # (
            #   module_colon_func_name_as_string,        # string
            #   args_to_func,                            # tuple
            #   kwargs_to_func                           # dict
            # )
            raise NotImplementedError('Put other sample_pipeline logic here, like resampling')
        actions.append(item)
    return actions

def final_on_sample_step(fitter,
                         model, s,
                         iter_offset,
                         fit_kwargs,
                         get_y_func=None,
                         get_y_kwargs=None,
                         get_weight_func=None,
                         get_weight_kwargs=None,
                         classes=None,
                         flatten=True,
                         flatten_y=False,
                      ):
    '''This is the final function called on a sample_pipeline
    or a simple sample that is input to training.  It ensures
    that:
       * Corresponding Y data are looked up for the X sample
       * The correct fit kwargs are passed to fit or partial_fit,
         depending on the method
    Params:
       fitter:  a model attribute like "fit" or "partial_fit"
       model:   a sklearn model like MiniBatchKmeans()
       s:       an ElmStore or xarray.Dataset with 'sample' DataArray
       fit_kwargs: kwargs to fit_func from config
       get_y_func: a function which takes an X sample DataFrame
                   and returns the corresponding Y
       get_y_kwargs: get_y_kwargs are kwargs to get_y_func from config
       get_weight_func: a function which returns sample weights for
                        an X sample
       get_weight_kwargs: keyword args needed by get_weight_func
       classes:  if using classification, all possible classes as iterable
                 or array of integers
       '''
    args, kwargs = get_args_kwargs_defaults(fitter)
    fit_kwargs = fit_kwargs or {}
    fit_kwargs = copy.deepcopy(fit_kwargs)
    if classes is not None:
        fit_kwargs['classes'] = classes
    if 'iter_offset' in kwargs:
        fit_kwargs['iter_offset'] = iter_offset
    if 'check_input' in kwargs:
        fit_kwargs['check_input'] = True
    if 'sample_weight' in kwargs and get_weight_func is not None:
        get_weight_kwargs = get_weight_kwargs or {}
        fit_kwargs['sample_weight'] = get_weight_func(s.sample.values, **get_weight_kwargs)
    if flatten:
        X = flatten_cube(s).sample.values
    if any(a.lower() == 'y' for a in args):
        Y = get_y_func(s)
        if flatten_y:
            Y = flatten_cube(Y)
        fit_args = (X, Y)
    else:
        fit_args = (X, )
    return fit_args, fit_kwargs

def flatten_cube(elm_store):
    es = elm_store['sample']
    flat = xr.DataArray(np.array(tuple(es.values[idx, :, :].ravel()
                              for idx in range(es.shape[0]))).T,
                        coords=[np.arange(np.prod(es.shape[1:])),
                                es.band.values],
                        dims=('space',
                              es.dims[0]),
                        attrs=es.attrs)
    flat_dropped = flat.dropna(dim='space')
    flat_dropped.attrs.update(flat.attrs)
    flat_dropped.attrs['dropped_points'] = flat.shape[0] - flat_dropped.shape[0]
    return ElmStore({'sample': flat_dropped}, attrs=flat_dropped.attrs)

def flattened_to_cube(flat):
    attrs = flat.attrs
    filled = np.empty((flat.band.size, attrs['Height'], attrs['Width'])) * np.NaN
    size = attrs['Height'] * attrs['Width']
    space = np.intersect1d(np.arange(size), flat.space)
    row = space // attrs['Width']
    col = space - attrs['Width'] * row
    for band in range(flat.sample.values.shape[1]):
        shp = filled[band, row, col].shape
        reshp = flat.sample.values[:, band].reshape(shp)
        filled[band, row, col] = reshp
    x, y =  row_col_to_xy(np.arange(attrs['Height']),
                  np.arange(attrs['Width']),
                  attrs['GeoTransform'])
    coords = [('band', flat.band), ('y', y), ('x', x)]
    filled = xr.DataArray(filled,
                          coords=coords,
                          dims=['band', 'y', 'x'])
    return ElmStore({'sample': filled}, attrs=attrs)

