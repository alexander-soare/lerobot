#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
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
# limitations under the License.d
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import datasets
import numpy as np
import pytest
import torch

from lerobot.common.datasets.data_buffer import (
    DataBuffer,
    TimestampOutsideToleranceError,
    compute_sampler_weights,
)
from lerobot.common.datasets.lerobot_dataset import CODEBASE_VERSION, DATA_DIR, LeRobotDataset
from lerobot.common.datasets.utils import hf_transform_to_torch, load_hf_dataset, load_info, load_videos
from lerobot.common.datasets.video_utils import VideoFrame, decode_video_frames_torchvision
from tests.utils import DevTestingError

# Some constants for DataBuffer tests.
data_key = "data"
data_shape = (2, 3)  # just some arbitrary > 1D shape
buffer_capacity = 100
fps = 10


def make_new_buffer(
    write_dir: str | None = None, delta_timestamps: dict[str, list[float]] | None = None
) -> tuple[DataBuffer, str]:
    if write_dir is None:
        write_dir = f"/tmp/online_buffer_{uuid4().hex}"
    buffer = DataBuffer(
        write_dir,
        data_spec={data_key: {"shape": data_shape, "dtype": np.dtype("float32")}},
        buffer_capacity=buffer_capacity,
        fps=None if delta_timestamps is None else fps,
        delta_timestamps=delta_timestamps,
    )
    return buffer, write_dir


def make_spoof_data_frames(n_episodes: int, n_frames_per_episode: int) -> dict[str, np.ndarray]:
    new_data = {
        data_key: np.arange(n_frames_per_episode * n_episodes * np.prod(data_shape)).reshape(-1, *data_shape),
        DataBuffer.INDEX_KEY: np.arange(n_frames_per_episode * n_episodes),
        DataBuffer.EPISODE_INDEX_KEY: np.repeat(np.arange(n_episodes), n_frames_per_episode),
        DataBuffer.FRAME_INDEX_KEY: np.tile(np.arange(n_frames_per_episode), n_episodes),
        DataBuffer.TIMESTAMP_KEY: np.tile(np.arange(n_frames_per_episode) / fps, n_episodes),
    }
    return new_data


