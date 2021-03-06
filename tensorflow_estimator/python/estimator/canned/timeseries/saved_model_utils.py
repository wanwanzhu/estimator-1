# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Convenience functions for working with time series saved_models.

@@predict_continuation
@@cold_start_filter
@@filter_continuation
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy

from tensorflow.python.util.all_util import remove_undocumented
from tensorflow_estimator.python.estimator.canned.timeseries import feature_keys as _feature_keys
from tensorflow_estimator.python.estimator.canned.timeseries import head as _head
from tensorflow_estimator.python.estimator.canned.timeseries import model_utils as _model_utils


def _canonicalize_numpy_data(data, require_single_batch):
  """Do basic checking and reshaping for Numpy data.

  Args:
    data: A dictionary mapping keys to Numpy arrays, with several possible
      shapes (requires keys `TrainEvalFeatures.TIMES` and
      `TrainEvalFeatures.VALUES`): Single example; `TIMES` is a scalar and
        `VALUES` is either a scalar or a vector of length [number of features].
        Sequence; `TIMES` is a vector of shape [series length], `VALUES` either
        has shape [series length] (univariate) or [series length x number of
        features] (multivariate). Batch of sequences; `TIMES` is a vector of
        shape [batch size x series length], `VALUES` has shape [batch size x
        series length] or [batch size x series length x number of features]. In
        any case, `VALUES` and any exogenous features must have their shapes
        prefixed by the shape of the value corresponding to the `TIMES` key.
    require_single_batch: If True, raises an error if the provided data has a
      batch dimension > 1.

  Returns:
    A dictionary with features normalized to have shapes prefixed with [batch
    size x series length]. The sizes of dimensions which were omitted in the
    inputs are 1.
  Raises:
    ValueError: If dimensions are incorrect or do not match, or required
      features are missing.
  """
  features = {key: numpy.array(value) for key, value in data.items()}
  if (_feature_keys.TrainEvalFeatures.TIMES not in features or
      _feature_keys.TrainEvalFeatures.VALUES not in features):
    raise ValueError("{} and {} are required features.".format(
        _feature_keys.TrainEvalFeatures.TIMES,
        _feature_keys.TrainEvalFeatures.VALUES))
  times = features[_feature_keys.TrainEvalFeatures.TIMES]
  for key, value in features.items():
    if value.shape[:len(times.shape)] != times.shape:
      raise ValueError(
          ("All features must have their shapes prefixed by the shape of the"
           " times feature. Got shape {} for feature '{}', but shape {} for"
           " '{}'").format(value.shape, key, times.shape,
                           _feature_keys.TrainEvalFeatures.TIMES))
  if not times.shape:  # a single example
    if not features[_feature_keys.TrainEvalFeatures.VALUES].shape:  # univariate
      # Add a feature dimension (with one feature)
      features[_feature_keys.TrainEvalFeatures.VALUES] = features[
          _feature_keys.TrainEvalFeatures.VALUES][..., None]
    elif len(features[_feature_keys.TrainEvalFeatures.VALUES].shape) > 1:
      raise ValueError(
          ("Got an unexpected number of dimensions for the '{}' feature."
           " Was expecting at most 1 dimension"
           " ([number of features]) since '{}' does not "
           "have a batch or time dimension, but got shape {}").format(
               _feature_keys.TrainEvalFeatures.VALUES,
               _feature_keys.TrainEvalFeatures.TIMES,
               features[_feature_keys.TrainEvalFeatures.VALUES].shape))
    # Add trivial batch and time dimensions for every feature
    features = {key: value[None, None, ...] for key, value in features.items()}
  if len(times.shape) == 1:  # shape [series length]
    if len(features[_feature_keys.TrainEvalFeatures.VALUES].shape
          ) == 1:  # shape [series length]
      # Add a feature dimension (with one feature)
      features[_feature_keys.TrainEvalFeatures.VALUES] = features[
          _feature_keys.TrainEvalFeatures.VALUES][..., None]
    elif len(features[_feature_keys.TrainEvalFeatures.VALUES].shape) > 2:
      raise ValueError(
          ("Got an unexpected number of dimensions for the '{}' feature."
           " Was expecting at most 2 dimensions"
           " ([series length, number of features]) since '{}' does not "
           "have a batch dimension, but got shape {}").format(
               _feature_keys.TrainEvalFeatures.VALUES,
               _feature_keys.TrainEvalFeatures.TIMES,
               features[_feature_keys.TrainEvalFeatures.VALUES].shape))
    # Add trivial batch dimensions for every feature
    features = {key: value[None, ...] for key, value in features.items()}
  elif len(features[_feature_keys.TrainEvalFeatures.TIMES].shape
          ) != 2:  # shape [batch size, series length]
    raise ValueError(
        ("Got an unexpected number of dimensions for times. Was expecting at "
         "most two ([batch size, series length]), but got shape {}.").format(
             times.shape))
  if require_single_batch:
    # We don't expect input to be already batched; batching is done later
    if features[_feature_keys.TrainEvalFeatures.TIMES].shape[0] != 1:
      raise ValueError("Got batch input, was expecting unbatched input.")
  return features


