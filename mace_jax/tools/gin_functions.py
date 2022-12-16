import datetime
import logging
import pickle
import time
from typing import Callable, Dict, List, Optional

import gin
import jax
import jax.numpy as jnp
import jraph
import numpy as np
import optax
from tqdm import tqdm
from unique_names_generator import get_random_name
from unique_names_generator.data import ADJECTIVES, NAMES

from mace_jax import modules, tools

loss = gin.configurable("loss")(modules.WeightedEnergyFrocesStressLoss)


@gin.configurable
def flags(
    debug: bool,
    dtype: str,
    seed: int,
    profile: bool = False,
):
    jax.config.update("jax_debug_nans", debug)
    jax.config.update("jax_debug_infs", debug)
    tools.set_default_dtype(dtype)
    tools.set_seeds(seed)
    if profile:
        import profile_nn_jax

        profile_nn_jax.enable(timing=True, statistics=True)
    return seed


@gin.configurable
def logs(
    name: str = None,
    level=logging.INFO,
    directory: str = "results",
):
    date = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    if name is None:
        name = get_random_name(
            separator="-", style="lowercase", combo=[ADJECTIVES, NAMES]
        )

    tag = f"{date}_{name}"

    tools.setup_logger(level, directory=directory, filename=f"{tag}.log", name=name)
    logger = tools.MetricsLogger(directory=directory, filename=f"{tag}.metrics")

    return directory, tag, logger


@gin.configurable
def reload(params, path=None):
    if path is not None:
        logging.info(f"Reloading parameters from '{path}'")
        with open(path, "rb") as f:
            _ = pickle.load(f)
            new_params = pickle.load(f)

        # check compatibility
        if jax.tree_util.tree_structure(params) != jax.tree_util.tree_structure(
            new_params
        ):
            logging.warning(
                f"Parameters from '{path}' are not compatible with current model"
            )

        return new_params
    return params


@gin.configurable
def checks(
    energy_forces_predictor, params, train_loader, *, enabled: bool = False
) -> bool:
    if not enabled:
        return False

    logging.info("We will check the normalization of the model and exit.")
    energies = []
    forces = []
    for graph in tqdm(train_loader):
        out = energy_forces_predictor(params, graph)
        node_mask = jraph.get_node_padding_mask(graph)
        graph_mask = jraph.get_graph_padding_mask(graph)
        energies += [out["energy"][graph_mask]]
        forces += [out["forces"][node_mask]]
    en = jnp.concatenate(energies)
    fo = jnp.concatenate(forces)
    fo = jnp.linalg.norm(fo, axis=1)

    logging.info(f"Energy: {jnp.mean(en):.3f} +/- {jnp.std(en):.3f}")
    logging.info(f"        min/max: {jnp.min(en):.3f}/{jnp.max(en):.3f}")
    logging.info(f"        median: {jnp.median(en):.3f}")
    logging.info(f"Forces: {jnp.mean(fo):.3f} +/- {jnp.std(fo):.3f}")
    logging.info(f"        min/max: {jnp.min(fo):.3f}/{jnp.max(fo):.3f}")
    logging.info(f"        median: {jnp.median(fo):.3f}")
    return True


@gin.configurable
def exponential_decay(
    lr: float,
    steps_per_epoch: int,
    *,
    transition_steps: float = 0.0,
    decay_rate: float = 0.5,
    transition_begin: float = 0.0,
    staircase: bool = True,
    end_value: Optional[float] = None,
):
    return optax.exponential_decay(
        init_value=lr,
        transition_steps=transition_steps * steps_per_epoch,
        decay_rate=decay_rate,
        transition_begin=transition_begin * steps_per_epoch,
        staircase=staircase,
        end_value=end_value,
    )


@gin.configurable
def piecewise_constant_schedule(
    lr: float, steps_per_epoch: int, *, boundaries_and_scales: Dict[float, float]
):
    boundaries_and_scales = {
        boundary * steps_per_epoch: scale
        for boundary, scale in boundaries_and_scales.items()
    }
    return optax.piecewise_constant_schedule(
        init_value=lr, boundaries_and_scales=boundaries_and_scales
    )


@gin.register
def constant_schedule(lr, steps_per_epoch):
    return optax.constant_schedule(lr)


gin.configurable("adam")(optax.scale_by_adam)
gin.configurable("amsgrad")(tools.scale_by_amsgrad)
gin.register("sgd")(optax.identity)


@gin.configurable
def optimizer(
    steps_per_epoch: int,
    weight_decay=0.0,
    lr=0.01,
    max_num_epochs: int = 2048,
    algorithm: Callable = optax.scale_by_adam,
    scheduler: Callable = constant_schedule,
):
    def weight_decay_mask(params):
        params = tools.flatten_dict(params)
        mask = {
            k: any(("linear_down" in ki) or ("symmetric_contraction" in ki) for ki in k)
            for k in params
        }
        assert any(any(("linear_down" in ki) for ki in k) for k in params)
        assert any(any(("symmetric_contraction" in ki) for ki in k) for k in params)
        return tools.unflatten_dict(mask)

    return (
        optax.chain(
            optax.add_decayed_weights(weight_decay, mask=weight_decay_mask),
            algorithm(),
            optax.scale_by_schedule(scheduler(lr, steps_per_epoch)),
            optax.scale(-1.0),  # Gradient descent.
        ),
        max_num_epochs,
    )


