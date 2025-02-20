import logging
from typing import Callable, Dict, List, Optional, Union

import ase.data
import e3nn_jax as e3nn
import gin
import haiku as hk
import jax
import jax.numpy as jnp
import jraph
import numpy as np

from mace_jax import data, modules, tools

gin.register(jax.nn.silu)
gin.register(jax.nn.relu)
gin.register(jax.nn.gelu)
gin.register(jnp.abs)
gin.register(jnp.tanh)
gin.register("identity")(lambda x: x)

gin.register("std_scaling")(tools.compute_mean_std_atomic_inter_energy)
gin.register("rms_forces_scaling")(tools.compute_mean_rms_energy_forces)


@gin.configurable
def constant_scaling(graphs, atomic_energies, *, mean=0.0, std=1.0):
    return mean, std


@gin.configurable
def bessel_basis(length, max_length, number: int):
    return e3nn.bessel(length, number, max_length)


@gin.configurable
def soft_envelope(
    length, max_length, arg_multiplicator: float = 2.0, value_at_origin: float = 1.2
):
    return e3nn.soft_envelope(
        length,
        max_length,
        arg_multiplicator=arg_multiplicator,
        value_at_origin=value_at_origin,
    )


@gin.configurable
def polynomial_envelope(length, max_length, degree0: int, degree1: int):
    return e3nn.poly_envelope(degree0, degree1, max_length)(length)


@gin.configurable
def u_envelope(length, max_length, p: int):
    return e3nn.poly_envelope(p - 1, 2, max_length)(length)


gin.external_configurable(modules.LinearNodeEmbeddingBlock, "LinearEmbedding")


@gin.configurable
class LinearMassEmbedding(hk.Module):
    def __init__(self, num_species: int, irreps_out: e3nn.Irreps):
        super().__init__()
        self.num_species = num_species
        self.irreps_out = e3nn.Irreps(irreps_out).filter("0e").regroup()

    def __call__(self, node_specie: jnp.ndarray) -> e3nn.IrrepsArray:
        w = hk.get_parameter(
            "embeddings",
            shape=(self.num_species, self.irreps_out.dim),
            dtype=jnp.float32,
            init=hk.initializers.RandomNormal(),
        )
        atomic_masses = jnp.asarray(ase.data.atomic_masses)[node_specie] / 90.0  # [...]
        return e3nn.IrrepsArray(
            self.irreps_out, w[node_specie] * atomic_masses[..., None]
        )