def _colate_features_to_feeds_and_fetches(signature, features, graph,
                                          continue_from=None):
  """Uses a saved model signature to construct feed and fetch dictionaries."""
  if continue_from is None:
    state_values = {}
  elif _feature_keys.FilteringResults.STATE_TUPLE in continue_from:
    # We're continuing from an evaluation, so we need to unpack/flatten state.
    state_values = _head.state_to_dictionary(
        continue_from[_feature_keys.FilteringResults.STATE_TUPLE])
  else:
    state_values = continue_from
  input_feed_tensors_by_name = {
      input_key: graph.as_graph_element(input_value.name)
      for input_key, input_value in signature.inputs.items()
  }
  output_tensors_by_name = {
      output_key: graph.as_graph_element(output_value.name)
      for output_key, output_value in signature.outputs.items()
  }
  feed_dict = {}
  for state_key, state_value in state_values.items():
    feed_dict[input_feed_tensors_by_name[state_key]] = state_value
  for feature_key, feature_value in features.items():
    feed_dict[input_feed_tensors_by_name[feature_key]] = feature_value
  return output_tensors_by_name, feed_dict


def predict_continuation(continue_from,
                         signatures,
                         session,
                         steps=None,
                         times=None,
                         exogenous_features=None):
  """Perform prediction using an exported saved model.

  Args:
    continue_from: A dictionary containing the results of either an Estimator's
      evaluate method or filter_continuation. Used to determine the model
      state to make predictions starting from.
    signatures: The `MetaGraphDef` protocol buffer returned from
      `tf.saved_model.loader.load`. Used to determine the names of Tensors to
      feed and fetch. Must be from the same model as `continue_from`.
    session: The session to use. The session's graph must be the one into which
      `tf.saved_model.loader.load` loaded the model.
    steps: The number of steps to predict (scalar), starting after the
      evaluation or filtering. If `times` is specified, `steps` must not be; one
      is required.
    times: A [batch_size x window_size] array of integers (not a Tensor)
      indicating times to make predictions for. These times must be after the
      corresponding evaluation or filtering. If `steps` is specified, `times`
      must not be; one is required. If the batch dimension is omitted, it is
      assumed to be 1.
    exogenous_features: Optional dictionary. If specified, indicates exogenous
      features for the model to use while making the predictions. Values must
      have shape [batch_size x window_size x ...], where `batch_size` matches
      the batch dimension used when creating `continue_from`, and `window_size`
      is either the `steps` argument or the `window_size` of the `times`
      argument (depending on which was specified).
  Returns:
    A dictionary with model-specific predictions (typically having keys "mean"
    and "covariance") and a _feature_keys.PredictionResults.TIMES key indicating
    the times for which the predictions were computed.
  Raises:
    ValueError: If `times` or `steps` are misspecified.
  """
  if exogenous_features is None:
    exogenous_features = {}
  predict_times = _model_utils.canonicalize_times_or_steps_from_output(
      times=times, steps=steps, previous_model_output=continue_from)
  features = {_feature_keys.PredictionFeatures.TIMES: predict_times}
  features.update(exogenous_features)
  predict_signature = signatures.signature_def[
      _feature_keys.SavedModelLabels.PREDICT]
  output_tensors_by_name, feed_dict = _colate_features_to_feeds_and_fetches(
      continue_from=continue_from,
      signature=predict_signature,
      features=features,
      graph=session.graph)
  output = session.run(output_tensors_by_name, feed_dict=feed_dict)
  output[_feature_keys.PredictionResults.TIMES] = features[
      _feature_keys.PredictionFeatures.TIMES]
  return output


