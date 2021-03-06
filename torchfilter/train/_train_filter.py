"""Private module; avoid importing from directly.
"""

from typing import Callable, cast

import fannypack
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import torchfilter


def _swap_batch_sequence_axes(tensor: torch.Tensor) -> torch.Tensor:
    """Converts data formatted as (N, T, ...) to (T, N, ...)"""
    return torch.transpose(tensor, 0, 1)


def train_filter(
    buddy: fannypack.utils.Buddy,
    filter_model: torchfilter.base.Filter,
    dataloader: DataLoader,
    initial_covariance: torch.Tensor,
    *,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor] = F.mse_loss,
    log_interval: int = 10,
    measurement_initialize=False,
    optimizer_name="train_filter_recurrent",
) -> None:
    """Trains a filter end-to-end via backpropagation through time for 1 epoch over a
    subsequence dataset.

    Args:
        buddy (fannypack.utils.Buddy): Training helper.
        filter_model (torchfilter.base.DynamicsModel): Model to train.
        dataloader (DataLoader): Loader for a SubsequenceDataset.
        initial_covariance (torch.Tensor): Covariance matrix of error in initial
            posterior, whose mean is sampled from a Gaussian centered at the
            ground-truth start state. Shape should be `(state_dim, state_dim)`.

    Keyword Args:
        loss_function (callable, optional): Loss function, from `torch.nn.functional`.
            Defaults to MSE.
        log_interval (int, optional): Minibatches between each Tensorboard log.
    """
    # Dataloader should load a SubsequenceDataset
    assert isinstance(dataloader.dataset, torchfilter.data.SubsequenceDataset)
    assert initial_covariance.shape == (filter_model.state_dim, filter_model.state_dim)
    assert filter_model.training, "Model needs to be set to train mode"

    # Track mean epoch loss
    epoch_loss = 0.0

    # Train filter model for 1 epoch
    batch_data: torchfilter.types.TrajectoryTorch
    for batch_idx, batch_data in enumerate(tqdm(dataloader)):
        # Move & unpack data
        batch_gpu = fannypack.utils.to_device(batch_data, buddy.device)

        states_labels = batch_gpu.states
        observations = batch_gpu.observations
        controls = batch_gpu.controls

        # Swap batch size, sequence length axes
        states_labels = _swap_batch_sequence_axes(states_labels)
        observations = fannypack.utils.SliceWrapper(observations).map(
            _swap_batch_sequence_axes
        )
        controls = fannypack.utils.SliceWrapper(controls).map(_swap_batch_sequence_axes)

        # Shape checks
        T, N, state_dim = states_labels.shape
        assert state_dim == filter_model.state_dim
        assert fannypack.utils.SliceWrapper(observations).shape[:2] == (T, N)
        assert fannypack.utils.SliceWrapper(controls).shape[:2] == (T, N)
        assert batch_idx != 0 or N == dataloader.batch_size

        # Populate initial filter belief
        if measurement_initialize:
            assert isinstance(
                filter_model, torchfilter.filters.VirtualSensorExtendedKalmanFilter
            ), "Only supported for virtual sensor EKFs!"

            # Initialize belief using virtual sensor
            cast(
                torchfilter.filters.VirtualSensorExtendedKalmanFilter, filter_model
            ).virtual_sensor_initialize_beliefs(
                observations=fannypack.utils.SliceWrapper(observations)[0]
            )
        else:
            # Initialize belief using `initial_covariance`
            initial_states_covariance = initial_covariance[None, :, :].expand(
                (N, state_dim, state_dim)
            )
            initial_states = torch.distributions.MultivariateNormal(
                states_labels[0], covariance_matrix=initial_states_covariance
            ).sample()

            filter_model.initialize_beliefs(
                mean=initial_states, covariance=initial_states_covariance
            )

        # Forward pass from the first state
        state_predictions = filter_model.forward_loop(
            observations=fannypack.utils.SliceWrapper(observations)[1:],
            controls=fannypack.utils.SliceWrapper(controls)[1:],
        )
        assert state_predictions.shape == (T - 1, N, state_dim)

        # Minimize loss
        loss = loss_function(state_predictions, states_labels[1:])
        buddy.minimize(loss, optimizer_name=optimizer_name)
        epoch_loss += fannypack.utils.to_numpy(loss)

        # Logging
        if batch_idx % log_interval == 0:
            with buddy.log_scope("train_filter_recurrent"):
                buddy.log_scalar("Training loss", loss)

    # Print average training loss
    epoch_loss /= len(dataloader)
    print("(train_filter) Epoch training loss: ", epoch_loss)
