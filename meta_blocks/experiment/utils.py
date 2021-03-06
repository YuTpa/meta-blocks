"""Utility functions for running experiments."""

import collections
import contextlib
import logging

import tensorflow.compat.v1 as tf

from meta_blocks import (
    adaptation,
    common,
    datasets,
    models,
    optimizers,
    samplers,
    tasks,
)

logger = logging.getLogger(__name__)

# Transition to V2 will happen in stages.
tf.disable_v2_behavior()
tf.enable_resource_variables()


class Experiment(
    collections.namedtuple("Experiment", ("meta_learners", "samplers", "task_dists"))
):
    """Represents built entities for the Experiment.

    Parameters
    ----------
    meta_learners: list of `AdaptationStrategy`s

    samplers: list of `Sampler`s

    task_dists: list of `TaskDistribution`s
    """

    pass


@contextlib.contextmanager
def session(gpu_allow_growth=True, log_device_placement=False):
    """Sets up an experiment and returns a tf.Session.

    Parameters
    ----------
    gpu_allow_growth : bool, optional (default=True)
        Used to configure tf.Session.
        See tf.ConfigProto.gpu_options.allow_growth.

    log_device_placement : bool, optional (default=False)
        Used for debugging tensor placement on devices.
        See tf.ConfigProto.log_device_placement.
    """
    # Reset TF graph.
    tf.reset_default_graph()

    # Create and configure a tf.Session.
    config = tf.ConfigProto()
    if gpu_allow_growth:
        config.gpu_options.allow_growth = True
    if log_device_placement:
        config.log_device_placement = True
    sess = tf.Session(config=config)

    try:
        yield sess
    finally:
        sess.close()


def build_and_initialize(cfg, sess, categories, mode=common.ModeKeys.TRAIN):
    """Builds and initializes all parts of the graph.

    Parameters
    ----------
    cfg : OmegaConf
        The experiment configuration.

    sess : tf.Session
        The TF session used for executing the computation graph.

    categories : dict of lists of Categories
        Each list of Categories is used to construct meta-datasets.

    mode : str, optional (default: common.ModeKeys.TRAIN)
        Defines the mode of the computation graph (TRAIN or EVAL).
        Note: this is likely to be removed from the API down the line.

    Returns
    -------
    exp : Experiment
        An object that represents the experiment.
        Contains `meta_learners`, `samplers`, and `task_dists`.
    """
    # Build and initialize data pools.
    data_pools = {
        task.set_name: datasets.get_datapool(
            dataset_name=cfg.data.name,
            categories=categories[task.set_name],
            name=f"DP_{task.log_dir.replace('/', '_')}",
        )
        .build(**cfg.data.build_config)
        .initialize(sess)
        for task in cfg[mode].tasks
    }

    # Build meta-dataset.
    meta_datasets = {
        task.set_name: datasets.get_metadataset(
            dataset_name=cfg.data.name,
            data_pool=data_pools[task.set_name],
            batch_size=cfg[mode].meta.batch_size,
            name=f"MD_{task.log_dir.replace('/', '_')}",
            **cfg[mode].dataset,
        ).build()
        for task in cfg[mode].tasks
    }

    # Build model.
    model = models.get(
        dataset_name=cfg.data.name,
        num_classes=cfg[mode].dataset.num_classes,
        **cfg.model,
    )

    # Build optimizer.
    optimizer = optimizers.get(**cfg.train.optimizer)

    # Build task distributions.
    task_dists = [
        tasks.get_distribution(
            meta_dataset=meta_datasets[task.set_name],
            name_suffix=task.log_dir.replace("/", "_"),
            **task.config,
        )
        for task in cfg[mode].tasks
    ]

    # Build meta-learners.
    meta_learners = [
        adaptation.get(
            model=model,
            optimizer=optimizer,
            mode=mode,
            tasks=task_dists[i].task_batch,
            **cfg.adapt,
        )
        for i, task in enumerate(cfg[mode].tasks)
    ]

    # Build samplers.
    samplers_list = [
        samplers.get(
            learner=meta_learners[i], tasks=task_dists[i].task_batch, **task.sampler
        )
        for i, task in enumerate(cfg[mode].tasks)
    ]

    # Run global init.
    sess.run(tf.global_variables_initializer())

    # Initialize task distribution.
    for task_dist, sampler in zip(task_dists, samplers_list):
        task_dist.initialize(sampler=sampler, sess=sess)

    return Experiment(
        meta_learners=meta_learners, samplers=samplers_list, task_dists=task_dists
    )