def cold_start_filter(signatures, session, features):
  """Perform filtering using an exported saved model.

  Filtering refers to updating model state based on new observations.
  Predictions based on the returned model state will be conditioned on these
  observations.

  Starts from the model's default/uninformed state.

  Args:
    signatures: The `MetaGraphDef` protocol buffer returned from
      `tf.saved_model.loader.load`. Used to determine the names of Tensors to
      feed and fetch. Must be from the same model as `continue_from`.
    session: The session to use. The session's graph must be the one into which
      `tf.saved_model.loader.load` loaded the model.
    features: A dictionary mapping keys to Numpy arrays, with several possible
      shapes (requires keys `FilteringFeatures.TIMES` and
      `FilteringFeatures.VALUES`):
        Single example; `TIMES` is a scalar and `VALUES` is either a scalar or a
          vector of length [number of features].
        Sequence; `TIMES` is a vector of shape [series length], `VALUES` either
          has shape [series length] (univariate) or [series length x number of
          features] (multivariate).
        Batch of sequences; `TIMES` is a vector of shape [batch size x series
          length], `VALUES` has shape [batch size x series length] or [batch
          size x series length x number of features].
      In any case, `VALUES` and any exogenous features must have their shapes
      prefixed by the shape of the value corresponding to the `TIMES` key.
  Returns:
    A dictionary containing model state updated to account for the observations
    in `features`.
  """
  filter_signature = signatures.signature_def[
      _feature_keys.SavedModelLabels.COLD_START_FILTER]
  features = _canonicalize_numpy_data(data=features, require_single_batch=False)
  output_tensors_by_name, feed_dict = _colate_features_to_feeds_and_fetches(
      signature=filter_signature,
      features=features,
      graph=session.graph)
  output = session.run(output_tensors_by_name, feed_dict=feed_dict)
  # Make it easier to chain filter -> predict by keeping track of the current
  # time.
  output[_feature_keys.FilteringResults.TIMES] = features[
      _feature_keys.FilteringFeatures.TIMES]
  return output


def filter_continuation(continue_from, signatures, session, features):
  """Perform filtering using an exported saved model.

  Filtering refers to updating model state based on new observations.
  Predictions based on the returned model state will be conditioned on these
  observations.

  Args:
    continue_from: A dictionary containing the results of either an Estimator's
      evaluate method or a previous filter step (cold start or
      continuation). Used to determine the model state to start filtering from.
    signatures: The `MetaGraphDef` protocol buffer returned from
      `tf.saved_model.loader.load`. Used to determine the names of Tensors to
      feed and fetch. Must be from the same model as `continue_from`.
    session: The session to use. The session's graph must be the one into which
      `tf.saved_model.loader.load` loaded the model.
    features: A dictionary mapping keys to Numpy arrays, with several possible
      shapes (requires keys `FilteringFeatures.TIMES` and
      `FilteringFeatures.VALUES`):
        Single example; `TIMES` is a scalar and `VALUES` is either a scalar or a
          vector of length [number of features].
        Sequence; `TIMES` is a vector of shape [series length], `VALUES` either
          has shape [series length] (univariate) or [series length x number of
          features] (multivariate).
        Batch of sequences; `TIMES` is a vector of shape [batch size x series
          length], `VALUES` has shape [batch size x series length] or [batch
          size x series length x number of features].
      In any case, `VALUES` and any exogenous features must have their shapes
      prefixed by the shape of the value corresponding to the `TIMES` key.
  Returns:
    A dictionary containing model state updated to account for the observations
    in `features`.
  """
  filter_signature = signatures.signature_def[
      _feature_keys.SavedModelLabels.FILTER]
  features = _canonicalize_numpy_data(data=features, require_single_batch=False)
  output_tensors_by_name, feed_dict = _colate_features_to_feeds_and_fetches(
      continue_from=continue_from,
      signature=filter_signature,
      features=features,
      graph=session.graph)
  output = session.run(output_tensors_by_name, feed_dict=feed_dict)
  # Make it easier to chain filter -> predict by keeping track of the current
  # time.
  output[_feature_keys.FilteringResults.TIMES] = features[
      _feature_keys.FilteringFeatures.TIMES]
  return output

remove_undocumented(module_name=__name__)