def test_non_mutate():
    """Checks that the data provided to the add_data method is copied rather than passed by reference.

    This means that mutating the data in the buffer does not mutate the original data.

    NOTE: If this test fails, it means some of the other tests may be compromised. For example, we can't trust
    a success case for `test_write_read`.
    """
    buffer, _ = make_new_buffer()
    new_data = make_spoof_data_frames(2, buffer_capacity // 4)
    new_data_copy = deepcopy(new_data)
    buffer.add_episodes(new_data)
    buffer._data[data_key][:] += 1
    assert all(np.array_equal(new_data[k], new_data_copy[k]) for k in new_data)


def test_index_error_no_data():
    buffer, _ = make_new_buffer()
    with pytest.raises(IndexError):
        buffer[0]


def test_index_error_with_data():
    buffer, _ = make_new_buffer()
    n_frames = buffer_capacity // 2
    new_data = make_spoof_data_frames(1, n_frames)
    buffer.add_episodes(new_data)
    with pytest.raises(IndexError):
        buffer[n_frames]
    with pytest.raises(IndexError):
        buffer[-n_frames - 1]


@pytest.mark.parametrize("do_reload", [False, True])
def test_write_read(do_reload: bool):
    """Checks that data can be added to the buffer and read back.

    If do_reload we delete the buffer object and load the buffer back from disk before reading.
    """
    buffer, write_dir = make_new_buffer()
    n_episodes = 2
    n_frames_per_episode = buffer_capacity // 4
    new_data = make_spoof_data_frames(n_episodes, n_frames_per_episode)
    buffer.add_episodes(new_data)

    if do_reload:
        del buffer
        buffer, _ = make_new_buffer(write_dir)

    assert len(buffer) == n_frames_per_episode * n_episodes
    for i, item in enumerate(buffer):
        assert all(isinstance(item[k], torch.Tensor) for k in item)
        assert np.array_equal(item[data_key].numpy(), new_data[data_key][i])


def test_read_data_key():
    """Tests that data can be added to a buffer and all data for a. specific key can be read back."""
    buffer, _ = make_new_buffer()
    n_episodes = 2
    n_frames_per_episode = buffer_capacity // 4
    new_data = make_spoof_data_frames(n_episodes, n_frames_per_episode)
    buffer.add_episodes(new_data)

    data_from_buffer = buffer.get_data_by_key(data_key)
    assert isinstance(data_from_buffer, torch.Tensor)
    assert np.array_equal(data_from_buffer.numpy(), new_data[data_key])


def test_fifo():
    """Checks that if data is added beyond the buffer capacity, we discard the oldest data first."""
    buffer, _ = make_new_buffer()
    n_frames_per_episode = buffer_capacity // 4
    n_episodes = 3
    new_data = make_spoof_data_frames(n_episodes, n_frames_per_episode)
    buffer.add_episodes(new_data)
    n_more_episodes = 2
    # Developer sanity check (in case someone changes the global `buffer_capacity`).
    assert (
        n_episodes + n_more_episodes
    ) * n_frames_per_episode > buffer_capacity, "Something went wrong with the test code."
    more_new_data = make_spoof_data_frames(n_more_episodes, n_frames_per_episode)
    buffer.add_episodes(more_new_data)
    assert len(buffer) == buffer_capacity, "The buffer should be full."

    expected_data = {}
    for k in new_data:
        # Concatenate, left-truncate, then roll, to imitate the cyclical FIFO pattern in DataBuffer.
        expected_data[k] = np.roll(
            np.concatenate([new_data[k], more_new_data[k]])[-buffer_capacity:],
            shift=len(new_data[k]) + len(more_new_data[k]) - buffer_capacity,
            axis=0,
        )

    for i, item in enumerate(buffer):
        assert all(isinstance(item[k], torch.Tensor) for k in item)
        assert np.array_equal(item[data_key].numpy(), expected_data[data_key][i])


def test_delta_timestamps_within_tolerance():
    """Check that getting an item with delta_timestamps within tolerance succeeds.

    Note: Copied from `test_datasets.py::test_load_previous_and_future_frames_within_tolerance`.
    """
    # Sanity check on global fps as we are assuming it is 10 here.
    assert fps == 10, "This test assumes fps==10"
    buffer, _ = make_new_buffer(delta_timestamps={"index": [-0.2, 0, 0.139]})
    new_data = make_spoof_data_frames(n_episodes=1, n_frames_per_episode=5)
    buffer.add_episodes(new_data)
    buffer.tolerance_s = 0.04
    item = buffer[2]
    data, is_pad = item["index"], item[f"index{DataBuffer.IS_PAD_POSTFIX}"]
    assert torch.allclose(data, torch.tensor([0, 2, 3])), "Data does not match expected values"
    assert not is_pad.any(), "Unexpected padding detected"


def test_delta_timestamps_outside_tolerance_inside_episode_range():
    """Check that getting an item with delta_timestamps outside of tolerance fails.

    We expect it to fail if and only if the requested timestamps are within the episode range.

    Note: Copied from
    `test_datasets.py::test_load_previous_and_future_frames_outside_tolerance_inside_episode_range`
    """
    # Sanity check on global fps as we are assuming it is 10 here.
    assert fps == 10, "This test assumes fps==10"
    buffer, _ = make_new_buffer(delta_timestamps={"index": [-0.2, 0, 0.141]})
    new_data = make_spoof_data_frames(n_episodes=1, n_frames_per_episode=5)
    buffer.add_episodes(new_data)
    buffer.tolerance_s = 0.04
    with pytest.raises(TimestampOutsideToleranceError):
        buffer[2]


def test_delta_timestamps_outside_tolerance_outside_episode_range():
    """Check that copy-padding of timestamps outside of the episode range works.

    Note: Copied from
    `test_datasets.py::test_load_previous_and_future_frames_outside_tolerance_outside_episode_range`
    """
    # Sanity check on global fps as we are assuming it is 10 here.
    assert fps == 10, "This test assumes fps==10"
    buffer, _ = make_new_buffer(delta_timestamps={"index": [-0.3, -0.24, 0, 0.26, 0.3]})
    new_data = make_spoof_data_frames(n_episodes=1, n_frames_per_episode=5)
    buffer.add_episodes(new_data)
    buffer.tolerance_s = 0.04
    item = buffer[2]
    data, is_pad = item["index"], item["index_is_pad"]
    assert torch.equal(data, torch.tensor([0, 0, 2, 4, 4])), "Data does not match expected values"
    assert torch.equal(
        is_pad, torch.tensor([True, False, False, True, True])
    ), "Padding does not match expected values"


@pytest.mark.parametrize(
    ("dataset_repo_id", "decode_video"),
    (
        ("lerobot/pusht", True),
        ("lerobot/pusht", False),
        ("lerobot/pusht_image", False),
    ),
)
def test_from_huggingface_hub(tmp_path: Path, dataset_repo_id: str, decode_video: bool):
    """Check that we can make a buffer from a Hugging Face Hub dataset repository.

    Check that the buffer we make, accurately reflects the hub dataset.
    """
    for iteration in range(2):  # do it twice to check that running with an existing cached buffer also works
        hf_dataset = load_hf_dataset(dataset_repo_id, version=CODEBASE_VERSION, root=DATA_DIR, split="train")
        hf_dataset.set_transform(lambda x: x)
        # Note: storage_dir specified explicitly in order to make use of pytest's temporary file fixture.
        # This ensures that the first time this loop is run, the storage directory does not already exist.
        storage_dir = tmp_path / DataBuffer._default_storage_dir_from_huggingface_hub(
            dataset_repo_id, hf_dataset._fingerprint, decode_video
        ).relative_to("/tmp")
        if iteration == 0 and storage_dir.exists():
            raise DevTestingError("The storage directory should not exist for the first pass of this test.")
        buffer = DataBuffer.from_huggingface_hub(
            dataset_repo_id,
            decode_video,
            storage_dir=storage_dir,
        )
        assert len(buffer) == len(hf_dataset)
        for k, feature in hf_dataset.features.items():
            if isinstance(feature, datasets.features.Image):
                assert np.array_equal(
                    buffer._data[k], np.stack([np.array(pil_img) for pil_img in hf_dataset[k]])
                )
            elif isinstance(feature, VideoFrame):
                if decode_video:
                    # Decode the video here.
                    lerobot_dataset_info = load_info(dataset_repo_id, version=CODEBASE_VERSION, root=DATA_DIR)
                    videos_path = load_videos(dataset_repo_id, version=CODEBASE_VERSION, root=DATA_DIR)
                    episode_indices = np.array(hf_dataset["episode_index"])
                    timestamps = np.array(hf_dataset["timestamp"])
                    all_imgs = []
                    for episode_index in np.unique(episode_indices):
                        episode_data_indices = np.where(episode_indices == episode_index)[0]
                        episode_timestamps = timestamps[episode_indices == episode_index]
                        episode_imgs = decode_video_frames_torchvision(
                            videos_path.parent / hf_dataset[k][episode_data_indices[0]]["path"],
                            episode_timestamps,
                            1 / lerobot_dataset_info["fps"] - 1e-4,
                            to_pytorch_format=False,
                        )
                        all_imgs.extend(episode_imgs)
                    assert np.array_equal(buffer._data[k], all_imgs)
                else:
                    # Check that the video paths are the same.
                    assert np.array_equal(
                        buffer._data[k], [item["path"].encode("ascii") for item in hf_dataset[k]]
                    )
            elif isinstance(feature, (datasets.features.Sequence, datasets.features.Value)):
                assert np.array_equal(buffer._data[k], hf_dataset[k])
            else:
                raise DevTestingError(f"Tests not implemented for this feature type: {type(feature)=}")


# Arbitrarily set small dataset sizes, making sure to have uneven sizes.
@pytest.mark.parametrize("offline_dataset_size", [0, 6])
@pytest.mark.parametrize("online_dataset_size", [0, 4])
@pytest.mark.parametrize("online_sampling_ratio", [0.0, 1.0])
def test_compute_sampler_weights_trivial(
    offline_dataset_size: int, online_dataset_size: int, online_sampling_ratio: float
):
    # Pass/skip the test if both datasets sizes are zero.
    if offline_dataset_size + online_dataset_size == 0:
        return
    # Create spoof offline dataset.
    offline_dataset = LeRobotDataset.from_preloaded(
        hf_dataset=datasets.Dataset.from_dict({"data": list(range(offline_dataset_size))})
    )
    offline_dataset.hf_dataset.set_transform(hf_transform_to_torch)
    if offline_dataset_size == 0:
        offline_dataset.episode_data_index = {}
    else:
        # Set up an episode_data_index with at least two episodes.
        offline_dataset.episode_data_index = {
            "from": torch.tensor([0, offline_dataset_size // 2]),
            "to": torch.tensor([offline_dataset_size // 2, offline_dataset_size]),
        }
    # Create spoof online datset.
    online_dataset, _ = make_new_buffer()
    if online_dataset_size > 0:
        online_dataset.add_episodes(
            make_spoof_data_frames(n_episodes=2, n_frames_per_episode=online_dataset_size // 2)
        )

    weights = compute_sampler_weights(
        offline_dataset, online_dataset=online_dataset, online_sampling_ratio=online_sampling_ratio
    )
    if offline_dataset_size == 0 or online_dataset_size == 0:
        expected_weights = torch.ones(offline_dataset_size + online_dataset_size)
    elif online_sampling_ratio == 0:
        expected_weights = torch.cat([torch.ones(offline_dataset_size), torch.zeros(online_dataset_size)])
    elif online_sampling_ratio == 1:
        expected_weights = torch.cat([torch.zeros(offline_dataset_size), torch.ones(online_dataset_size)])
    expected_weights /= expected_weights.sum()
    assert torch.allclose(weights, expected_weights)


def test_compute_sampler_weights_nontrivial_ratio():
    # Arbitrarily set small dataset sizes, making sure to have uneven sizes.
    # Create spoof offline dataset.
    offline_dataset = LeRobotDataset.from_preloaded(
        hf_dataset=datasets.Dataset.from_dict({"data": list(range(4))})
    )
    offline_dataset.hf_dataset.set_transform(hf_transform_to_torch)
    offline_dataset.episode_data_index = {
        "from": torch.tensor([0, 2]),
        "to": torch.tensor([2, 4]),
    }
    # Create spoof online datset.
    online_dataset, _ = make_new_buffer()
    online_dataset.add_episodes(make_spoof_data_frames(n_episodes=4, n_frames_per_episode=2))
    online_sampling_ratio = 0.8
    weights = compute_sampler_weights(
        offline_dataset, online_dataset=online_dataset, online_sampling_ratio=online_sampling_ratio
    )
    assert torch.allclose(
        weights, torch.tensor([0.05, 0.05, 0.05, 0.05, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    )


def test_compute_sampler_weights_nontrivial_ratio_and_drop_last_n():
    # Arbitrarily set small dataset sizes, making sure to have uneven sizes.
    # Create spoof offline dataset.
    offline_dataset = LeRobotDataset.from_preloaded(
        hf_dataset=datasets.Dataset.from_dict({"data": list(range(4))})
    )
    offline_dataset.hf_dataset.set_transform(hf_transform_to_torch)
    offline_dataset.episode_data_index = {
        "from": torch.tensor([0]),
        "to": torch.tensor([4]),
    }
    # Create spoof online datset.
    online_dataset, _ = make_new_buffer()
    online_dataset.add_episodes(make_spoof_data_frames(n_episodes=4, n_frames_per_episode=2))
    weights = compute_sampler_weights(
        offline_dataset, online_dataset=online_dataset, online_sampling_ratio=0.8, online_drop_n_last_frames=1
    )
    assert torch.allclose(
        weights, torch.tensor([0.05, 0.05, 0.05, 0.05, 0.2, 0.0, 0.2, 0.0, 0.2, 0.0, 0.2, 0.0])
    )


def test_compute_sampler_weights_drop_n_last_frames():
    """Note: test copied from test_sampler."""
    data_dict = {
        "timestamp": [0, 0.1],
        "index": [0, 1],
        "episode_index": [0, 0],
        "frame_index": [0, 1],
    }
    offline_dataset = LeRobotDataset.from_preloaded(hf_dataset=datasets.Dataset.from_dict(data_dict))
    offline_dataset.hf_dataset.set_transform(hf_transform_to_torch)
    offline_dataset.episode_data_index = {"from": torch.tensor([0]), "to": torch.tensor([2])}

    online_dataset, _ = make_new_buffer()
    online_dataset.add_episodes(make_spoof_data_frames(n_episodes=4, n_frames_per_episode=2))

    weights = compute_sampler_weights(
        offline_dataset,
        offline_drop_n_last_frames=1,
        online_dataset=online_dataset,
        online_sampling_ratio=0.5,
        online_drop_n_last_frames=1,
    )
    assert torch.allclose(weights, torch.tensor([0.5, 0, 0.125, 0, 0.125, 0, 0.125, 0, 0.125, 0]))