@gin.configurable
def train(
    model,
    params,
    optimizer_state,
    train_loader,
    valid_loader,
    test_loader,
    gradient_transform,
    max_num_epochs,
    logger,
    directory,
    tag,
    *,
    patience: int,
    eval_train: bool = False,
    eval_test: bool = False,
    eval_interval: int = 1,
    log_errors: str = "PerAtomRMSE",
    **kwargs,
):
    lowest_loss = np.inf
    patience_counter = 0
    loss_fn = loss()
    start_time = time.perf_counter()
    total_time_per_epoch = []
    eval_time_per_epoch = []

    for epoch, params, optimizer_state, ema_params in tools.train(
        model=model,
        params=params,
        loss_fn=loss_fn,
        train_loader=train_loader,
        gradient_transform=gradient_transform,
        optimizer_state=optimizer_state,
        start_epoch=0,
        logger=logger,
        **kwargs,
    ):
        total_time_per_epoch += [time.perf_counter() - start_time]
        start_time = time.perf_counter()

        try:
            import profile_nn_jax
        except ImportError:
            pass
        else:
            profile_nn_jax.restart_timer()

        last_epoch = epoch == max_num_epochs
        if epoch % eval_interval == 0 or last_epoch:
            with open(f"{directory}/{tag}.pkl", "wb") as f:
                pickle.dump(gin.operative_config_str(), f)
                pickle.dump(params, f)

            def eval_and_print(loader, mode: str):
                loss_, metrics_ = tools.evaluate(
                    model=model,
                    params=ema_params,
                    loss_fn=loss_fn,
                    data_loader=loader,
                )
                metrics_["mode"] = mode
                metrics_["epoch"] = epoch
                logger.log(metrics_)

                def _(x):
                    return "N/A" if x is None else f"{1e3 * x:.1f}"

                if log_errors == "PerAtomRMSE":
                    error_e = "rmse_e_per_atom"
                    error_f = "rmse_f"
                    error_s = "rmse_s"
                elif log_errors == "TotalRMSE":
                    error_e = "rmse_e"
                    error_f = "rmse_f"
                    error_s = "rmse_s"
                elif log_errors == "PerAtomMAE":
                    error_e = "mae_e_per_atom"
                    error_f = "mae_f"
                    error_s = "mae_s"
                elif log_errors == "TotalMAE":
                    error_e = "mae_e"
                    error_f = "mae_f"
                    error_s = "mae_s"

                logging.info(
                    f"Epoch {epoch}: {mode}: "
                    f"loss={loss_:.4f}, "
                    f"{error_e}={_(metrics_[error_e])} meV, "
                    f"{error_f}={_(metrics_[error_f])} meV/A, "
                    f"{error_s}={_(metrics_[error_s])} meV/A^3"
                )
                return loss_

            if eval_train or last_epoch:
                eval_and_print(train_loader, "eval_train")

            if (
                (eval_test or last_epoch)
                and test_loader is not None
                and len(test_loader) > 0
            ):
                eval_and_print(test_loader, "eval_test")

            if valid_loader is not None and len(valid_loader) > 0:
                loss_ = eval_and_print(valid_loader, "eval_valid")

                if loss_ >= lowest_loss:
                    patience_counter += 1
                    if patience_counter >= patience:
                        logging.info(
                            f"Stopping optimization after {patience_counter} epochs without improvement"
                        )
                        break
                else:
                    lowest_loss = loss_
                    patience_counter = 0

            eval_time_per_epoch += [time.perf_counter() - start_time]
            avg_time_per_epoch = np.mean(total_time_per_epoch[-eval_interval:])
            avg_eval_time_per_epoch = np.mean(eval_time_per_epoch[-eval_interval:])

            logging.info(
                f"Epoch {epoch}: Time per epoch: {avg_time_per_epoch:.1f}s, "
                f"among which {avg_eval_time_per_epoch:.1f}s for evaluation."
            )
        else:
            eval_time_per_epoch += [time.perf_counter() - start_time]  # basically 0

        if last_epoch:
            break

    logging.info("Training complete")
    return epoch, ema_params


def parse_argv(argv: List[str]):
    def gin_bind_parameter(key: str, value: str):
        # We need to guess if value is a string or not
        value = value.strip()
        if value[0] == value[-1] and value[0] in ('"', "'"):
            gin.parse_config(f"{key} = {value}")
        if value[0] == "@":
            gin.parse_config(f"{key} = {value}")
        if value in ["True", "False", "None"]:
            gin.parse_config(f"{key} = {value}")
        if any(c.isalpha() for c in value):
            gin.parse_config(f'{key} = "{value}"')
        else:
            gin.parse_config(f"{key} = {value}")

    only_the_key = None
    for arg in argv[1:]:
        if only_the_key is None:
            if arg.endswith(".gin"):
                gin.parse_config_file(arg)
            elif arg.startswith("--"):
                if "=" in arg:
                    key, value = arg[2:].split("=")
                    gin_bind_parameter(key, value)
                else:
                    only_the_key = arg[2:]
            else:
                raise ValueError(
                    f"Unknown argument: '{arg}'. Expected a .gin file or a --key \"some value\" pair."
                )
        else:
            gin_bind_parameter(only_the_key, arg)
            only_the_key = None
