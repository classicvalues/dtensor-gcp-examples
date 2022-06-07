# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Bert model with dtensor.

This application sets up a Bert Model with 8 devices, using a 4x2 mesh,
4 for the batch dimension, and 2 for the model dimension.
"""

import argparse
import numpy as np
import os

import tensorflow as tf

from tensorflow.experimental import dtensor
import tensorflow_models as tfm
from tensorflow_models import nlp

ap = argparse.ArgumentParser()
ap.add_argument(
    "--prefix",
    default="gs://dtensor-checkpoints",
    help="prefix for checkpointing")
ap.add_argument(
    "--device-type", default="GPU", choices=["GPU", "CPU"], help="device type")

# Parameters for distribution(dtensor)

MODEL_DIM = "model"
BATCH_DIM = "batch"

mesh_dims = [
    (BATCH_DIM, 4),  # shard to 4 devices in batch dimension
    (MODEL_DIM, 2),  # shard to 2 devices in model dimension
]

# Parameters for Bert model
num_classes = 2  # sentiment classifier
vocab_size = 100  # small vocab size, just for demo

# Parameters for mock data
batch_size = 32
sequence_length = 10


# Util functions
def dprint(*args):
  """Print from all clients."""
  prefix = f"[Client: {dtensor.client_id():03d}] "
  lines = " ".join([str(a) for a in args]).split("\n")
  msg = "\n".join([(prefix + line) for line in lines])
  print(msg)


def rprint(*args):
  """Print from leader client."""
  if dtensor.client_id() == 0:
    dprint(*args)


def configure_virtual_cpus(ncpu):
  """Configures number of virtual CPUs for TensorFlow."""
  phy_devices = tf.config.list_physical_devices("CPU")
  tf.config.set_logical_device_configuration(phy_devices[0], [
      tf.config.LogicalDeviceConfiguration(),
  ] * ncpu)


client_mesh = None


def unique_across_clients(x):
  """Returns the set of unique values of x across all clients."""
  global client_mesh
  if client_mesh is None:
    client_mesh = dtensor.create_distributed_mesh(
        [("clients", dtensor.num_clients())],
        device_type="CPU",
        num_global_devices=dtensor.num_clients())

  packed = dtensor.pack([tf.constant([x], tf.int32)],
                        layout=dtensor.Layout(["clients"], client_mesh))
  replicated = dtensor.relayout(
      packed, layout=dtensor.Layout([dtensor.UNSHARDED], client_mesh))
  unique = np.unique(replicated.numpy())
  unique = np.sort(unique)
  return list(unique)


# ML starts here
def get_dataset(mesh):

  def get_pipeline_params(mesh=mesh):
    # The return value is similiar to tf.distribute.InputContext.
    # input_pipeline_id: the id of the input pipeline (0 ~ num_input_pipelines)
    # num_input_pipelines: number of unique input pipeline ids.
    # num_local_replica_in_sync: the number of data samples processed by this
    # client per sync.

    replicas = set()
    for entry in mesh.local_device_locations():
      replicas.add(entry[BATCH_DIM])
    first_replica = min(replicas)
    first_replicas = unique_across_clients(first_replica)
    pipeline_id = first_replicas.index(first_replica)
    return dict(
        input_pipeline_id=pipeline_id,
        num_input_pipelines=len(first_replicas),
        num_local_replicas_in_sync=len(replicas))

  pipeline_params = get_pipeline_params(mesh)

  dprint("Pipeline Params", pipeline_params)
  rng = np.random.RandomState(pipeline_params["input_pipeline_id"])

  word_id_data = rng.randint(vocab_size, size=(batch_size, sequence_length))
  mask_data = rng.randint(num_classes, size=(batch_size, sequence_length))
  type_id_data = rng.randint(num_classes, size=(batch_size, sequence_length))
  labels = rng.randint(num_classes, size=(batch_size))

  # Create dummy dataset
  client_local_batch_size = batch_size // mesh.dim_size(
      BATCH_DIM) * pipeline_params["num_local_replicas_in_sync"]
  client_local_dataset = tf.data.Dataset.from_tensor_slices(
      (word_id_data, mask_data, type_id_data, labels)).repeat()
  client_local_dataset = client_local_dataset.batch(client_local_batch_size)

  # Pack the input into dtensor
  def shard_data(tensor, batch_dim=BATCH_DIM, mesh=mesh):
    # Batch shard the per-client data tensor to a DTensor.
    layout = dtensor.Layout.batch_sharded(
        mesh, batch_dim, rank=len(tensor.shape))

    replicas = set()
    for entry in mesh.local_device_locations():
      replicas.add(entry[batch_dim])
    replica_to_slice = {
        replica_id: slice_id
        for slice_id, replica_id in enumerate(sorted(replicas))
    }

    # Each batch-replica will receive an equal slice of the data tensor.
    slices = tf.split(tensor, len(replicas), axis=0)

    # Find the slice for each local device, then pack the components to a DTensor.
    components = []
    for entry in mesh.local_device_locations():
      replica_id = entry[batch_dim]
      slice_id = replica_to_slice[replica_id]
      components.append(slices[slice_id])
    return dtensor.pack(components, layout)

  return client_local_dataset, shard_data


@tf.function
def train_step(model, feature, label, loss_obj, optimizer):

  with tf.GradientTape() as tape:
    predict = model(feature, training=True)
    loss = loss_obj(label, predict)

  gradients = tape.gradient(loss, model.trainable_variables)
  optimizer.apply_gradients(zip(gradients, model.trainable_variables))
  return loss


def train_model(model,
                optimizer,
                mesh,
                dataset,
                pack_fn,
                steps_per_epoch=10,
                num_epochs=3):

  rprint("Training started...")
  loss_obj = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)

  iterator = iter(dataset)
  train_losses = []
  for epoch in range(num_epochs):
    total_loss = 0.00
    for _ in range(steps_per_epoch):
      word_id_data, mask_data, type_id_data, labels = next(iterator)
      d_word_id_data = pack_fn(word_id_data)
      d_mask_data = pack_fn(mask_data)
      d_type_id_data = pack_fn(type_id_data)
      d_labels = pack_fn(labels)
      assert d_labels.shape[0] == batch_size
      # FIXME(rainwoodman): This run_on could have been merged into the model
      # We need it here becasue the init_scope in the keras model is eagerly
      # executed using the default mesh, which may differ from the mesh
      # we intended to use.
      with dtensor.run_on(mesh):
        total_loss += train_step(model,
                                 [d_word_id_data, d_mask_data, d_type_id_data],
                                 d_labels, loss_obj, optimizer)

    train_loss = tf.reduce_mean(total_loss / steps_per_epoch)

    rprint(f"Epoch {epoch}: Loss: {train_loss}")
    train_losses.append(train_loss)
  return train_losses


def get_model(mesh):
  """Returns a dtensor Bert model for the given Mesh."""
  layout_map = tf.keras.dtensor.experimental.LayoutMap(mesh=mesh)
  layout_map[".*pooler_transform.kernel"] = dtensor.Layout(
      [dtensor.UNSHARDED, MODEL_DIM], mesh)
  layout_map[".*pooler_transform.bias"] = dtensor.Layout([MODEL_DIM], mesh)
  layout_map[".*attention_layer.*key.*kernel"] = dtensor.Layout(
      [dtensor.UNSHARDED, dtensor.UNSHARDED, MODEL_DIM], mesh)
  layout_map[".*attention_layer.*key.*bias"] = dtensor.Layout(
      [MODEL_DIM, dtensor.UNSHARDED], mesh)
  layout_map[".*attention_layer.*query.*kernel"] = dtensor.Layout(
      [dtensor.UNSHARDED, dtensor.UNSHARDED, MODEL_DIM], mesh)
  layout_map[".*attention_layer.*query.*bias"] = dtensor.Layout(
      [MODEL_DIM, dtensor.UNSHARDED], mesh)
  layout_map[".*attention_layer.*value.*kernel"] = dtensor.Layout(
      [dtensor.UNSHARDED, dtensor.UNSHARDED, MODEL_DIM], mesh)
  layout_map[".*attention_layer.*value.*bias"] = dtensor.Layout(
      [MODEL_DIM, dtensor.UNSHARDED], mesh)
  layout_map[".*transformer/layer.\d*._output_dense.kernel"] = dtensor.Layout(
      [MODEL_DIM, dtensor.UNSHARDED], mesh)
  layout_map[".*transformer/layer.\d*._output_dense.bias"] = dtensor.Layout(
      [dtensor.UNSHARDED], mesh)

  with tf.keras.dtensor.experimental.layout_map_scope(layout_map=layout_map):
    #!!! We need to fix this. The tf.gather doesn't support SPMD at the moment,
    # we have to force the use_one_hot code path to walkaround the issue.
    embedding = nlp.layers.OnDeviceEmbedding(
        vocab_size=vocab_size,
        embedding_width=768,
        initializer=tf.keras.initializers.TruncatedNormal(stddev=0.02),
        use_one_hot=True,
        name="word_embeddings")

    network = nlp.networks.BertEncoder(
        vocab_size=vocab_size, embedding_layer=embedding)
    bert_classifier = nlp.models.BertClassifier(
        network, num_classes=num_classes)

  for weight in bert_classifier.trainable_weights:
    rprint(f"{weight.name} has layout spec: {weight.layout.sharding_specs}")

  return bert_classifier


def main():
  args = ap.parse_args()

  print("tensorflow version", tf.__version__)

  # Initializes multi-client dtensor.
  configure_virtual_cpus(8 // dtensor.num_clients())
  dtensor.initialize_multi_client()

  dprint("device type", args.device_type, "num local devices",
         dtensor.num_local_devices(args.device_type))

  # Creates the DTensor device mesh.
  mesh = dtensor.create_distributed_mesh(
      mesh_dims, device_type=args.device_type, num_global_devices=8)

  # Ensure model replicas are initialized identically by using an identical
  # RNG seed across the clients (for numpy, tf and keras)
  tf.keras.utils.set_random_seed(1337)
  tf.keras.backend.experimental.enable_tf_random_generator()

  # Data, model, and optimizer.
  dataset, pack_fn = get_dataset(mesh)

  # FIXME(rainwoodman): The run_on() should have been merged into the
  # keras.dtensor API points. Without run_on() the eagerly executed code
  # in Keras uses the default mesh which can be wrong and cause a segfault.
  with dtensor.run_on(mesh):
    model = get_model(mesh)
    optimizer = tf.keras.dtensor.experimental.optimizers.Adam(
        learning_rate=0.001, mesh=mesh)

  # Train the model
  train_model(model, optimizer, mesh, dataset, pack_fn)

  # Save a check point
  cpt = dtensor.DTensorCheckpoint(mesh=mesh, root=model)
  saved_path = cpt.save(os.path.join(args.prefix, "bert-checkpoint-1/cpt"))

  # Then load it back
  cpt.restore(saved_path)


if __name__ == "__main__":
  main()