@gin.configurable
def model(
    *,
    r_max: float,
    atomic_energies_dict: Dict[int, float] = None,
    train_graphs: List[jraph.GraphsTuple] = None,
    initialize_seed: Optional[int] = None,
    scaling: Callable = None,
    atomic_energies: Union[str, np.ndarray, Dict[int, float]] = None,
    avg_num_neighbors: float = "average",
    avg_r_min: float = None,
    num_species: int = None,
    num_interactions=3,
    path_normalization="path",
    gradient_normalization="path",
    learnable_atomic_energies=False,
    radial_basis: Callable[[jnp.ndarray], jnp.ndarray] = bessel_basis,
    radial_envelope: Callable[[jnp.ndarray], jnp.ndarray] = soft_envelope,
    **kwargs,
):
    if train_graphs is None:
        z_table = None
    else:
        z_table = data.get_atomic_number_table_from_zs(
            z for graph in train_graphs for z in graph.nodes.species
        )
    logging.info(f"z_table= {z_table}")

    if avg_num_neighbors == "average":
        avg_num_neighbors = tools.compute_avg_num_neighbors(train_graphs)
        logging.info(
            f"Compute the average number of neighbors: {avg_num_neighbors:.3f}"
        )
    else:
        logging.info(f"Use the average number of neighbors: {avg_num_neighbors:.3f}")

    if avg_r_min == "average":
        avg_r_min = tools.compute_avg_min_neighbor_distance(train_graphs)
        logging.info(f"Compute the average min neighbor distance: {avg_r_min:.3f}")
    elif avg_r_min is None:
        logging.info("Do not normalize the radial basis (avg_r_min=None)")
    else:
        logging.info(f"Use the average min neighbor distance: {avg_r_min:.3f}")

    if atomic_energies is None:
        if atomic_energies_dict is None or len(atomic_energies_dict) == 0:
            atomic_energies = "average"
        else:
            atomic_energies = "isolated_atom"

    if atomic_energies == "average":
        atomic_energies_dict = data.compute_average_E0s(train_graphs, z_table)
        logging.info(
            f"Computed average Atomic Energies using least squares: {atomic_energies_dict}"
        )
        atomic_energies = np.array(
            [atomic_energies_dict.get(z, 0.0) for z in range(num_species)]
        )
    elif atomic_energies == "isolated_atom":
        logging.info(
            f"Using atomic energies from isolated atoms in the dataset: {atomic_energies_dict}"
        )
        atomic_energies = np.array(
            [atomic_energies_dict.get(z, 0.0) for z in range(num_species)]
        )
    elif atomic_energies == "zero":
        logging.info("Not using atomic energies")
        atomic_energies = np.zeros(num_species)
    elif isinstance(atomic_energies, np.ndarray):
        logging.info(
            f"Use Atomic Energies that are provided: {atomic_energies.tolist()}"
        )
        if atomic_energies.shape != (num_species,):
            logging.error(
                f"atomic_energies.shape={atomic_energies.shape} != (num_species={num_species},)"
            )
            raise ValueError
    elif isinstance(atomic_energies, dict):
        atomic_energies_dict = atomic_energies
        logging.info(f"Use Atomic Energies that are provided: {atomic_energies_dict}")
        atomic_energies = np.array(
            [atomic_energies_dict.get(z, 0.0) for z in range(num_species)]
        )
    else:
        raise ValueError(f"atomic_energies={atomic_energies} is not supported")

    # check that num_species is consistent with the dataset
    if z_table is None:
        if train_graphs is not None:
            for graph in train_graphs:
                if not np.all(graph.nodes.species < num_species):
                    raise ValueError(
                        f"max(graph.nodes.species)={np.max(graph.nodes.species)} >= num_species={num_species}"
                    )
    else:
        if max(z_table.zs) >= num_species:
            raise ValueError(
                f"max(z_table.zs)={max(z_table.zs)} >= num_species={num_species}"
            )

    if scaling is None:
        mean, std = 0.0, 1.0
    else:
        mean, std = scaling(train_graphs, atomic_energies)
        logging.info(
            f"Scaling with {scaling.__qualname__}: mean={mean:.2f}, std={std:.2f}"
        )

    kwargs.update(
        dict(
            r_max=r_max,
            avg_num_neighbors=avg_num_neighbors,
            num_interactions=num_interactions,
            avg_r_min=avg_r_min,
            num_species=num_species,
            radial_basis=radial_basis,
            radial_envelope=radial_envelope,
        )
    )
    logging.info(f"Create MACE with parameters {kwargs}")

    @hk.without_apply_rng
    @hk.transform
    def model_(
        vectors: jnp.ndarray,  # [n_edges, 3]
        node_z: jnp.ndarray,  # [n_nodes]
        senders: jnp.ndarray,  # [n_edges]
        receivers: jnp.ndarray,  # [n_edges]
    ) -> jnp.ndarray:
        e3nn.config("path_normalization", path_normalization)
        e3nn.config("gradient_normalization", gradient_normalization)

        mace = modules.MACE(output_irreps="0e", **kwargs)

        if hk.running_init():
            logging.info(
                "model: "
                f"num_features={mace.num_features} "
                f"hidden_irreps={mace.hidden_irreps} "
                f"sh_irreps={mace.sh_irreps} "
                f"interaction_irreps={mace.interaction_irreps} ",
            )

        contributions = mace(
            vectors, node_z, senders, receivers
        )  # [n_nodes, num_interactions, 0e]
        contributions = contributions.array[:, :, 0]  # [n_nodes, num_interactions]
        node_energies = jnp.sum(contributions, axis=1)  # [n_nodes, ]

        node_energies = mean + std * node_energies

        if learnable_atomic_energies:
            atomic_energies_ = hk.get_parameter(
                "atomic_energies",
                shape=(num_species,),
                init=hk.initializers.Constant(atomic_energies),
            )
        else:
            atomic_energies_ = jnp.asarray(atomic_energies)
        node_energies += atomic_energies_[node_z]  # [n_nodes, ]

        return node_energies

    if initialize_seed is not None:
        params = jax.jit(model_.init)(
            jax.random.PRNGKey(initialize_seed),
            jnp.zeros((1, 3)),
            jnp.array([16]),
            jnp.array([0]),
            jnp.array([0]),
        )
    else:
        params = None

    return model_.apply, params, num_interactions
